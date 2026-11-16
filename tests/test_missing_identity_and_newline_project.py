"""A stored question with no identity stays blank, and a newline project stays one row.

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

import ask
import server


TOKEN = "unit-test-key"
FORGED_PROJECT = "FORGED-PROJECT"
FORGED_QUESTION = "FORGED-QUESTION"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class MissingIdentityTests(unittest.TestCase):
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
        server.QUESTIONS_FILE = os.path.join(self.dir, "questions.json")
        server.ANSWERS_FILE = os.path.join(self.dir, "answers.jsonl")
        server.LOG_FILE = os.path.join(self.dir, "server.log")
        server.LOCK_FILE = os.path.join(self.dir, ".queue.lock")
        server.INDEX_FILE = os.path.join(self.dir, "index.html")
        server.TOKEN = TOKEN

    def seed(self, questions):
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(questions, fh)

    def exchange(self, method, path, body=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body)
            resp = conn.getresponse()
            payload = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, payload, hdrs
        finally:
            conn.close()

    def test_missing_project_and_question_stay_blank(self):
        # Defaults must not be filled from the payload, and they must not
        # become the string "None".
        self.seed([
            {"id": "q1", "status": "open"},
            {
                "id": "q2",
                "status": "open",
                "project": "FAB",
                "question": "sibling stays",
            },
        ])
        body = json.dumps({
            "id": "q1",
            "answer": "yes",
            "project": FORGED_PROJECT,
            "question": FORGED_QUESTION,
        }).encode()
        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 200, payload)
        self.assertNotIn("connection", hdrs)
        self.assertNotIn(FORGED_PROJECT.encode(), payload)
        self.assertNotIn(FORGED_QUESTION.encode(), payload)

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "q1")
        self.assertEqual(rows[0]["project"], "")
        self.assertEqual(rows[0]["question"], "")
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertEqual(rows[0]["other_text"], "")
        self.assertEqual(rows[0]["note"], "")
        rendered = json.dumps(rows)
        self.assertNotIn(FORGED_PROJECT, rendered)
        self.assertNotIn(FORGED_QUESTION, rendered)
        self.assertNotIn("None", rendered)
        self.assertNotIn(TOKEN, rendered)

        saved = self.questions()
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "yes")
        self.assertNotIn("project", saved[0])
        self.assertNotIn("question", saved[0])
        self.assertNotIn("[note:", saved[0]["answer_summary"])
        self.assertEqual(saved[1]["status"], "open")
        self.assertEqual(saved[1]["question"], "sibling stays")
        self.assertNotIn("answer_summary", saved[1])
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))

        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 409, payload)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertIn(b"already answered", payload)
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(sum(1 for line in fh if line.strip()), 1)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(FORGED_PROJECT, log)
        self.assertNotIn(FORGED_QUESTION, log)
        self.assertIn("ANSWER q1 = yes", log)

    def test_trailing_space_on_status_is_still_open(self):
        self.seed([{
            "id": "q1",
            "status": "answered ",
            "project": "LOAN",
            "question": "STATUS-SPACE-QUESTION",
        }])
        body = json.dumps({"id": "q1", "answer": "yes"}).encode()
        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 200, payload)
        self.assertNotIn("connection", hdrs)
        saved = self.questions()
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "yes")
        self.assertEqual(saved[0]["question"], "STATUS-SPACE-QUESTION")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertEqual(rows[0]["question"], "STATUS-SPACE-QUESTION")

        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 409, payload)
        self.assertEqual(hdrs.get("connection"), "close")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(sum(1 for line in fh if line.strip()), 1)

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)


class NewlineProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_newline_project_stays_one_row_and_the_note_is_not_options(self):
        project = 'fab\n{"id":"forged"}'
        note = "Keep|this\nCaSe"
        stdout = io.StringIO()
        old = sys.argv
        sys.argv = [
            "ask.py",
            "--project", project,
            "--type", "choice",
            "--q", "Which wing?",
            "--opt", "A|B",
            "--note", note,
        ]
        try:
            with contextlib.redirect_stdout(stdout):
                ask.main()
        finally:
            sys.argv = old

        qid = stdout.getvalue().strip()
        self.assertTrue(qid.endswith("-which-wing"), qid)
        self.assertNotIn("forged", qid)
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        saved = json.loads(raw)
        self.assertEqual(len(saved), 1)
        item = saved[0]
        self.assertEqual(item["id"], qid)
        self.assertEqual(item["project"], project.upper())
        self.assertIn("\n", item["project"])
        self.assertNotIn("forged", item["project"])
        self.assertEqual(item["context"], note)
        self.assertEqual(item["options"], ["A", "B"])
        self.assertEqual(item["question"], "Which wing?")
        self.assertEqual(item["type"], "choice")
        self.assertEqual(item["status"], "open")
        self.assertNotIn("\\u", raw)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))
        self.assertEqual([row.get("id") for row in saved], [qid])


if __name__ == "__main__":
    unittest.main()
