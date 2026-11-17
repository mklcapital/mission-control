"""An answered duplicate blocks its open twin, and ask.py keeps note spacing.

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
NOTE_SECRET = "NOTE-SECRET"
FORGED_PROJECT = "FORGED-PROJECT"
SECOND = "SECOND-STAYS-OPEN"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class AnsweredDuplicateTests(unittest.TestCase):
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

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_an_answered_duplicate_blocks_the_open_twin(self):
        # The first row with this id is already answered. A later open row
        # with the same id must stay open, and the submitted answer must not
        # be recorded against either row.
        self.seed([
            {
                "id": "q1",
                "status": "answered",
                "project": "LOAN",
                "question": "FIRST-ALREADY-ANSWERED",
                "answer_summary": "no",
                "answered_ts": "2026-01-01T00:00:00-05:00",
                "options": ["keep-me"],
            },
            {
                "id": "q1",
                "status": "open",
                "project": "FAB",
                "question": SECOND,
                "options": ["wing"],
            },
            {
                "id": "q2",
                "status": "open",
                "project": "LTM",
                "question": "sibling",
            },
        ])
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            before = fh.read()
        body = json.dumps({
            "id": "q1",
            "answer": "yes",
            "project": FORGED_PROJECT,
            "question": "FORGED-QUESTION",
            "note": NOTE_SECRET,
        }).encode()
        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 409, payload)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertIn(b"already answered", payload)
        decoded = json.loads(payload)
        self.assertEqual(decoded["answer_summary"], "no")
        self.assertEqual(decoded["answered_ts"], "2026-01-01T00:00:00-05:00")
        self.assertNotIn(b"yes", payload)
        self.assertNotIn(SECOND.encode(), payload)
        self.assertNotIn(NOTE_SECRET.encode(), payload)
        self.assertNotIn(FORGED_PROJECT.encode(), payload)
        self.assertNotIn(TOKEN.encode(), payload)
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))

        saved = self.questions()
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "no")
        self.assertEqual(saved[0]["options"], ["keep-me"])
        self.assertEqual(saved[1]["status"], "open")
        self.assertEqual(saved[1]["question"], SECOND)
        self.assertEqual(saved[1]["options"], ["wing"])
        self.assertNotIn("answer_summary", saved[1])
        self.assertEqual(saved[2]["status"], "open")

        body = json.dumps({"id": "q2", "answer": "yes"}).encode()
        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 200, payload)
        self.assertNotIn("connection", hdrs)
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "q2")
        self.assertEqual(rows[0]["project"], "LTM")
        self.assertEqual(rows[0]["question"], "sibling")
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertNotIn(NOTE_SECRET, json.dumps(rows))
        self.assertNotIn(TOKEN, json.dumps(rows))
        saved = self.questions()
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "no")
        self.assertEqual(saved[1]["status"], "open")
        self.assertEqual(saved[2]["status"], "answered")
        self.assertEqual(saved[2]["answer_summary"], "yes")

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("ANSWER q1", log)
        self.assertIn("ANSWER q2 = yes", log)
        self.assertNotIn(NOTE_SECRET, log)
        self.assertNotIn(SECOND, log)
        self.assertNotIn(FORGED_PROJECT, log)


class NoteSpacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_note_spacing_is_kept_and_is_not_part_of_the_id(self):
        note = "  keep  spaces  "
        stdout = io.StringIO()
        old = sys.argv
        sys.argv = [
            "ask.py",
            "--project", "loan",
            "--type", "yesno",
            "--q", "Ready now?",
            "--note", note,
        ]
        try:
            with contextlib.redirect_stdout(stdout):
                ask.main()
        finally:
            sys.argv = old

        qid = stdout.getvalue().strip()
        self.assertTrue(qid.endswith("-ready-now"), qid)
        self.assertNotIn("keep", qid)
        self.assertNotIn("spaces", qid)
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        saved = json.loads(raw)
        self.assertEqual(len(saved), 1)
        item = saved[0]
        self.assertEqual(item["id"], qid)
        self.assertEqual(item["project"], "LOAN")
        self.assertEqual(item["type"], "yesno")
        self.assertEqual(item["question"], "Ready now?")
        self.assertEqual(item["options"], [])
        self.assertEqual(item["context"], note)
        self.assertEqual(item["status"], "open")
        self.assertNotEqual(item["context"], note.strip())
        self.assertIn("  keep  spaces  ", raw)
        self.assertNotIn("keep", qid)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))


if __name__ == "__main__":
    unittest.main()
