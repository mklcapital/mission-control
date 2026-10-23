"""Auth is the query key only, and only POST /api/answer mutates the queue.

Stdlib only. The server binds localhost and writes only inside a temp dir.
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


class AuthMethodBoundaryTests(unittest.TestCase):
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

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body=body, headers=headers)

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def log_text(self):
        if not os.path.exists(server.LOG_FILE):
            return ""
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_a_header_token_does_not_authorize(self):
        for headers in (
            {"Authorization": "Bearer " + TOKEN},
            {"X-Api-Key": TOKEN},
        ):
            status, body, hdrs = self.request("GET", "/api/questions", headers=headers)
            self.assertEqual(status, 403, headers)
            self.assertEqual(json.loads(body)["error"], "bad or missing key")
            self.assertEqual(hdrs.get("connection"), "close")
            self.assertNotIn(QUESTION.encode(), body)
            self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertNotIn(TOKEN, self.log_text())
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))

        status, body, _hdrs = self.request(
            "GET", "/api/questions?key=" + TOKEN)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)[0]["question"], QUESTION)
        self.assertNotIn(TOKEN, self.log_text())

    def test_put_and_delete_do_not_record_an_answer(self):
        payload = json.dumps({
            "id": "q1",
            "answer": "yes",
            "other_text": "forged",
            "note": "forged",
        }).encode()
        for method in ("PUT", "DELETE"):
            status, body, _hdrs = self.request(
                method,
                "/api/answer?key=" + TOKEN,
                body=payload,
                headers={"Content-Type": "application/json"},
            )
            self.assertGreaterEqual(status, 400, method)
            self.assertNotEqual(status, 200, method)
            self.assertNotIn(QUESTION.encode(), body)
            self.assertNotIn(TOKEN.encode(), body)
            self.assertNotIn(b"forged", body)
        self.assertEqual(self.questions()[0]["status"], "open")
        self.assertNotIn("answer_summary", self.questions()[0])
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        log = self.log_text()
        self.assertNotIn(TOKEN, log)
        self.assertIn("key=REDACTED", log)

        status, body, _hdrs = self.request(
            "POST",
            "/api/answer?key=" + TOKEN,
            body=json.dumps({"id": "q1", "answer": "yes"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(self.questions()[0]["status"], "answered")
        self.assertEqual(self.questions()[0]["answer_summary"], "yes")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertNotIn(TOKEN, lines[0])
        self.assertNotIn(TOKEN, self.log_text())


if __name__ == "__main__":
    unittest.main()
