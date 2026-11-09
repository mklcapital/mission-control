"""POST /health is not liveness, and an answer note stays out of the log.

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
SENTINEL = "HEALTH-SENTINEL-QUESTION"
NOTE = "NOTE-SECRET"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class PostHealthAndNoteTests(unittest.TestCase):
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
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([
                {
                    "id": "q1",
                    "question": SENTINEL,
                    "project": "LOAN",
                    "status": "open",
                },
            ], fh)

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

    def test_post_health_does_not_answer_and_the_note_stays_out_of_the_log(self):
        # GET /health is open. POST /health is not, with or without the key,
        # and it must not apply an answer body.
        body = json.dumps({
            "id": "q1",
            "answer": "Approved",
            "note": NOTE,
            "project": "FORGED",
            "question": "FORGED",
        }).encode()

        status, payload, hdrs = self.exchange("POST", "/health", body)
        self.assertEqual(status, 403)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), payload)
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(NOTE.encode(), payload)

        status, payload, hdrs = self.exchange(
            "POST", "/health?key=" + TOKEN, body)
        self.assertEqual(status, 404, payload)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertIn(b"not found", payload)
        self.assertNotIn(SENTINEL.encode(), payload)
        self.assertNotIn(NOTE.encode(), payload)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            queue = json.load(fh)
        self.assertEqual(queue[0]["status"], "open")
        self.assertNotIn("answer_summary", queue[0])

        status, payload, hdrs = self.exchange("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), payload)
        self.assertNotIn(TOKEN.encode(), payload)

        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body)
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(NOTE.encode(), payload)

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            ledger = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["answer"], "Approved")
        self.assertEqual(ledger[0]["note"], NOTE)
        self.assertEqual(ledger[0]["question"], SENTINEL)
        self.assertEqual(ledger[0]["project"], "LOAN")
        self.assertNotIn(TOKEN, json.dumps(ledger))

        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            queue = json.load(fh)
        self.assertEqual(queue[0]["status"], "answered")
        self.assertEqual(queue[0]["answer_summary"], "Approved [note: %s]" % NOTE)
        self.assertEqual(queue[0]["question"], SENTINEL)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("ANSWER q1 = Approved", log)
        self.assertNotIn(NOTE, log)
        self.assertNotIn("[note:", log)
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)


if __name__ == "__main__":
    unittest.main()
