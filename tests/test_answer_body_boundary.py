"""An answer body cannot smuggle a second write or extra secret fields.

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
PASSWORD = "hunter2-secret"
BODY_KEY = "access-key-secret"
BODY_TOKEN = "body-token-secret"


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


class AnswerBodyBoundaryTests(unittest.TestCase):
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
            json.dump([
                {"id": "q1", "question": "Ship it?", "project": "LOAN", "status": "open"},
                {"id": "q2", "question": "Leave q2 open?", "project": "LOAN", "status": "open"},
            ], fh)

    def post(self, body):
        return http_request(
            self.host, self.port, "POST", "/api/answer?key=" + TOKEN, body=body)

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def answer_text(self):
        if not os.path.exists(server.ANSWERS_FILE):
            return ""
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_a_second_json_value_and_extra_fields_do_not_widen_the_ledger(self):
        smuggled = (
            b'{"id":"q1","answer":"yes"}'
            b'{"id":"q2","answer":"no","password":"' + PASSWORD.encode() + b'"}'
        )
        status, body, hdrs = self.post(smuggled)
        self.assertEqual(status, 400)
        self.assertIn(b"bad json", body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(PASSWORD.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(self.answer_text(), "")
        saved = self.questions()
        self.assertEqual(saved[0]["status"], "open")
        self.assertEqual(saved[1]["status"], "open")
        self.assertNotIn("answer_summary", saved[0])

        payload = {
            "id": "q1",
            "answer": "Approved",
            "password": PASSWORD,
            "key": BODY_KEY,
            "token": BODY_TOKEN,
            "project": "EVIL",
            "question": "hijacked",
        }
        status, body, hdrs = self.post(json.dumps(payload).encode())
        self.assertEqual(status, 200, body)
        self.assertNotIn("connection", hdrs)
        self.assertNotIn(PASSWORD.encode(), body)
        self.assertNotIn(BODY_KEY.encode(), body)
        self.assertNotIn(BODY_TOKEN.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        ledger = self.answer_text()
        self.assertEqual(ledger.count("\n"), 1, ledger)
        record = json.loads(ledger)
        self.assertEqual(record["id"], "q1")
        self.assertEqual(record["answer"], "Approved")
        self.assertEqual(record["project"], "LOAN")
        self.assertEqual(record["question"], "Ship it?")
        self.assertEqual(set(record), {
            "ts", "id", "project", "question", "answer", "other_text", "note",
        })
        for secret in (PASSWORD, BODY_KEY, BODY_TOKEN, TOKEN, "EVIL", "hijacked"):
            self.assertNotIn(secret, ledger)

        saved = self.questions()
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "Approved")
        self.assertEqual(saved[1]["status"], "open")
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            raw_questions = fh.read()
        for secret in (PASSWORD, BODY_KEY, BODY_TOKEN, TOKEN):
            self.assertNotIn(secret, raw_questions)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        for secret in (PASSWORD, BODY_KEY, BODY_TOKEN, TOKEN):
            self.assertNotIn(secret, log)
        self.assertIn("key=REDACTED", log)
        answer_logs = [ln for ln in log.splitlines() if "ANSWER " in ln]
        self.assertEqual(len(answer_logs), 1, log)
        self.assertTrue(answer_logs[0].endswith(" ANSWER q1 = Approved"), answer_logs[0])
