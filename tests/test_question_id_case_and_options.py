"""Question ids are case-sensitive, and OPTIONS cannot record an answer.

Stdlib only. The server binds localhost and every data file is in a temp dir.
"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import server


TOKEN = "unit-test-key"
QUESTION = "QUEUE-SENTINEL-do-not-leak"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def response_of(resp):
    payload = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    return resp.status, payload, hdrs


class QueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd = start_server()
        cls.host, cls.port = cls.httpd.server_address

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

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.putrequest(method, path)
            if body is not None and headers is None:
                conn.putheader("Content-Length", str(len(body)))
                conn.putheader("Content-Type", "application/json")
            for key, value in headers or ():
                conn.putheader(key, value)
            conn.endheaders(body)
            return response_of(conn.getresponse())
        finally:
            conn.close()

    def seed(self):
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([
                {"id": "q1", "question": QUESTION, "project": "LOAN", "status": "open"},
                {"id": "q2", "question": "sibling stays open", "project": "FAB", "status": "open"},
            ], fh)

    def ledger(self):
        if not os.path.exists(server.ANSWERS_FILE):
            return []
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def log(self):
        if not os.path.exists(server.LOG_FILE):
            return ""
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_question_id_match_is_case_sensitive(self):
        # "Q1" is a different id from "q1". A case-folded match would answer
        # the wrong question and then 409 the real one.
        self.seed()
        body = json.dumps({
            "id": "Q1",
            "answer": "FORGED-CASE",
            "project": "FORGED",
            "question": "FORGED-QUESTION",
        }).encode()
        status, payload, hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 404)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertIn(b"unknown question id", payload)
        self.assertNotIn(b"FORGED-CASE", payload)
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(QUESTION.encode(), payload)
        self.assertEqual(self.ledger(), [])
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        stored = self.questions()
        self.assertEqual(stored[0]["status"], "open")
        self.assertEqual(stored[1]["status"], "open")
        self.assertNotIn("answer_summary", stored[0])

        status, payload, hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN,
            body=json.dumps({"id": "q1", "answer": "yes"}).encode())
        self.assertEqual(status, 200)
        self.assertIsNone(hdrs.get("connection"))
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "q1")
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertEqual(rows[0]["question"], QUESTION)
        self.assertNotIn("FORGED-CASE", json.dumps(rows[0]))
        self.assertEqual(self.questions()[0]["status"], "answered")
        self.assertEqual(self.questions()[1]["status"], "open")
        log = self.log()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("FORGED-CASE", log)

    def test_options_does_not_record_an_answer(self):
        # Browsers send OPTIONS before a cross-origin POST. That preflight
        # must not append a ledger row or mark the question answered.
        self.seed()
        status, payload, hdrs = self.request(
            "OPTIONS", "/api/answer?key=" + TOKEN,
            body=json.dumps({"id": "q1", "answer": "FORGED-OPTIONS"}).encode(),
            headers=(
                ("Content-Length", "0"),
                ("Content-Type", "application/json"),
            ))
        self.assertEqual(status, 501)
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(QUESTION.encode(), payload)
        self.assertNotIn(b"FORGED-OPTIONS", payload)
        self.assertEqual(self.ledger(), [])
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        stored = self.questions()
        self.assertEqual(stored[0]["status"], "open")
        self.assertEqual(stored[1]["status"], "open")
        log = self.log()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("FORGED-OPTIONS", log)

        status, payload, _hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN,
            body=json.dumps({"id": "q1", "answer": "yes"}).encode())
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.ledger()[0]["answer"], "yes")
        self.assertEqual(self.questions()[0]["status"], "answered")


if __name__ == "__main__":
    unittest.main()
