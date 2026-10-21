"""A newline in an answer must not split the ledger, and /index.html stays gated.

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
PAGE = "PAGE_MARKER_DO_NOT_LEAK"
QUESTION = "QUESTION_MARKER_DO_NOT_LEAK"


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


class AnswerLineTests(unittest.TestCase):
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
        for name in (
            "QUESTIONS_FILE", "ANSWERS_FILE", "LOG_FILE", "LOCK_FILE",
            "INDEX_FILE", "TOKEN",
        ):
            self.addCleanup(setattr, server, name, getattr(server, name))
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        server.QUESTIONS_FILE = os.path.join(self.dir, "questions.json")
        server.ANSWERS_FILE = os.path.join(self.dir, "answers.jsonl")
        server.LOG_FILE = os.path.join(self.dir, "server.log")
        server.LOCK_FILE = os.path.join(self.dir, ".queue.lock")
        server.INDEX_FILE = os.path.join(self.dir, "index.html")
        server.TOKEN = TOKEN
        answers.ANSWERS_FILE = server.ANSWERS_FILE
        answers.CURSOR_FILE = os.path.join(self.dir, ".answers_cursor")
        with open(server.INDEX_FILE, "w", encoding="utf-8") as fh:
            fh.write("<html>%s</html>\n" % PAGE)
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([
                {"id": "q1", "question": QUESTION, "project": "LOAN", "status": "open"},
                {"id": "q2", "question": "sibling", "project": "LOAN", "status": "open"},
            ], fh)

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body=body, headers=headers)

    def authed(self, path):
        return "%s?key=%s" % (path, quote(TOKEN))

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_newline_in_the_answer_does_not_split_the_ledger(self):
        # answers.jsonl is one JSON object per line. A raw newline plus a
        # second object would make the poller return an injected row.
        payload_answer = (
            "yes\n"
            "{\"ts\":\"2999-01-01T00:00:00+00:00\",\"id\":\"q2\",\"answer\":\"pwned\"}"
        )
        payload_note = "note line\nstill the same note"
        status, body, hdrs = self.request(
            "POST",
            self.authed("/api/answer"),
            body=json.dumps({
                "id": "q1",
                "answer": payload_answer,
                "note": payload_note,
            }).encode(),
        )
        self.assertEqual(status, 200, body)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(QUESTION.encode(), body)

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            ledger = fh.read()
        self.assertEqual(ledger.count("\n"), 1, ledger)
        self.assertNotIn("\n{", ledger)
        record = json.loads(ledger)
        self.assertEqual(record["id"], "q1")
        self.assertEqual(record["answer"], payload_answer)
        self.assertEqual(record["note"], payload_note)
        self.assertNotEqual(record["id"], "q2")

        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py", "--all"]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        self.assertEqual(stderr.getvalue(), "")
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "q1")
        self.assertEqual(rows[0]["answer"], payload_answer)
        self.assertNotIn("pwned", rows[0]["id"])
        self.assertFalse(os.path.exists(answers.CURSOR_FILE))

        queued = self.questions()
        self.assertEqual(queued[0]["status"], "answered")
        self.assertEqual(queued[0]["id"], "q1")
        self.assertEqual(queued[1]["status"], "open")
        self.assertEqual(queued[1]["id"], "q2")
        self.assertNotIn(TOKEN, ledger)

    def test_index_html_is_gated_like_the_page(self):
        status, body, hdrs = self.request("GET", "/index.html")
        self.assertEqual(status, 403)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(PAGE.encode(), body)
        self.assertNotIn(QUESTION.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        status, body, hdrs = self.request("GET", "/index.html?key=wrong")
        self.assertEqual(status, 403)
        self.assertNotIn(PAGE.encode(), body)
        self.assertNotIn(QUESTION.encode(), body)

        status, body, hdrs = self.request("GET", self.authed("/index.html"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body, ("<html>%s</html>\n" % PAGE).encode())
        self.assertTrue(hdrs["content-type"].startswith("text/html"))
        self.assertIn("frame-ancestors 'none'", hdrs["content-security-policy"])
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(QUESTION.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        status, body, hdrs = self.request("HEAD", self.authed("/index.html"))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertTrue(hdrs["content-type"].startswith("text/html"))
        self.assertIn("frame-ancestors 'none'", hdrs["content-security-policy"])
        self.assertNotIn(PAGE.encode(), body)

        self.assertEqual(self.questions()[1]["status"], "open")
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))


if __name__ == "__main__":
    unittest.main()
