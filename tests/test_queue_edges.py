"""Queue shape, poll precedence, and health independence. Stdlib only."""

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


class QueueEdgeTests(unittest.TestCase):
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

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body, headers)

    def authed(self, path):
        return "%s?key=%s" % (path, quote(TOKEN))

    def seed(self, questions):
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(questions, fh, ensure_ascii=False)

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def answer_lines(self):
        if not os.path.exists(server.ANSWERS_FILE):
            return []
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln]

    def test_health_ignores_a_wrong_key_and_leaks_nothing(self):
        self.seed([{"id": "q1", "question": SENTINEL, "status": "open"}])
        for path in ("/health?key=wrong-secret", "/health?key=%s" % quote(TOKEN)):
            status, body, hdrs = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(json.loads(body)["ok"], True)
            self.assertNotIn(SENTINEL.encode(), body)
            self.assertNotIn(TOKEN.encode(), body)
            self.assertNotIn(b"question", body)
            self.assertNotEqual(hdrs.get("connection"), "close")

    def test_a_question_without_status_can_be_answered_once(self):
        self.seed([{"id": "q1", "question": "Q", "project": "LOAN"}])
        body = json.dumps({"id": "q1", "answer": "yes"}).encode()
        status, _payload, _hdrs = self.request("POST", self.authed("/api/answer"), body=body)
        self.assertEqual(status, 200)
        self.assertEqual(self.questions()[0]["status"], "answered")
        self.assertEqual(len(self.answer_lines()), 1)

        status, payload, _hdrs = self.request("POST", self.authed("/api/answer"), body=body)
        self.assertEqual(status, 409)
        self.assertIn(b"already answered", payload)
        self.assertEqual(len(self.answer_lines()), 1)

    def test_unicode_answers_are_stored_unescaped(self):
        self.seed([{"id": "q1", "question": "Q", "project": "LOAN", "status": "open"}])
        body = json.dumps(
            {"id": "q1", "answer": "café", "other_text": "naïve", "note": "résumé"},
            ensure_ascii=False,
        ).encode()
        status, _payload, _hdrs = self.request("POST", self.authed("/api/answer"), body=body)
        self.assertEqual(status, 200)
        line = self.answer_lines()[0]
        self.assertIn("café", line)
        self.assertIn("naïve", line)
        self.assertIn("résumé", line)
        self.assertNotIn("\\u00e9", line)
        saved = self.questions()[0]
        self.assertEqual(saved["answer_summary"], "café — naïve [note: résumé]")
        self.assertNotIn("\\u", json.dumps(saved, ensure_ascii=False))

    def test_a_non_list_queue_is_not_replaced(self):
        original = b'{"id": "q1", "question": "%s"}\n' % SENTINEL.encode()
        with open(server.QUESTIONS_FILE, "wb") as fh:
            fh.write(original)
        try:
            status, _body, _hdrs = self.request(
                "POST",
                self.authed("/api/answer"),
                body=json.dumps({"id": "q1", "answer": "yes"}).encode(),
            )
        except (http.client.HTTPException, ConnectionError, TimeoutError):
            status = None
        else:
            self.assertNotEqual(status, 200)
        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))


class PollPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")

    def test_all_returns_history_and_ignores_since_without_moving_the_cursor(self):
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": "2026-01-01T00:00:00", "id": "old"}) + "\n")
            fh.write(json.dumps({"ts": "2026-01-05T00:00:00", "id": "new"}) + "\n")
        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("2026-01-03T00:00:00\n")

        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py", "--all", "--since", "2026-01-09T00:00:00"]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old

        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.getvalue().splitlines()],
            ["old", "new"],
        )
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-03T00:00:00\n")


if __name__ == "__main__":
    unittest.main()
