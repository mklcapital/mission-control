"""Answer and question timestamps stay at second resolution.

Stdlib only. The server binds localhost and every data file is in a temp dir.
answers.py compares timestamps as strings, so a fractional second would
reorder a --since value that has no fraction.
"""

import contextlib
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import ask
import server


TOKEN = "unit-test-key"
SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
ID_STAMP = re.compile(r"^\d{8}-\d{6}-")


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def http_request(host, port, method, path, body=None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        payload = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, payload, hdrs, resp.version
    finally:
        conn.close()


class AnswerTimestampTests(unittest.TestCase):
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
                "question": "Approve the draw?",
                "project": "LOAN",
                "status": "open",
            }], fh)

    def test_an_answer_timestamp_has_no_fractional_second(self):
        status, body, hdrs, version = http_request(
            self.host, self.port, "POST", "/api/answer?key=" + TOKEN,
            body=json.dumps({"id": "q1", "answer": "Approved"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(version, 11)
        self.assertIsNone(hdrs.get("connection"))
        ts = json.loads(body)["ts"]
        self.assertRegex(ts, SECOND)
        self.assertNotIn(".", ts)
        self.assertNotIn(TOKEN, body.decode())

        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answered_ts"], ts)
        self.assertEqual(saved[0]["answer_summary"], "Approved")

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["ts"], ts)
        self.assertEqual(record["id"], "q1")
        self.assertEqual(record["answer"], "Approved")
        self.assertNotIn(TOKEN, lines[0])


class QuestionTimestampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_a_new_question_id_and_created_ts_use_whole_seconds(self):
        stdout = io.StringIO()
        old = sys.argv
        sys.argv = [
            "ask.py", "--project", "loan", "--type", "yesno",
            "--q", "Rebooted yet?",
        ]
        try:
            with contextlib.redirect_stdout(stdout):
                ask.main()
        finally:
            sys.argv = old
        qid = stdout.getvalue().strip()
        self.assertRegex(qid, ID_STAMP)
        self.assertNotIn(".", qid)

        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["id"], qid)
        self.assertEqual(saved[0]["project"], "LOAN")
        self.assertEqual(saved[0]["type"], "yesno")
        self.assertEqual(saved[0]["question"], "Rebooted yet?")
        self.assertRegex(saved[0]["created_ts"], SECOND)
        self.assertNotIn(".", saved[0]["created_ts"])
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))


if __name__ == "__main__":
    unittest.main()
