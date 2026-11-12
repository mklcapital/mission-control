"""An ampersand in the access key is a separator until it is percent-encoded.

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


TOKEN = "unit&test-key"
SENTINEL = "AMP-SENTINEL-QUESTION"
NOTE = "NOTE-SECRET"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class AmpersandQueryKeyTests(unittest.TestCase):
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
        with open(server.INDEX_FILE, "w", encoding="utf-8") as fh:
            fh.write("<html>queue</html>\n")
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{
                "id": "q1",
                "question": SENTINEL,
                "project": "LOAN",
                "status": "open",
            }], fh)

    def request(self, method, path, body=None):
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

    def test_an_ampersand_in_the_key_stays_a_separator_until_encoded(self):
        # Split first, then decode. A raw '&' ends the key; a later parameter
        # that holds the real token must not authorize. '%26' is one key.
        split = "/api/questions?key=unit&test-key=" + "unit%26test-key"
        status, body, hdrs = self.request("GET", split)
        self.assertEqual(status, 403, body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        status, body, hdrs = self.request("GET", "/api/questions?key=unit%2526test-key")
        self.assertEqual(status, 403, body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)

        status, body, hdrs = self.request(
            "GET", "/api/questions?key=unit%26test-key&key=wrong-secret")
        self.assertEqual(status, 200, body)
        self.assertNotIn("connection", hdrs)
        self.assertIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(b"wrong-secret", body)

        answer = json.dumps({"id": "q1", "answer": "yes", "note": NOTE}).encode()
        status, body, hdrs = self.request(
            "POST", "/api/answer?key=unit&test-key=yes", answer)
        self.assertEqual(status, 403, body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(NOTE.encode(), body)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        self.assertEqual(self.queue()[0]["status"], "open")

        status, body, hdrs = self.request(
            "POST", "/api/answer?key=unit%26test-key", answer)
        self.assertEqual(status, 200, body)
        self.assertNotIn("connection", hdrs)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(self.queue()[0]["status"], "answered")
        self.assertEqual(self.queue()[0]["answer_summary"], "yes [note: %s]" % NOTE)
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["answer"], "yes")
        self.assertEqual(record["note"], NOTE)
        self.assertEqual(record["question"], SENTINEL)
        self.assertNotIn(TOKEN, lines[0])

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("unit%26test-key", log)
        self.assertNotIn("wrong-secret", log)
