"""Wrong methods and bodies must not answer, and a shared timestamp is one poll.

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
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import answers
import server


TOKEN = "unit-test-key"
SENTINEL = "SECRET_QUESTION_DO_NOT_LEAK"
FORGED = "FORGED_QUESTION_DO_NOT_STORE"


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


class MethodAndBodyTests(unittest.TestCase):
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
        self.queue_bytes = json.dumps([{
            "id": "q1",
            "question": SENTINEL,
            "project": "LOAN",
            "status": "open",
        }]).encode()
        with open(server.QUESTIONS_FILE, "wb") as fh:
            fh.write(self.queue_bytes)

    def queue(self):
        with open(server.QUESTIONS_FILE, "rb") as fh:
            return fh.read()

    def ledger(self):
        if not os.path.exists(server.ANSWERS_FILE):
            return []
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def log_text(self):
        if not os.path.exists(server.LOG_FILE):
            return ""
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_form_body_and_missing_length_do_not_answer(self):
        form = b"id=q1&answer=yes&note=FORGED_NOTE"
        header = (
            "POST /api/answer?key=%s HTTP/1.1\r\n"
            "Host: %s:%s\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "\r\n"
        ) % (TOKEN, self.host, self.port, len(form))
        status, raw = raw_exchange(self.host, self.port, header.encode("ascii") + form)
        self.assertEqual(status, 400)
        self.assertIn(b"bad json", raw)
        self.assertIn(b"Connection: close", raw)
        self.assertNotIn(SENTINEL.encode(), raw)
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertEqual(self.queue(), self.queue_bytes)
        self.assertEqual(self.ledger(), [])

        missing = (
            "POST /api/answer?key=%s HTTP/1.1\r\n"
            "Host: %s:%s\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{\"id\":\"q1\",\"answer\":\"yes\"}"
        ) % (TOKEN, self.host, self.port)
        status, raw = raw_exchange(self.host, self.port, missing.encode())
        self.assertEqual(status, 400)
        self.assertIn(b"bad content-length", raw)
        self.assertIn(b"Connection: close", raw)
        self.assertNotIn(SENTINEL.encode(), raw)
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertEqual(self.queue(), self.queue_bytes)
        self.assertEqual(self.ledger(), [])

        body = json.dumps({"id": "q1", "answer": "yes"}).encode()
        conn = HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/api/answer?key=%s" % TOKEN,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            payload = resp.read()
            code = resp.status
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()
        self.assertEqual(code, 200)
        self.assertIn(b'"ok": true', payload)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.ledger()[0]["answer"], "yes")
        self.assertEqual(self.ledger()[0]["question"], SENTINEL)
        stored = json.loads(self.queue())
        self.assertEqual(stored[0]["status"], "answered")
        log = self.log_text()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("FORGED_NOTE", log)

    def test_put_delete_and_patch_do_not_replace_the_queue(self):
        forged = json.dumps([{
            "id": "forged",
            "question": FORGED,
            "status": "open",
        }]).encode()
        for method in ("PUT", "DELETE", "PATCH"):
            conn = HTTPConnection(self.host, self.port, timeout=5)
            try:
                conn.request(
                    method,
                    "/api/questions?key=%s" % TOKEN,
                    body=forged,
                    headers={"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                payload = resp.read()
                code = resp.status
            finally:
                conn.close()
            self.assertEqual(code, 501, method)
            self.assertNotIn(SENTINEL.encode(), payload)
            self.assertNotIn(FORGED.encode(), payload)
            self.assertNotIn(TOKEN.encode(), payload)
            self.assertEqual(self.queue(), self.queue_bytes)
            self.assertEqual(self.ledger(), [])
        log = self.log_text()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(FORGED, log)


class SharedTimestampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            for row in (
                {"ts": "2026-01-02T00:00:00", "id": "older", "answer": "no"},
                {"ts": "2026-01-03T00:00:00", "id": "first", "answer": "yes-a"},
                {"ts": "2026-01-03T00:00:00", "id": "second", "answer": "yes-b"},
            ):
                fh.write(json.dumps(row) + "\n")
        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("2026-01-02T00:00:00\n")

    def run_main(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py"]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        return stdout.getvalue(), stderr.getvalue()

    def test_rows_sharing_a_timestamp_are_both_returned_once(self):
        stdout, stderr = self.run_main()
        self.assertEqual(stderr, "")
        rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([row["id"] for row in rows], ["first", "second"])
        self.assertEqual([row["answer"] for row in rows], ["yes-a", "yes-b"])
        self.assertNotIn("older", stdout)
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-03T00:00:00\n")

        stdout, stderr = self.run_main()
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "# no new answers\n")
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-03T00:00:00\n")


if __name__ == "__main__":
    unittest.main()
