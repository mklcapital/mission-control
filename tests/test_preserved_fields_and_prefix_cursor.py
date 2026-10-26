"""Answering keeps the question definition, and a short cursor is not a prefix.

Stdlib only. The server binds localhost and every data file is in a temp dir.
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


class PreserveFieldsTests(unittest.TestCase):
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

    def test_answering_keeps_the_question_definition(self):
        self.seed([
            {
                "id": "q1",
                "status": "open",
                "project": "LOAN",
                "type": "choice",
                "question": "Which filing?",
                "options": ["Keep", "café"],
                "context": "see the binder",
                "extra_flag": True,
            },
            {
                "id": "q2",
                "status": "open",
                "project": "TAX",
                "type": "yesno",
                "question": "Other",
                "options": ["nope"],
            },
        ])
        status, body, _hdrs = self.request(
            "POST",
            self.authed("/api/answer"),
            body=json.dumps({
                "id": "q1",
                "answer": "Keep",
                "options": ["admin"],
                "type": "text",
                "context": "hijacked",
                "extra_flag": False,
                "question": "hijacked",
                "project": "EVIL",
            }).encode(),
        )
        self.assertEqual(status, 200, body)
        saved = self.questions()
        self.assertEqual(saved[0]["options"], ["Keep", "café"])
        self.assertEqual(saved[0]["type"], "choice")
        self.assertEqual(saved[0]["context"], "see the binder")
        self.assertIs(saved[0]["extra_flag"], True)
        self.assertEqual(saved[0]["question"], "Which filing?")
        self.assertEqual(saved[0]["project"], "LOAN")
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "Keep")
        self.assertEqual(saved[1]["status"], "open")
        self.assertEqual(saved[1]["options"], ["nope"])
        self.assertNotIn("answer_summary", saved[1])
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))

        self.assertEqual(len(self.answer_lines()), 1)
        record = json.loads(self.answer_lines()[0])
        self.assertEqual(set(record), {
            "ts", "id", "project", "question", "answer", "other_text", "note",
        })
        self.assertEqual(record["id"], "q1")
        self.assertEqual(record["project"], "LOAN")
        self.assertEqual(record["question"], "Which filing?")
        self.assertEqual(record["answer"], "Keep")
        ledger = self.answer_lines()[0]
        for leaked in ("café", "admin", "hijacked", "EVIL", "see the binder"):
            self.assertNotIn(leaked, ledger)

        status, body, _hdrs = self.request("GET", self.authed("/api/questions"))
        self.assertEqual(status, 200, body)
        listed = json.loads(body)
        self.assertEqual(listed[0]["options"], ["Keep", "café"])
        self.assertEqual(listed[1]["options"], ["nope"])
        self.assertNotIn(TOKEN.encode(), body)
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            self.assertNotIn(TOKEN, fh.read())


class PrefixCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            for row in (
                {"ts": "2026-01-01T23:00:00", "id": "before", "answer": "no"},
                {"ts": "2026-01-02", "id": "exact", "answer": "no"},
                {"ts": "2026-01-02T00:00:00", "id": "same-day", "answer": "yes"},
                {"ts": "2026-01-03T00:00:00", "id": "later", "answer": "yes"},
            ):
                fh.write(json.dumps(row) + "\n")
        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("2026-01-02\n")

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

    def test_a_shorter_cursor_does_not_swallow_the_same_day(self):
        # String order, not a prefix match: "2026-01-02" must not hide
        # "2026-01-02T00:00:00".
        stdout, stderr = self.run_poll()
        self.assertEqual(stderr, "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.splitlines()],
            ["same-day", "later"],
        )
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-03T00:00:00\n")

        stdout, stderr = self.run_poll()
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "# no new answers\n")
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-03T00:00:00\n")


if __name__ == "__main__":
    unittest.main()
