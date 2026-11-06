"""Partial bodies, boolean answers, and repeated choice options.

Stdlib only. The server binds localhost and every data file is in a temp dir.
"""

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import ask
import server


TOKEN = "unit-test-key"
SENTINEL = "SECRET_QUESTION_DO_NOT_LEAK"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def raw_exchange(host, port, payload):
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    line = raw.split(b"\r\n", 1)[0]
    return int(line.split()[1]), raw


class PartialBodyAndBoolAnswerTests(unittest.TestCase):
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
                "question": SENTINEL,
                "project": "LOAN",
                "status": "open",
            }], fh)

    def ledger(self):
        if not os.path.exists(server.ANSWERS_FILE):
            return []
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def queue(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_declared_length_ignores_a_trailing_second_object(self):
        first = json.dumps({"id": "q1", "answer": "yes"}).encode()
        extra = b'{"id":"q1","answer":"forged-second"}'
        raw = (
            "POST /api/answer?key=%s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "\r\n"
        ) % (TOKEN, self.host, len(first))
        status, body = raw_exchange(self.host, self.port, raw.encode() + first + extra)
        self.assertEqual(status, 200)
        self.assertNotIn(b"forged-second", body)
        self.assertNotIn(SENTINEL.encode(), body)
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertEqual(rows[0]["id"], "q1")
        self.assertEqual(rows[0]["question"], SENTINEL)
        self.assertNotIn("forged-second", json.dumps(rows))
        saved = self.queue()
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], "yes")
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn(TOKEN, log)
        self.assertIn("key=REDACTED", log)
        self.assertNotIn("forged-second", log)

    def test_json_true_is_stored_as_text_and_a_second_submit_conflicts(self):
        body = json.dumps({"id": "q1", "answer": True}).encode()
        raw = (
            "POST /api/answer?key=%s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "\r\n"
        ) % (TOKEN, self.host, len(body))
        status, response = raw_exchange(self.host, self.port, raw.encode() + body)
        self.assertEqual(status, 200)
        self.assertNotIn(b"Connection: close", response.split(b"\r\n\r\n", 1)[0])
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["answer"], "True")
        self.assertNotEqual(rows[0]["answer"], "true")
        self.assertEqual(self.queue()[0]["answer_summary"], "True")
        again, again_body = raw_exchange(self.host, self.port, raw.encode() + body)
        self.assertEqual(again, 409)
        self.assertIn(b"already answered", again_body)
        self.assertIn(b"Connection: close", again_body)
        self.assertEqual(len(self.ledger()), 1)
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn(TOKEN, log)
        self.assertTrue(any(line.endswith(" ANSWER q1 = True") for line in log.splitlines()))


class OptionOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_repeated_options_stay_in_order_and_the_note_is_context(self):
        note = "see  binder"
        question = "Which wing?"
        stdout = io.StringIO()
        old = sys.argv
        sys.argv = [
            "ask.py",
            "--project", "loan ops",
            "--type", "choice",
            "--q", question,
            "--opt", "C|A|A|B",
            "--note", note,
        ]
        try:
            with contextlib.redirect_stdout(stdout):
                ask.main()
        finally:
            sys.argv = old
        qid = stdout.getvalue().strip()
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            raw = fh.read()
        stored = json.loads(raw)
        self.assertEqual(len(stored), 1)
        item = stored[0]
        self.assertEqual(item["id"], qid)
        self.assertEqual(item["options"], ["C", "A", "A", "B"])
        self.assertEqual(item["context"], note)
        self.assertEqual(item["question"], question)
        self.assertEqual(item["project"], "LOAN OPS")
        self.assertEqual(item["type"], "choice")
        self.assertEqual(item["status"], "open")
        self.assertTrue(raw.endswith("\n"))
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))


if __name__ == "__main__":
    unittest.main()
