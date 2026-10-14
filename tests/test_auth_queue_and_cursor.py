"""Auth bypasses, a corrupt queue, and timestamp-less answer rows.

Stdlib only. The HTTP tests bind localhost and redirect every data file
into a temp directory; they do not touch the repo's runtime queue.
"""

import contextlib
import fcntl
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
import ask
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


class QueueTests(unittest.TestCase):
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
        self.addCleanup(setattr, server, "QUESTIONS_FILE", server.QUESTIONS_FILE)
        self.addCleanup(setattr, server, "ANSWERS_FILE", server.ANSWERS_FILE)
        self.addCleanup(setattr, server, "LOG_FILE", server.LOG_FILE)
        self.addCleanup(setattr, server, "LOCK_FILE", server.LOCK_FILE)
        self.addCleanup(setattr, server, "INDEX_FILE", server.INDEX_FILE)
        self.addCleanup(setattr, server, "TOKEN", server.TOKEN)
        server.QUESTIONS_FILE = os.path.join(self.dir, "questions.json")
        server.ANSWERS_FILE = os.path.join(self.dir, "answers.jsonl")
        server.LOG_FILE = os.path.join(self.dir, "server.log")
        server.LOCK_FILE = os.path.join(self.dir, ".queue.lock")
        server.INDEX_FILE = os.path.join(self.dir, "index.html")
        server.TOKEN = TOKEN
        with open(server.INDEX_FILE, "w", encoding="utf-8") as fh:
            fh.write("<html>queue</html>\n")

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body, headers)

    def authed(self, path):
        return "%s?key=%s" % (path, quote(TOKEN))

    def seed(self, questions):
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(questions, fh)

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def answer_lines(self):
        if not os.path.exists(server.ANSWERS_FILE):
            return []
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln]

    def test_a_body_key_cannot_authorize_a_post(self):
        self.seed([{
            "id": "q1",
            "status": "open",
            "question": SENTINEL,
            "project": "LOAN",
        }])
        body = json.dumps({"key": TOKEN, "id": "q1", "answer": "yes"}).encode()
        status, payload, hdrs = self.request("POST", "/api/answer", body=body)
        self.assertEqual(status, 403)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), payload)
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertEqual(self.answer_lines(), [])

        status, payload, hdrs = self.request(
            "POST", "/api/answer?key=wrong",
            body=json.dumps({"id": "q1", "answer": "yes"}).encode())
        self.assertEqual(status, 403)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertEqual(self.answer_lines(), [])
        self.assertEqual(self.questions()[0]["status"], "open")

        status, _payload, hdrs = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotEqual(hdrs.get("connection"), "close")

    def test_corrupt_queue_is_left_intact_and_does_not_stick_the_lock(self):
        original = "{not-json"
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            fh.write(original)

        self._expect_rejected("GET", self.authed("/api/questions"))
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)
        self.assertEqual(self.answer_lines(), [])

        status, body, _hdrs = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn(original.encode(), body)

        self.seed([{"id": "q1", "status": "open", "question": "still here"}])
        status, body, _hdrs = self.request("GET", self.authed("/api/questions"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["id"], "q1")

        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            fh.write(original)
        self._expect_rejected(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": "q1", "answer": "yes"}).encode())
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)
        self.assertEqual(self.answer_lines(), [])

    def _expect_rejected(self, method, path, body=None):
        """A bad queue may drop the connection or return an error. Either way
        the handler must not report success."""
        try:
            status, _payload, _hdrs = self.request(method, path, body=body)
        except OSError:
            return
        self.assertGreaterEqual(status, 400)


class AskIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_a_corrupt_queue_is_not_replaced(self):
        original = "{not-json"
        with open(ask.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            fh.write(original)
        old = sys.argv
        sys.argv = ["ask.py", "--project", "LOAN", "--type", "yesno", "--q", "Rebooted?"]
        try:
            with self.assertRaises(json.JSONDecodeError):
                ask.main()
        finally:
            sys.argv = old

        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))

        fh = open(ask.LOCK_FILE, "a")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")

    def write_rows(self, rows):
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def cursor(self):
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            return fh.read()

    def run_poll(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py", *args]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        return stdout.getvalue(), stderr.getvalue()

    def test_rows_without_timestamps_do_not_move_the_cursor(self):
        self.write_rows([
            {"id": "blank", "answer": "hidden"},
            {"ts": "", "id": "empty", "answer": "hidden"},
            {"ts": "2026-01-02T00:00:00", "id": "visible", "answer": "yes"},
        ])
        stdout, stderr = self.run_poll()
        self.assertEqual(stderr, "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.splitlines()],
            ["visible"],
        )
        self.assertEqual(self.cursor(), "2026-01-02T00:00:00\n")

        stdout, stderr = self.run_poll("--since", "2026-01-01T00:00:00")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.splitlines()],
            ["visible"],
        )
        self.assertEqual(self.cursor(), "2026-01-02T00:00:00\n")

        stdout, stderr = self.run_poll("--all")
        self.assertEqual(stderr, "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.splitlines()],
            ["blank", "empty", "visible"],
        )
        self.assertEqual(self.cursor(), "2026-01-02T00:00:00\n")

        stdout, stderr = self.run_poll()
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "# no new answers\n")
        self.assertEqual(self.cursor(), "2026-01-02T00:00:00\n")


if __name__ == "__main__":
    unittest.main()
