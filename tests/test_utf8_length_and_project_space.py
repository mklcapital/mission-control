"""UTF-8 body length is bytes, and a project chip is not trimmed.

Stdlib only. The server binds localhost and every data file is in a temp dir.
"""

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import ask
import server


TOKEN = "unit-test-key"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class Utf8LengthTests(unittest.TestCase):
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
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{
                "id": "q1",
                "status": "open",
                "project": "LOAN",
                "question": "Ship it?",
            }], fh)

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

    def test_content_length_counts_utf8_bytes(self):
        # "café" is one character longer in bytes than in Unicode scalars.
        # A length counted in characters truncates the JSON and must not
        # record a partial answer. The byte length stores the word intact.
        body = json.dumps(
            {"id": "q1", "answer": "café"}, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(body), len(body.decode("utf-8")))
        short = str(len(body.decode("utf-8")))

        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body,
            headers={"Content-Length": short})
        self.assertEqual(status, 400, payload)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertIn(b"bad json", payload)
        self.assertNotIn("café".encode(), payload)
        self.assertNotIn(b"Ship it?", payload)
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "open")
        self.assertNotIn("answer_summary", saved[0])

        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 200, payload)
        self.assertNotIn("connection", hdrs)
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["answer"], "café")
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertEqual(rows[0]["question"], "Ship it?")
        self.assertNotIn("\\u00e9", raw)
        self.assertIn("café", raw)
        self.assertNotIn(TOKEN, raw)
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            queue = fh.read()
        saved = json.loads(queue)
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "café")
        self.assertIn("café", queue)
        self.assertNotIn("\\u00e9", queue)

        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 409, payload)
        self.assertEqual(hdrs.get("connection"), "close")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertIn("ANSWER q1 = café", log)


class ProjectSpaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_project_is_uppercased_without_trimming(self):
        stdout = io.StringIO()
        old = sys.argv
        sys.argv = [
            "ask.py",
            "--project", " loan ",
            "--type", "yesno",
            "--q", "Ready?",
        ]
        try:
            with contextlib.redirect_stdout(stdout):
                ask.main()
        finally:
            sys.argv = old

        qid = stdout.getvalue().strip()
        self.assertTrue(qid.endswith("-ready"), qid)
        self.assertNotIn("loan", qid)
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        saved = json.loads(raw)
        self.assertEqual(len(saved), 1)
        item = saved[0]
        self.assertEqual(item["project"], " LOAN ")
        self.assertNotEqual(item["project"], "LOAN")
        self.assertEqual(item["type"], "yesno")
        self.assertEqual(item["question"], "Ready?")
        self.assertEqual(item["options"], [])
        self.assertEqual(item["context"], "")
        self.assertEqual(item["status"], "open")
        self.assertIn('" LOAN "', raw)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))


if __name__ == "__main__":
    unittest.main()
