"""Content-Type does not authorize, and a newline stays inside one option.

Stdlib only. The server binds localhost and every data file is in a temp dir.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import ask
import server


TOKEN = "unit-test-key"
FORGED = "FORGED-PROJECT"


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


class ContentTypeDoesNotAuthorizeTests(unittest.TestCase):
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
            }, {
                "id": "q2",
                "project": "FAB",
                "question": "sibling stays open",
                "status": "open",
            }], fh)
        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.original = fh.read()

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body, headers)

    def log_text(self):
        if not os.path.exists(server.LOG_FILE):
            return ""
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_content_type_does_not_authorize_or_block_json(self):
        body = json.dumps({
            "id": "q1",
            "answer": "yes",
            "project": FORGED,
            "question": "forged question",
        }).encode()
        typed = "application/json; key=%s" % TOKEN
        status, payload, headers = self.request(
            "POST", "/api/answer", body, {"Content-Type": typed, "Content-Length": str(len(body))})
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("connection"), "close")
        self.assertNotIn(self.question.encode(), payload)
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.assertEqual(fh.read(), self.original)

        path = "/api/answer?key=%s" % quote(TOKEN)
        status, payload, headers = self.request(
            "POST", path, body, {"Content-Type": "text/plain", "Content-Length": str(len(body))})
        self.assertEqual(status, 200)
        self.assertNotIn("connection", headers)
        self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(FORGED.encode(), payload)
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln]
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["id"], "q1")
        self.assertEqual(row["answer"], "yes")
        self.assertEqual(row["project"], "LOAN")
        self.assertEqual(row["question"], self.question)
        self.assertNotIn(FORGED, lines[0])
        self.assertNotIn(TOKEN, lines[0])
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            queue = json.load(fh)
        self.assertEqual(queue[0]["status"], "answered")
        self.assertEqual(queue[0]["answer_summary"], "yes")
        self.assertEqual(queue[1]["status"], "open")

        log = self.log_text()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(typed, log)
        self.assertIn("ANSWER q1 = yes", log)
        self.assertNotIn(FORGED, log)

        status, payload, headers = self.request(
            "POST", path, body, {"Content-Type": "text/plain", "Content-Length": str(len(body))})
        self.assertEqual(status, 409)
        self.assertEqual(headers.get("connection"), "close")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(len([ln for ln in fh.read().splitlines() if ln]), 1)


class OptionNewlineTests(unittest.TestCase):
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
            buf = __import__("io").StringIO()
            with __import__("contextlib").redirect_stdout(buf):
                ask.main()
            return buf.getvalue().strip()
        finally:
            sys.argv = old

    def test_newline_inside_an_option_does_not_split_the_row(self):
        qid = self.ask(
            "--project", "loan", "--type", "choice", "--q", "Which?",
            "--opt", "A|B\nDROP|C")
        self.assertTrue(qid.endswith("-which"), qid)
        self.assertNotIn("drop", qid)
        self.assertNotIn("\n", qid)
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        rows = json.loads(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], qid)
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertEqual(rows[0]["type"], "choice")
        self.assertEqual(rows[0]["question"], "Which?")
        self.assertEqual(rows[0]["options"], ["A", "B\nDROP", "C"])
        self.assertEqual(rows[0]["status"], "open")
        self.assertNotIn("DROP", qid)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))
        # The stored question is one JSON value. A raw newline may appear only
        # as the escaped option, which json.loads already checked above.
        self.assertEqual(raw.count("{"), 1)


if __name__ == "__main__":
    unittest.main()
