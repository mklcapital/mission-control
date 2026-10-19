"""HEAD must not write the queue, and a cursor timestamp is exclusive.

Stdlib only. The server binds localhost and writes only inside a temp dir.
"""

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import answers
import server


TOKEN = "unit-test-key"
SENTINEL = "SECRET_QUESTION_DO_NOT_LEAK"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


def http_request(host, port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, payload, hdrs
    finally:
        conn.close()


class HeadDoesNotWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.host, cls.port, cls.httpd = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=self.tmp.name)
        for name in ("QUESTIONS_FILE", "ANSWERS_FILE", "LOG_FILE", "LOCK_FILE", "INDEX_FILE", "TOKEN"):
            self.addCleanup(setattr, server, name, getattr(server, name))
        server.QUESTIONS_FILE = os.path.join(self.dir, "questions.json")
        server.ANSWERS_FILE = os.path.join(self.dir, "answers.jsonl")
        server.LOG_FILE = os.path.join(self.dir, "server.log")
        server.LOCK_FILE = os.path.join(self.dir, ".queue.lock")
        server.INDEX_FILE = os.path.join(self.dir, "index.html")
        server.TOKEN = TOKEN
        with open(server.INDEX_FILE, "w", encoding="utf-8") as fh:
            fh.write("<html>%s</html>\n" % SENTINEL)
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{"id": "q1", "question": SENTINEL, "status": "open"}], fh)

    def request(self, method, path):
        return http_request(self.host, self.port, method, path)

    def authed(self, path):
        return "%s?key=%s" % (path, quote(TOKEN))

    def test_head_with_the_right_key_has_an_empty_body(self):
        status, body, hdrs = self.request("GET", self.authed("/api/questions"))
        self.assertEqual(status, 200)
        self.assertIn(SENTINEL.encode(), body)

        status, body, hdrs = self.request("HEAD", self.authed("/api/questions"))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertGreater(int(hdrs["content-length"]), 0)
        self.assertNotEqual(hdrs.get("connection"), "close")

        status, body, hdrs = self.request("HEAD", self.authed("/"))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertIn("text/html", hdrs["content-type"])

        status, body, _hdrs = self.request("GET", self.authed("/"))
        self.assertEqual(status, 200)
        self.assertIn(SENTINEL.encode(), body)


class CursorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            for row in (
                {"ts": "2026-01-02T00:00:00", "id": "same", "answer": "no"},
                {"ts": "2026-01-02T00:00:01", "id": "next", "answer": "yes"},
            ):
                fh.write(json.dumps(row) + "\n")

    def run_main(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        return stdout.getvalue(), stderr.getvalue()

    def test_a_row_at_the_cursor_is_not_replayed(self):
        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("2026-01-02T00:00:00\n")

        stdout, stderr = self.run_main(["answers.py"])
        self.assertEqual(stderr, "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.splitlines()],
            ["next"],
        )
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-02T00:00:01\n")

        stdout, stderr = self.run_main(["answers.py"])
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "# no new answers\n")

    def test_since_uses_the_same_exclusive_boundary_and_does_not_move_the_cursor(self):
        stdout, stderr = self.run_main(["answers.py", "--since", "2026-01-02T00:00:00"])
        self.assertEqual(stderr, "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.splitlines()],
            ["next"],
        )
        self.assertFalse(os.path.exists(answers.CURSOR_FILE))


if __name__ == "__main__":
    unittest.main()
