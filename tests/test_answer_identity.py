"""Answer identity: falsy-looking text counts, and ids do not prefix-match.

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


class AnswerIdentityTests(unittest.TestCase):
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

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body, headers)

    def authed(self, path):
        return "%s?key=%s" % (path, quote(TOKEN))

    def seed(self, questions):
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(questions, fh)

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def answer_lines(self):
        if not os.path.exists(server.ANSWERS_FILE):
            return []
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln]

    def test_string_zero_is_a_real_answer(self):
        self.seed([{
            "id": "q1",
            "status": "open",
            "project": "LOAN",
            "question": SENTINEL,
        }])
        status, body, hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=json.dumps({
                "id": "q1",
                "answer": "0",
                "other_text": "0",
                "note": "0",
            }).encode())
        self.assertEqual(status, 200, body)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        record = json.loads(self.answer_lines()[0])
        self.assertEqual(record["answer"], "0")
        self.assertEqual(record["other_text"], "0")
        self.assertEqual(record["note"], "0")
        self.assertEqual(record["question"], SENTINEL)
        saved = self.questions()[0]
        self.assertEqual(saved["status"], "answered")
        self.assertEqual(saved["answer_summary"], "0 — 0 [note: 0]")

        status, body, hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": "q1", "answer": "1"}).encode())
        self.assertEqual(status, 409, body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertEqual(len(self.answer_lines()), 1)
        self.assertEqual(self.questions()[0]["answer_summary"], "0 — 0 [note: 0]")

    def test_a_prefix_of_the_id_does_not_answer(self):
        self.seed([
            {"id": "q1", "status": "open", "project": "LOAN", "question": SENTINEL},
            {"id": "q10", "status": "open", "project": "LOAN", "question": "other"},
        ])
        for qid in ("q", "q1x", "q10x"):
            status, body, hdrs = self.request(
                "POST", self.authed("/api/answer"),
                body=json.dumps({"id": qid, "answer": "yes"}).encode())
            self.assertEqual(status, 404, qid)
            self.assertEqual(hdrs.get("connection"), "close")
            self.assertIn(b"unknown question id", body)
            self.assertNotIn(SENTINEL.encode(), body)
        self.assertEqual(self.answer_lines(), [])
        self.assertEqual(
            [q["status"] for q in self.questions()],
            ["open", "open"],
        )

        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": " q1 ", "answer": "yes"}).encode())
        self.assertEqual(status, 200, body)
        saved = self.questions()
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[1]["status"], "open")
        self.assertEqual(saved[1]["id"], "q10")
        record = json.loads(self.answer_lines()[0])
        self.assertEqual(record["id"], "q1")
        self.assertEqual(record["question"], SENTINEL)

    def test_bad_json_closes_the_connection_and_does_not_answer(self):
        self.seed([{"id": "q1", "status": "open", "question": SENTINEL, "project": "LOAN"}])
        status, body, hdrs = self.request(
            "POST", self.authed("/api/answer"), body=b"{")
        self.assertEqual(status, 400)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertIn(b"bad json", body)
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(self.answer_lines(), [])
        self.assertEqual(self.questions()[0]["status"], "open")


if __name__ == "__main__":
    unittest.main()
