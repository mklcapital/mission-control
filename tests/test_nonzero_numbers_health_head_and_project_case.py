"""Non-zero answers are kept, HEAD /health is empty, and project case is Unicode.

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
import unittest.mock
from http.server import ThreadingHTTPServer

import ask
import server


TOKEN = "unit-test-key"
SENTINEL = "HEALTH-SENTINEL-QUESTION"
SIBLING = "SIBLING-STAYS-OPEN"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class NonZeroAndHealthTests(unittest.TestCase):
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
            json.dump([
                {"id": "q1", "question": SENTINEL, "project": "LOAN", "status": "open"},
                {"id": "q2", "question": SIBLING, "project": "FAB", "status": "open"},
            ], fh)

    def exchange(self, method, path, body=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body)
            resp = conn.getresponse()
            payload = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, payload, hdrs
        finally:
            conn.close()

    def test_head_health_is_empty_and_a_nonzero_number_is_stored(self):
        # JSON 0 is dropped by `or ""`. 2, 3, and 4 must still be recorded.
        status, body, hdrs = self.exchange("HEAD", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn("content-security-policy", hdrs)
        get_status, get_body, get_hdrs = self.exchange("GET", "/health")
        self.assertEqual(get_status, 200)
        self.assertEqual(hdrs.get("content-length"), str(len(get_body)))
        self.assertNotIn(SENTINEL.encode(), get_body)
        self.assertNotIn(TOKEN.encode(), get_body)
        self.assertNotEqual(get_hdrs.get("connection"), "close")

        payload = json.dumps({
            "id": "q1",
            "answer": 2,
            "other_text": 3,
            "note": 4,
            "project": "FORGED",
            "question": "FORGED",
        }).encode()
        status, body, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, payload)
        self.assertEqual(status, 200, body)
        self.assertNotIn("connection", hdrs)
        self.assertNotIn(TOKEN.encode(), body)
        record = json.loads(body)
        self.assertTrue(record["ok"])
        self.assertNotIn(".", record["ts"])

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            ledger = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["answer"], "2")
        self.assertEqual(ledger[0]["other_text"], "3")
        self.assertEqual(ledger[0]["note"], "4")
        self.assertEqual(ledger[0]["question"], SENTINEL)
        self.assertEqual(ledger[0]["project"], "LOAN")
        self.assertNotIn("key", ledger[0])
        self.assertNotIn(TOKEN, json.dumps(ledger))

        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            queue = json.load(fh)
        answered = next(q for q in queue if q["id"] == "q1")
        sibling = next(q for q in queue if q["id"] == "q2")
        self.assertEqual(answered["status"], "answered")
        self.assertEqual(answered["answer_summary"], "2 — 3 [note: 4]")
        self.assertEqual(sibling["status"], "open")
        self.assertNotIn("answer_summary", sibling)

        again, again_body, again_hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, payload)
        self.assertEqual(again, 409, again_body)
        self.assertEqual(again_hdrs.get("connection"), "close")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(sum(1 for line in fh if line.strip()), 1)
        log = ""
        if os.path.exists(server.LOG_FILE):
            with open(server.LOG_FILE, encoding="utf-8") as fh:
                log = fh.read()
        self.assertNotIn(TOKEN, log)
        self.assertIn("key=REDACTED", log)

    def test_unicode_project_is_uppercased_and_the_question_stays_verbatim(self):
        for name in ("QUESTIONS_FILE", "LOCK_FILE"):
            self.addCleanup(setattr, ask, name, getattr(ask, name))
        ask.QUESTIONS_FILE = os.path.join(self.dir, "asked.json")
        ask.LOCK_FILE = os.path.join(self.dir, ".ask.lock")
        question = "Is café ready?"
        buf = io.StringIO()
        argv = ["ask.py", "--project", "café", "--type", "yesno", "--q", question]
        with patch_argv(argv):
            with contextlib.redirect_stdout(buf):
                ask.main()
        qid = buf.getvalue().strip()
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        stored = json.loads(raw)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], qid)
        self.assertEqual(stored[0]["project"], "CAFÉ")
        self.assertEqual(stored[0]["question"], question)
        self.assertEqual(stored[0]["type"], "yesno")
        self.assertEqual(stored[0]["options"], [])
        self.assertEqual(stored[0]["status"], "open")
        self.assertIn("café", raw)
        self.assertIn("CAFÉ", raw)
        self.assertNotIn("\\u00e9", raw)


def patch_argv(argv):
    return unittest.mock.patch.object(sys, "argv", argv)


if __name__ == "__main__":
    unittest.main()
