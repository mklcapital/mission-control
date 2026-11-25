"""A percent-encoded '?' or '#' is part of the path, not a query or fragment.

Decoding the path before routing would turn `/api/questions%3Fkey=<token>`
into an authorized request, or turn `/health%23/...` into the open health
check. A literal '#' that a client sends is only a fragment.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server


class EncodedPathDoesNotAuthorizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        server.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        server.LOG_FILE = os.path.join(self.tmp.name, "server.log")
        server.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")
        server.INDEX_FILE = os.path.join(self.tmp.name, "index.html")
        self.sentinel = "SENTINEL-QUESTION-TEXT"
        queue = [{
            "id": "q1",
            "project": "LOAN",
            "question": self.sentinel,
            "status": "open",
            "options": ["yes", "no"],
        }]
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(queue, fh)
        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.original_queue = fh.read()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.token = server.TOKEN

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        last = None
        for _ in range(50):
            conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                headers = {}
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                payload = resp.read()
                status = resp.status
                header_map = {k.lower(): v for k, v in resp.getheaders()}
                conn.close()
                return status, payload, header_map
            except (ConnectionRefusedError, ConnectionResetError) as exc:
                last = exc
                conn.close()
                time.sleep(0.02)
        raise AssertionError("server did not accept a connection: %r" % (last,))

    def test_encoded_query_and_hash_do_not_change_the_route(self):
        key = quote(self.token, safe="")
        encoded = "/api/questions%3Fkey=" + key
        status, body, headers = self.request("GET", encoded)
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("connection"), "close")
        text = body.decode("utf-8")
        self.assertNotIn(self.sentinel, text)
        self.assertNotIn(self.token, text)
        self.assertIn("bad or missing key", text)

        status, body, headers = self.request("POST", encoded, b'{"id":"q1","answer":"yes"}')
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("connection"), "close")
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.assertEqual(fh.read(), self.original_queue)

        cases = (
            ("/health%23/api/questions", 403),
            ("/health%23/api/questions?key=" + key, 404),
            ("/api/questions%23/health?key=" + key, 404),
        )
        for path, expect in cases:
            status, body, headers = self.request("GET", path)
            self.assertEqual(status, expect, path)
            self.assertEqual(headers.get("connection"), "close", path)
            text = body.decode("utf-8")
            self.assertNotIn(self.sentinel, text, path)
            self.assertNotIn(self.token, text, path)
            self.assertNotIn('"ok"', text, path)

        status, body, headers = self.request("GET", "/health#/api/questions")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("connection"))
        health = json.loads(body.decode("utf-8"))
        self.assertEqual(set(health), {"ok", "ts"})
        self.assertIs(health["ok"], True)
        self.assertNotIn(self.sentinel, body.decode("utf-8"))
        self.assertNotIn(self.token, body.decode("utf-8"))

        status, body, headers = self.request("GET", "/api/questions?key=" + key + "#/health")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("connection"))
        queued = json.loads(body.decode("utf-8"))
        self.assertEqual(queued[0]["question"], self.sentinel)
        self.assertEqual(queued[0]["status"], "open")
        self.assertNotIn(self.token, body.decode("utf-8"))

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(self.token, log)
        self.assertNotIn(self.sentinel, log)


if __name__ == "__main__":
    unittest.main()
