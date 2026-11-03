"""A POST body that is not UTF-8 is rejected before the queue is touched.

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
SENTINEL = "SECRET_QUESTION_DO_NOT_LEAK"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


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


class Utf8BodyTests(unittest.TestCase):
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
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{
                "id": "q1",
                "question": SENTINEL,
                "project": "LOAN",
                "status": "open",
            }], fh)

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body, headers)

    def authed(self, path):
        sep = "&" if "?" in path else "?"
        return "%s%skey=%s" % (path, sep, quote(TOKEN))

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def log_text(self):
        if not os.path.exists(server.LOG_FILE):
            return ""
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_invalid_utf8_does_not_answer_and_a_later_post_still_works(self):
        body = b'{"id":"q1","answer":"\xff"}'
        status, payload, hdrs = self.request(
            "POST", self.authed("/api/answer"), body=body)
        self.assertEqual(status, 400)
        self.assertIn(b"bad json", payload)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
        self.assertNotIn(SENTINEL.encode(), payload)
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertNotIn("answer_summary", self.questions()[0])
        log = self.log_text()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(SENTINEL, log)

        status, payload, hdrs = self.request(
            "GET", self.authed("/api/questions"))
        self.assertEqual(status, 200)
        self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
        self.assertNotIn("content-security-policy", hdrs)
        self.assertIn(SENTINEL, payload.decode())

        status, payload, hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": "q1", "answer": "yes"}).encode())
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertEqual(self.questions()[0]["status"], "answered")
        self.assertEqual(self.questions()[0]["answer_summary"], "yes")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["answer"], "yes")
        self.assertNotIn(TOKEN, lines[0])
