"""A payload cannot reopen an answer, and a percent sign is not part of the id.

Stdlib only. The server binds localhost and every data file is in a temp dir.
"""

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import ask
import server


TOKEN = "unit-test-key"
FORGED = "FORGED-SUMMARY"
FORGED_TS = "1999-01-01T00:00:00+00:00"


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


class ForgedStatusTests(unittest.TestCase):
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
        self.question = "SENTINEL-QUESTION-TEXT"
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{
                "id": "q1",
                "project": "LOAN",
                "question": self.question,
                "status": "open",
                "options": ["yes", "no"],
            }, {
                "id": "q2",
                "project": "FAB",
                "question": "sibling stays open",
                "status": "open",
            }], fh)

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body, headers)

    def test_payload_status_cannot_reopen_or_rewrite_the_summary(self):
        body = json.dumps({
            "id": "q1",
            "answer": "yes",
            "status": "open",
            "answered_ts": FORGED_TS,
            "answer_summary": FORGED,
            "project": "FORGED-PROJECT",
            "question": "forged question",
        }).encode()
        path = "/api/answer?key=%s" % quote(TOKEN)
        status, payload, headers = self.request(
            "POST", path, body, {"Content-Type": "application/json", "Content-Length": str(len(body))})
        self.assertEqual(status, 200)
        self.assertNotIn("connection", headers)
        stored = json.loads(payload)
        self.assertEqual(stored["ok"], True)
        self.assertNotEqual(stored["ts"], FORGED_TS)
        self.assertNotIn(".", stored["ts"])
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            queue = json.load(fh)
        self.assertEqual(queue[0]["status"], "answered")
        self.assertEqual(queue[0]["answered_ts"], stored["ts"])
        self.assertEqual(queue[0]["answer_summary"], "yes")
        self.assertEqual(queue[0]["options"], ["yes", "no"])
        self.assertEqual(queue[0]["project"], "LOAN")
        self.assertEqual(queue[0]["question"], self.question)
        self.assertEqual(queue[1]["status"], "open")
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            raw_queue = fh.read()
        self.assertNotIn(FORGED, raw_queue)
        self.assertNotIn(FORGED_TS, raw_queue)
        self.assertNotIn("FORGED-PROJECT", raw_queue)
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln]
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(list(row), ["ts", "id", "project", "question", "answer", "other_text", "note"])
        self.assertEqual(row["ts"], stored["ts"])
        self.assertEqual(row["answer"], "yes")
        self.assertEqual(row["project"], "LOAN")
        self.assertEqual(row["question"], self.question)
        self.assertNotIn(FORGED, lines[0])
        self.assertNotIn(FORGED_TS, lines[0])
        self.assertNotIn(TOKEN, lines[0])
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertIn("ANSWER q1 = yes", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(FORGED, log)
        self.assertNotIn(FORGED_TS, log)

        status, payload, headers = self.request(
            "POST", path, body, {"Content-Type": "application/json", "Content-Length": str(len(body))})
        self.assertEqual(status, 409)
        self.assertEqual(headers.get("connection"), "close")
        conflict = json.loads(payload)
        self.assertEqual(conflict["answer_summary"], "yes")
        self.assertEqual(conflict["answered_ts"], stored["ts"])
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(len([ln for ln in fh.read().splitlines() if ln]), 1)


class PercentSlugTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def ask(self, *args):
        old = sys.argv
        sys.argv = ["ask.py", *args]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                ask.main()
            return buf.getvalue().strip()
        finally:
            sys.argv = old

    def test_percent_is_dropped_from_the_id_and_kept_in_the_question(self):
        question = "100% done?"
        qid = self.ask("--project", "loan", "--type", "yesno", "--q", question)
        self.assertTrue(qid.endswith("-100-done"), qid)
        self.assertNotIn("%", qid)
        self.assertNotIn("?", qid)
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        rows = json.loads(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], qid)
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertEqual(rows[0]["type"], "yesno")
        self.assertEqual(rows[0]["question"], question)
        self.assertEqual(rows[0]["options"], [])
        self.assertEqual(rows[0]["context"], "")
        self.assertEqual(rows[0]["status"], "open")
        self.assertIn("100% done?", raw)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))


if __name__ == "__main__":
    unittest.main()
