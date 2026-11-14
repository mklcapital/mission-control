"""Padded answer fields are stored trimmed, and Content-Length prefixes stay numeric.

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
QUESTION = "QUEUE-SENTINEL-do-not-leak"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def response_of(resp):
    payload = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    return resp.status, payload, hdrs


class QueueTests(unittest.TestCase):
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

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.putrequest(method, path)
            if body is not None and headers is None:
                conn.putheader("Content-Length", str(len(body)))
                conn.putheader("Content-Type", "application/json")
            for key, value in headers or ():
                conn.putheader(key, value)
            conn.endheaders(body)
            return response_of(conn.getresponse())
        finally:
            conn.close()

    def seed(self):
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([
                {"id": "q1", "question": QUESTION, "project": "LOAN", "status": "open"},
                {"id": "q2", "question": "sibling stays open", "project": "FAB", "status": "open"},
            ], fh)

    def ledger(self):
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def log(self):
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_padded_answer_fields_are_stored_trimmed(self):
        # A padded yes must conflict with a later exact yes. Unicode spaces
        # count: strip() is what the ledger and the 409 gate both use.
        self.seed()
        body = json.dumps({
            "id": " \u00a0q1\t ",
            "answer": " \u00a0yes\t ",
            "other_text": " \u00a0wing\t ",
            "note": " \u00a0n\t ",
            "project": "FORGED",
            "question": "FORGED-QUESTION",
        }).encode()
        status, payload, hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 200)
        self.assertIsNone(hdrs.get("connection"))
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(QUESTION.encode(), payload)

        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "q1")
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertEqual(rows[0]["other_text"], "wing")
        self.assertEqual(rows[0]["note"], "n")
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertEqual(rows[0]["question"], QUESTION)
        self.assertNotIn("\u00a0", rows[0]["answer"])
        self.assertNotIn(TOKEN, json.dumps(rows[0]))

        stored = self.questions()
        self.assertEqual(stored[0]["status"], "answered")
        self.assertEqual(stored[0]["answer_summary"], "yes — wing [note: n]")
        self.assertEqual(stored[1]["status"], "open")
        self.assertNotIn("answer_summary", stored[1])

        log = self.log()
        self.assertIn("ANSWER q1 = yes — wing", log)
        self.assertNotIn("[note:", log)
        self.assertNotIn(TOKEN, log)
        self.assertIn("key=REDACTED", log)

        status, payload, hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN,
            body=json.dumps({"id": "q1", "answer": "yes"}).encode())
        self.assertEqual(status, 409)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertIn(b"already answered", payload)
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.questions()[1]["status"], "open")

    def test_numeric_content_length_prefixes_parse_and_junk_does_not(self):
        # int() accepts a leading plus and leading zeros. A hex or exponent
        # length must not be read as a body, and +0 is still an empty body.
        self.seed()
        yes = json.dumps({"id": "q1", "answer": "yes"}).encode()
        approved = json.dumps({"id": "q2", "answer": "Approved"}).encode()

        status, payload, hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN, body=yes,
            headers=(
                ("Content-Length", "+" + str(len(yes))),
                ("Content-Type", "application/json"),
            ))
        self.assertEqual(status, 200, payload)
        self.assertIsNone(hdrs.get("connection"))
        self.assertEqual(self.ledger()[0]["answer"], "yes")
        self.assertEqual(self.questions()[0]["status"], "answered")
        self.assertEqual(self.questions()[1]["status"], "open")

        status, payload, hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN, body=approved,
            headers=(
                ("Content-Length", str(len(approved)).zfill(4)),
                ("Content-Type", "application/json"),
            ))
        self.assertEqual(status, 200, payload)
        self.assertTrue(str(len(approved)).zfill(4).startswith("0"))
        rows = self.ledger()
        self.assertEqual([row["id"] for row in rows], ["q1", "q2"])
        self.assertEqual(rows[1]["answer"], "Approved")
        self.assertEqual(rows[1]["project"], "FAB")
        self.assertEqual(self.questions()[1]["status"], "answered")

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            before = fh.read()
        for length in ("0x10", "1e2", "+0"):
            status, payload, hdrs = self.request(
                "POST", "/api/answer?key=" + TOKEN,
                body=json.dumps({"id": "q1", "answer": "forged-" + length}).encode(),
                headers=(
                    ("Content-Length", length),
                    ("Content-Type", "application/json"),
                ))
            self.assertEqual(status, 400, length)
            self.assertEqual(hdrs.get("connection"), "close", length)
            self.assertIn(b"bad content-length", payload, length)
            self.assertNotIn(b"forged-", payload)
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        log = self.log()
        self.assertNotIn(TOKEN, log)
        self.assertIn("key=REDACTED", log)
        self.assertNotIn("forged-", log)
