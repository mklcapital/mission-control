"""A decision is recorded even when server.log cannot be opened.

Stdlib only. The server binds localhost and every data file is in a temp dir.
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
SENTINEL = "LOG-SENTINEL-QUESTION"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


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


class UnwritableLogTests(unittest.TestCase):
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
        os.mkdir(server.LOG_FILE)
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{
                "id": "q1",
                "question": SENTINEL,
                "project": "LOAN",
                "status": "open",
            }], fh)

    def request(self, method, path, body=None):
        return http_request(self.host, self.port, method, path, body)

    def answer_lines(self):
        if not os.path.exists(server.ANSWERS_FILE):
            return []
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln]

    def test_health_and_answer_succeed_when_the_log_is_a_directory(self):
        status, body, hdrs = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], True)
        self.assertIsNone(hdrs.get("connection"))
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        payload = json.dumps({
            "id": "q1",
            "answer": "Approved",
            "question": "forged question",
            "project": "forged",
        }).encode()
        status, body, hdrs = self.request(
            "POST", "/api/answer?key=%s" % quote(TOKEN), body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], True)
        self.assertIsNone(hdrs.get("connection"))
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(b"forged", body)

        self.assertTrue(os.path.isdir(server.LOG_FILE))
        self.assertEqual(os.listdir(server.LOG_FILE), [])
        lines = self.answer_lines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["id"], "q1")
        self.assertEqual(record["answer"], "Approved")
        self.assertEqual(record["question"], SENTINEL)
        self.assertEqual(record["project"], "LOAN")
        self.assertNotIn(TOKEN, lines[0])

        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "Approved")
        self.assertEqual(saved[0]["question"], SENTINEL)

        status, body, hdrs = self.request(
            "POST", "/api/answer?key=%s" % quote(TOKEN), body=payload)
        self.assertEqual(status, 409)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertEqual(len(self.answer_lines()), 1)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertTrue(os.path.isdir(server.LOG_FILE))


if __name__ == "__main__":
    unittest.main()
