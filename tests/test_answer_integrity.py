"""Answer identity, failed writes, and cursor whitespace. Stdlib only."""

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


class AnswerTests(unittest.TestCase):
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
        if not os.path.exists(server.ANSWERS_FILE) or os.path.isdir(server.ANSWERS_FILE):
            return []
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln]

    def _expect_rejected(self, method, path, body=None):
        try:
            status, payload, _hdrs = self.request(method, path, body=body)
        except (OSError, http.client.HTTPException):
            return
        self.assertGreaterEqual(status, 400)
        self.assertNotIn(SENTINEL.encode(), payload)

    def test_the_first_duplicate_id_wins(self):
        self.seed([
            {
                "id": "q1",
                "status": "answered",
                "project": "LOAN",
                "question": "first",
                "answer_summary": "original",
                "answered_ts": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "q1",
                "status": "open",
                "project": "LOAN",
                "question": SENTINEL,
            },
        ])
        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": "q1", "answer": "second"}).encode())
        self.assertEqual(status, 409)
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(b"second", body)
        saved = self.questions()
        self.assertEqual(saved[0]["answer_summary"], "original")
        self.assertEqual(saved[1]["status"], "open")
        self.assertNotIn("answer_summary", saved[1])
        self.assertEqual(self.answer_lines(), [])

    def test_id_types_do_not_cross_match(self):
        self.seed([{
            "id": 1,
            "status": "open",
            "project": "LOAN",
            "question": SENTINEL,
        }])
        self._expect_rejected(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": "1", "answer": "yes"}).encode())
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertEqual(self.answer_lines(), [])

        self.seed([{
            "id": "1",
            "status": "open",
            "project": "LOAN",
            "question": "Ship it?",
        }])
        status, _body, _hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": 1, "answer": "yes"}).encode())
        self.assertEqual(status, 200)
        record = json.loads(self.answer_lines()[0])
        self.assertEqual(record["id"], "1")
        self.assertEqual(record["question"], "Ship it?")
        self.assertEqual(self.questions()[0]["status"], "answered")

    def test_a_non_object_body_does_not_record_an_answer(self):
        self.seed([{
            "id": "q1",
            "status": "open",
            "project": "LOAN",
            "question": SENTINEL,
        }])
        self._expect_rejected(
            "POST", self.authed("/api/answer"),
            body=b"[1, 2, 3]")
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertEqual(self.answer_lines(), [])
        status, body, _hdrs = self.request("GET", self.authed("/api/questions"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["status"], "open")

    def test_an_unwritable_answer_log_does_not_mark_the_question(self):
        self.seed([{
            "id": "q1",
            "status": "open",
            "project": "LOAN",
            "question": SENTINEL,
        }])
        os.mkdir(server.ANSWERS_FILE)
        self._expect_rejected(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": "q1", "answer": "yes"}).encode())
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertFalse(os.path.isfile(server.ANSWERS_FILE))
        status, body, _hdrs = self.request("GET", self.authed("/api/questions"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["status"], "open")
        self.assertEqual(json.loads(body)[0]["question"], SENTINEL)


class AskTypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def run_ask(self, *args):
        stdout = io.StringIO()
        old = sys.argv
        sys.argv = ["ask.py", *args]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                ask.main()
        finally:
            sys.argv = old
        return stdout.getvalue().strip()

    def test_multi_does_not_upgrade_dropdown_or_text(self):
        self.run_ask(
            "--project", "loan", "--type", "dropdown", "--multi",
            "--q", "Which desk?", "--opt", " A | B ",
        )
        self.run_ask(
            "--project", "loan", "--type", "text", "--multi",
            "--q", "Anything else?",
        )
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual([item["type"] for item in saved], ["dropdown", "text"])
        self.assertEqual(saved[0]["options"], ["A", "B"])
        self.assertEqual(saved[1]["options"], [])
        self.assertTrue(all(item["status"] == "open" for item in saved))


class CursorWhitespaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")

    def test_a_padded_cursor_does_not_replay_older_rows(self):
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            for row in (
                {"ts": "2026-01-01T00:00:00", "id": "old", "answer": "no"},
                {"ts": "2026-01-03T00:00:00", "id": "new", "answer": "yes"},
            ):
                fh.write(json.dumps(row) + "\n")
        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("  2026-01-02T00:00:00  \n")

        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py"]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.getvalue().splitlines()],
            ["new"],
        )
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-03T00:00:00\n")

        stdout = io.StringIO()
        stderr = io.StringIO()
        sys.argv = ["answers.py"]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "# no new answers\n")


if __name__ == "__main__":
    unittest.main()
