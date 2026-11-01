"""The access key is the `key` query parameter, not a prefix of it.

A semicolon is not a separator, and `Key` is a different parameter.
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
SENTINEL = "SENTINEL-QUESTION"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def http_request(host, port, method, path):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        payload = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, payload, hdrs
    finally:
        conn.close()


class QueryKeySeparatorTests(unittest.TestCase):
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

    def request(self, path):
        return http_request(self.host, self.port, "GET", path)

    def log_text(self):
        if not os.path.exists(server.LOG_FILE):
            return ""
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_semicolon_and_capital_key_do_not_authorize(self):
        # `key=<token>;admin=1` must stay one value. Splitting on `;` would
        # accept the token and treat `admin` as a second parameter.
        semi = "/api/questions?key=%s;admin=1" % quote(TOKEN, safe="")
        status, body, hdrs = self.request(semi)
        self.assertEqual(status, 403, semi)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertIn(b"bad or missing key", body)

        status, body, hdrs = self.request(
            "/api/questions?key=%s&admin=1" % quote(TOKEN, safe=""))
        self.assertEqual(status, 200)
        self.assertIsNone(hdrs.get("connection"))
        self.assertTrue(hdrs.get("server", "").startswith("MissionControl/1.0"))
        self.assertEqual(json.loads(body)[0]["question"], SENTINEL)
        self.assertNotIn(TOKEN.encode(), body)

        log = self.log_text()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)

        # `Key` is a different parameter. Checked after the log assertion so a
        # case-sensitive redaction miss cannot hide the semicolon result.
        capital = "/api/questions?Key=%s" % quote(TOKEN, safe="")
        status, body, hdrs = self.request(capital)
        self.assertEqual(status, 403, capital)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(self.questions_status(), "open")

    def questions_status(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)[0]["status"]


if __name__ == "__main__":
    unittest.main()
