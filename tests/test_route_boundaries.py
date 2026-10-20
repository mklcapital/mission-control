"""Route prefixes must not inherit a real route, and answers must not store the key.

Stdlib only. The server binds localhost and writes only inside a temp dir.
"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote

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


class RouteBoundaryTests(unittest.TestCase):
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
            fh.write("<html>%s</html>\n" % SENTINEL)
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{
                "id": "q1",
                "question": SENTINEL,
                "project": "LOAN",
                "status": "open",
            }], fh)

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body=body, headers=headers)

    def authed(self, path):
        return "%s?key=%s" % (path, quote(TOKEN))

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_health_or_queue_prefix_does_not_inherit_the_route(self):
        status, body, hdrs = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIn(b'"ok": true', body)
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotEqual(hdrs.get("connection"), "close")

        for path in ("/health/", "/health/extra", "/Health"):
            status, body, hdrs = self.request("GET", path)
            self.assertNotEqual(status, 200, path)
            self.assertNotIn(b'"ok"', body, path)
            self.assertNotIn(SENTINEL.encode(), body, path)
            self.assertNotIn(TOKEN.encode(), body, path)
            self.assertEqual(hdrs.get("connection"), "close", path)

        for path in ("/api/questions/", "/api/questions/extra", "/api/question"):
            status, body, hdrs = self.request("GET", self.authed(path))
            self.assertEqual(status, 404, path)
            self.assertNotIn(SENTINEL.encode(), body, path)
            self.assertNotIn(TOKEN.encode(), body, path)
            self.assertEqual(hdrs.get("connection"), "close", path)
            self.assertIn(b"not found", body, path)

        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))

    def test_a_negative_length_does_not_answer(self):
        status, body, hdrs = self.request(
            "POST",
            self.authed("/api/answer"),
            body=json.dumps({"id": "q1", "answer": "yes"}).encode(),
            headers={"Content-Length": "-1"},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"bad content-length", body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))

    def test_a_recorded_answer_does_not_store_the_key(self):
        status, body, hdrs = self.request(
            "POST",
            self.authed("/api/answer"),
            body=json.dumps({
                "id": "q1",
                "answer": "Approved",
                "other_text": "",
                "note": "",
            }).encode(),
        )
        self.assertEqual(status, 200, body)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(SENTINEL.encode(), body)

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            ledger = fh.read()
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            queue = fh.read()
        self.assertIn("Approved", ledger)
        self.assertIn(SENTINEL, queue)
        self.assertNotIn(TOKEN, ledger)
        self.assertNotIn(TOKEN, queue)
        self.assertEqual(self.questions()[0]["status"], "answered")


if __name__ == "__main__":
    unittest.main()
