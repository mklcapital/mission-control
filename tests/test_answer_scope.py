"""One answer must not close another question. Stdlib only."""

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


class AnswerScopeTests(unittest.TestCase):
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

    def test_answering_one_question_leaves_its_sibling_open(self):
        self.seed([
            {"id": "q1", "status": "open", "project": "LOAN", "question": SENTINEL},
            {"id": "q2", "status": "open", "project": "TAX", "question": "Other question"},
        ])
        status, body, _hdrs = self.request(
            "POST",
            self.authed("/api/answer"),
            body=json.dumps({
                "id": "q2",
                "answer": "filed",
                "project": "EVIL",
                "question": SENTINEL,
            }).encode(),
        )
        self.assertEqual(status, 200, body)
        saved = self.questions()
        self.assertEqual(saved[0]["id"], "q1")
        self.assertEqual(saved[0]["status"], "open")
        self.assertNotIn("answer_summary", saved[0])
        self.assertEqual(saved[1]["status"], "answered")
        self.assertEqual(saved[1]["answer_summary"], "filed")
        self.assertEqual(saved[1]["question"], "Other question")

        self.assertEqual(len(self.answer_lines()), 1)
        record = json.loads(self.answer_lines()[0])
        self.assertEqual(record["id"], "q2")
        self.assertEqual(record["project"], "TAX")
        self.assertEqual(record["question"], "Other question")
        self.assertNotEqual(record["question"], SENTINEL)
        self.assertNotIn(SENTINEL, self.answer_lines()[0])
        self.assertNotIn(SENTINEL.encode(), body)

    def test_a_post_to_another_route_does_not_record_an_answer(self):
        self.seed([
            {"id": "q1", "status": "open", "project": "LOAN", "question": SENTINEL},
        ])
        status, body, hdrs = self.request(
            "POST",
            self.authed("/api/questions"),
            body=json.dumps({"id": "q1", "answer": "yes", "question": SENTINEL}).encode(),
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertNotIn("answer_summary", self.questions()[0])
        self.assertEqual(self.answer_lines(), [])

    def test_a_missing_page_does_not_leak_the_queue(self):
        os.remove(server.INDEX_FILE)
        self.seed([
            {"id": "q1", "status": "open", "project": "LOAN", "question": SENTINEL},
        ])
        status, body, hdrs = self.request("GET", self.authed("/"))
        self.assertEqual(status, 500, body)
        self.assertIn(b"index.html missing", body)
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertEqual(self.answer_lines(), [])


class AskOptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_whitespace_only_options_are_rejected(self):
        old = sys.argv
        sys.argv = [
            "ask.py", "--project", "loan", "--type", "choice",
            "--q", "Pick one?", "--opt", " |  | ",
        ]
        try:
            with self.assertRaises(SystemExit) as caught:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    ask.main()
        finally:
            sys.argv = old
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE))


if __name__ == "__main__":
    unittest.main()
