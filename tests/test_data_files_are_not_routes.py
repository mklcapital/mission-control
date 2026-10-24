"""Queue files are not HTTP routes.

An authenticated GET of the ledger, the token file, or the log must not
return their contents. Stdlib only. The server binds localhost and writes
only inside a temp dir.
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
QUESTION = "QUESTION_MARKER_DO_NOT_LEAK"
LEDGER = "ANSWER_LEDGER_MUST_NOT_LEAK"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


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


class DataFileRouteTests(unittest.TestCase):
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
                "question": QUESTION,
                "project": "LOAN",
                "status": "open",
            }], fh)
        with open(server.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "id": "q1",
                "answer": LEDGER,
                "ts": "2026-01-01T00:00:01",
            }) + "\n")
        with open(os.path.join(self.dir, "token.txt"), "w", encoding="utf-8") as fh:
            fh.write(TOKEN + "\n")

    def request(self, path):
        return http_request(self.host, self.port, "GET", path)

    def assert_hidden(self, status, body, hdrs, path):
        self.assertIn(status, (403, 404), path)
        self.assertEqual(hdrs.get("connection"), "close", path)
        self.assertNotIn(LEDGER.encode(), body, path)
        self.assertNotIn(QUESTION.encode(), body, path)
        self.assertNotIn(TOKEN.encode(), body, path)
        self.assertNotIn(b"queue</html>", body, path)

    def test_ledger_token_and_log_are_not_served(self):
        paths = (
            "/token.txt",
            "/answers.jsonl",
            "/server.log",
            "/questions.json",
            "/api/answer",
            "/api/answers",
            "/../token.txt",
            "/%2e%2e/token.txt",
            "/index.html/../token.txt",
        )
        for path in paths:
            status, body, hdrs = self.request(path)
            self.assertEqual(status, 403, path)
            self.assert_hidden(status, body, hdrs, path)

            authed = path + "?key=" + TOKEN
            status, body, hdrs = self.request(authed)
            self.assertEqual(status, 404, authed)
            self.assert_hidden(status, body, hdrs, authed)

        status, body, hdrs = self.request("/api/questions?key=" + TOKEN)
        self.assertEqual(status, 200)
        self.assertNotIn("connection", hdrs)
        self.assertIn(QUESTION.encode(), body)
        self.assertNotIn(LEDGER.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertIn(LEDGER, fh.read())
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[0]["status"], "open")
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn(TOKEN, log)
        self.assertIn("key=REDACTED", log)


if __name__ == "__main__":
    unittest.main()
