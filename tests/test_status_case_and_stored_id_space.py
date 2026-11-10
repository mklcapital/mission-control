"""Status is case-sensitive, and a stored id is not stripped before matching.

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
SENTINEL = "STATUS-SENTINEL-QUESTION"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class StatusCaseAndStoredIdTests(unittest.TestCase):
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

    def post_answer(self, qid):
        body = json.dumps({
            "id": qid,
            "answer": "yes",
            "question": "FORGED-QUESTION",
            "project": "FORGED",
        }).encode()
        return self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)

    def test_answered_status_is_case_sensitive(self):
        # "Answered" is not the closed state. One submit records the stored
        # question, and a second submit is the conflict.
        self.seed([{
            "id": "q1",
            "question": SENTINEL,
            "project": "LOAN",
            "status": "Answered",
        }])
        status, payload, hdrs = self.post_answer("q1")
        self.assertEqual(status, 200, payload)
        self.assertNotIn("connection", hdrs)
        saved = self.questions()
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["question"], SENTINEL)
        self.assertEqual(saved[0]["answer_summary"], "yes")
        self.assertNotIn("FORGED", json.dumps(saved))
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question"], SENTINEL)
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertNotIn(TOKEN, json.dumps(rows))

        status, payload, hdrs = self.post_answer("q1")
        self.assertEqual(status, 409, payload)
        self.assertEqual(hdrs.get("connection"), "close")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(sum(1 for line in fh if line.strip()), 1)
        self.assertEqual(self.questions()[0]["answer_summary"], "yes")
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)

    def test_stored_id_space_is_not_stripped(self):
        # The payload id is stripped. The stored id is not, so neither the
        # trimmed form nor the exact padded form answers "q1 ".
        self.seed([
            {
                "id": "q1 ",
                "question": SENTINEL,
                "project": "LOAN",
                "status": "open",
            },
            {
                "id": "q2",
                "question": "sibling",
                "project": "LOAN",
                "status": "open",
            },
        ])
        for qid in ("q1", " q1 ", "q1 "):
            status, payload, hdrs = self.post_answer(qid)
            self.assertEqual(status, 404, (qid, payload))
            self.assertIn(b"unknown question id", payload)
            self.assertEqual(hdrs.get("connection"), "close")
            self.assertNotIn(SENTINEL.encode(), payload)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        saved = self.questions()
        self.assertEqual([q["id"] for q in saved], ["q1 ", "q2"])
        self.assertEqual([q["status"] for q in saved], ["open", "open"])
        self.assertNotIn("answer_summary", saved[0])
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(SENTINEL, log)

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)


if __name__ == "__main__":
    unittest.main()
