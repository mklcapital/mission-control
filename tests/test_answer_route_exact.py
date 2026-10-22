"""The answer route is exact. A longer path must not record an answer.

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


class AnswerRouteTests(unittest.TestCase):
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

    def request(self, method, path, body=None):
        return http_request(self.host, self.port, method, path, body=body)

    def authed(self, path):
        return "%s?key=%s" % (path, quote(TOKEN))

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_longer_answer_path_does_not_record(self):
        body_for = lambda answer: json.dumps({"id": "q1", "answer": answer}).encode()
        for path in ("/api/answer/", "/api/answer/extra", "/api/answerextra"):
            status, body, hdrs = self.request(
                "POST", self.authed(path), body=body_for("injected-via-" + path))
            self.assertEqual(status, 404, path)
            self.assertEqual(hdrs.get("connection"), "close", path)
            self.assertNotIn(QUESTION.encode(), body)
            self.assertNotIn(TOKEN.encode(), body)
            self.assertNotIn(b"injected-via-", body)
            self.assertFalse(os.path.exists(server.ANSWERS_FILE), path)
            self.assertEqual(self.questions()[0]["status"], "open", path)

        status, body, hdrs = self.request(
            "POST", self.authed("/api/answer"), body=body_for("the real answer"))
        self.assertEqual(status, 200, body)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(QUESTION.encode(), body)

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["id"], "q1")
        self.assertEqual(record["answer"], "the real answer")
        self.assertNotIn("injected-via-", lines[0])
        self.assertEqual(self.questions()[0]["status"], "answered")
        self.assertEqual(self.questions()[0]["answer_summary"], "the real answer")
