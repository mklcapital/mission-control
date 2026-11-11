"""A question is answered from the JSON body, not the query string.

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
FORGED = "FORGED-URL-ANSWER"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class QueryStringAnswerTests(unittest.TestCase):
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
                {"id": "q1", "question": "Ship q1?", "project": "LOAN", "status": "open"},
                {"id": "q2", "question": "Ship q2?", "project": "FAB", "status": "open"},
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

    def queue(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_query_id_and_answer_do_not_override_the_body(self):
        # The URL can carry the access key. It must not also be an answer channel:
        # a query id/answer does not satisfy a missing body, and does not retarget
        # the question the JSON body names.
        status, payload, hdrs = self.exchange(
            "POST",
            "/api/answer?key=%s&id=q1&answer=%s" % (TOKEN, FORGED),
            b"{}",
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertIn(b"id and answer required", payload)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        self.assertEqual([q["status"] for q in self.queue()], ["open", "open"])

        body = json.dumps({"id": "q2", "answer": "yes"}).encode()
        status, payload, hdrs = self.exchange(
            "POST",
            "/api/answer?key=%s&id=q1&answer=%s" % (TOKEN, FORGED),
            body,
        )
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(FORGED.encode(), payload)

        queue = self.queue()
        self.assertEqual(queue[0]["status"], "open")
        self.assertNotIn("answer_summary", queue[0])
        self.assertEqual(queue[1]["status"], "answered")
        self.assertEqual(queue[1]["answer_summary"], "yes")
        self.assertEqual(queue[1]["question"], "Ship q2?")
        self.assertEqual(queue[1]["project"], "FAB")
        self.assertNotIn(FORGED, json.dumps(queue))

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["id"], "q2")
        self.assertEqual(record["answer"], "yes")
        self.assertEqual(record["question"], "Ship q2?")
        self.assertEqual(record["project"], "FAB")
        self.assertNotIn(FORGED, lines[0])
        self.assertNotIn(TOKEN, lines[0])

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
