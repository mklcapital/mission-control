"""A failed queue replace must not report success or mark the question answered.

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


TOKEN = "unit-test-key"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class QueueReplaceFailureTests(unittest.TestCase):
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
        self.queue = [{
            "id": "q1",
            "status": "open",
            "project": "LOAN",
            "question": "Ship it?",
            "options": ["yes", "no"],
        }, {
            "id": "q2",
            "status": "open",
            "project": "FAB",
            "question": "Sibling stays open",
        }]
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.queue, fh)
            fh.write("\n")
        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.queue_bytes = fh.read()

    def exchange(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            payload = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, payload, hdrs
        finally:
            conn.close()

    def post_answer(self, answer):
        body = json.dumps({
            "id": "q1",
            "answer": answer,
            "project": "FORGED-PROJECT",
            "question": "FORGED-QUESTION",
        }).encode()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(
                "POST", "/api/answer?key=" + TOKEN, body=body,
                headers={"Content-Type": "application/json"})
            try:
                resp = conn.getresponse()
            except (ConnectionResetError, http.client.HTTPException):
                return None, b"", {}
            payload = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, payload, hdrs
        finally:
            conn.close()

    def test_failed_replace_leaves_the_question_open(self):
        calls = {"n": 0}
        real_replace = server.os.replace

        def replace_once(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_replace(src, dst)

        server.os.replace = replace_once
        self.addCleanup(setattr, server.os, "replace", real_replace)
        try:
            status, payload, hdrs = self.post_answer("yes")
        finally:
            server.os.replace = real_replace

        self.assertEqual(calls["n"], 1)
        if status is not None:
            self.assertNotEqual(status, 200, payload)
            self.assertNotIn(b'"ok"', payload)
            self.assertNotIn(b"Ship it?", payload)
            self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotEqual(hdrs.get("connection"), "keep-alive")

        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.assertEqual(fh.read(), self.queue_bytes)
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "open")
        self.assertNotIn("answer_summary", saved[0])
        self.assertEqual(saved[1]["id"], "q2")
        self.assertEqual(saved[1]["status"], "open")
        self.assertEqual(saved[1]["question"], "Sibling stays open")

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "q1")
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertEqual(rows[0]["question"], "Ship it?")
        self.assertNotIn("FORGED-PROJECT", raw)
        self.assertNotIn("FORGED-QUESTION", raw)
        self.assertNotIn(TOKEN, raw)

        status, payload, hdrs = self.exchange("GET", "/health")
        self.assertEqual(status, 200, payload)
        self.assertNotIn("connection", hdrs)
        self.assertNotIn(b"Ship it?", payload)
        self.assertNotIn(TOKEN.encode(), payload)

        status, payload, hdrs = self.post_answer("Approved")
        self.assertEqual(status, 200, payload)
        self.assertNotIn("connection", hdrs)
        self.assertNotIn(TOKEN.encode(), payload)
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "Approved")
        self.assertEqual(saved[0]["options"], ["yes", "no"])
        self.assertEqual(saved[1]["status"], "open")
        self.assertEqual(saved[1]["question"], "Sibling stays open")
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        self.assertEqual([r["answer"] for r in rows], ["yes", "Approved"])
        self.assertEqual(rows[1]["project"], "LOAN")
        self.assertEqual(rows[1]["question"], "Ship it?")
        self.assertNotIn("FORGED", raw)
        self.assertNotIn(TOKEN, raw)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertIn("ANSWER q1 = Approved", log)
        self.assertNotIn("ANSWER q1 = yes", log)
        self.assertNotIn("FORGED", log)


if __name__ == "__main__":
    unittest.main()
