"""An empty token does not authorize, and duplicate JSON keys keep the last value.

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
SENTINEL = "QUEUE-SENTINEL-do-not-leak"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def http_request(host, port, method, path, body=None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body)
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

    def request(self, method, path, body=None):
        return http_request(self.host, self.port, method, path, body)

    def seed(self, questions):
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(questions, fh)

    def test_an_empty_token_does_not_authorize_an_empty_key(self):
        # compare_digest("", "") is true. The bool(supplied) guard is what
        # keeps a blank token from opening the queue.
        server.TOKEN = ""
        self.seed([{
            "id": "q1",
            "question": SENTINEL,
            "project": "LOAN",
            "status": "open",
        }])

        for path in ("/api/questions?key=", "/api/questions", "/api/questions?key=" + TOKEN):
            status, body, hdrs = self.request("GET", path)
            self.assertEqual(status, 403, path)
            self.assertEqual(hdrs.get("connection"), "close", path)
            self.assertNotIn(SENTINEL.encode(), body, path)
            self.assertNotIn(b"LOAN", body, path)

        status, body, hdrs = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertIsNone(hdrs.get("connection"))

        status, body, hdrs = self.request(
            "POST", "/api/answer?key=",
            body=json.dumps({"id": "q1", "answer": "Approved"}).encode())
        self.assertEqual(status, 403)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[0]["status"], "open")
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn(SENTINEL, log)
        self.assertNotIn("Approved", log)

    def test_duplicate_json_keys_answer_the_last_question(self):
        self.seed([
            {"id": "q1", "question": "first question", "project": "LOAN", "status": "open"},
            {"id": "q2", "question": "second question", "project": "INFRA", "status": "open"},
        ])
        # json.dumps would collapse the duplicates. The last id and answer win.
        body = b'{"id":"q1","answer":"no","id":"q2","answer":"yes"}'
        status, _payload, _hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 200)

        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "open")
        self.assertNotIn("answer_summary", saved[0])
        self.assertEqual(saved[1]["status"], "answered")
        self.assertEqual(saved[1]["answer_summary"], "yes")
        self.assertEqual(saved[1]["question"], "second question")

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["id"], "q2")
        self.assertEqual(record["answer"], "yes")
        self.assertEqual(record["question"], "second question")
        self.assertEqual(record["project"], "INFRA")
        self.assertNotIn("first question", lines[0])
        self.assertNotIn(TOKEN, lines[0])


if __name__ == "__main__":
    unittest.main()
