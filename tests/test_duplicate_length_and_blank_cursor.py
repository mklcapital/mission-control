"""Duplicate lengths cannot answer, and a blank cursor still polls.

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

import answers
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


class DuplicateLengthTests(unittest.TestCase):
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

    def post_raw(self, header_lines, body):
        head = "POST /api/answer?key=%s HTTP/1.1\r\nHost: %s:%s\r\n%s\r\n\r\n" % (
            TOKEN, self.host, self.port, "\r\n".join(header_lines),
        )
        return raw_exchange(self.host, self.port, head.encode() + body)

    def queue(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_zero_length_and_a_spaced_length_do_not_record(self):
        body = json.dumps({"id": "q1", "answer": "yes", "note": SENTINEL}).encode()
        # The first Content-Length wins. A later length must not smuggle a body.
        status, raw = self.post_raw(
            ["Content-Length: 0", "Content-Length: %d" % len(body)],
            body,
        )
        self.assertEqual(status, 400)
        self.assertIn(b"bad content-length", raw)
        self.assertIn(b"Connection: close", raw)
        self.assertNotIn(SENTINEL.encode(), raw)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        self.assertEqual(self.queue()[0]["status"], "open")

        status, raw = self.post_raw(
            ["Content-Length: %d %d" % (len(body), len(body))],
            body,
        )
        self.assertEqual(status, 400)
        self.assertIn(b"bad content-length", raw)
        self.assertNotIn(SENTINEL.encode(), raw)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        self.assertEqual(self.queue()[0]["status"], "open")

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(SENTINEL, log)

        import http.client
        ok_body = json.dumps({"id": "q1", "answer": "yes"}).encode()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/api/answer?key=%s" % TOKEN,
                body=ok_body,
                headers={"Content-Length": str(len(ok_body))},
            )
            resp = conn.getresponse()
            payload = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertNotIn("connection", hdrs)
        self.assertNotIn(SENTINEL.encode(), payload)
        self.assertEqual(self.queue()[0]["status"], "answered")
        self.assertEqual(self.queue()[0]["answer_summary"], "yes")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["answer"], "yes")


class BlankCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")

    def test_a_whitespace_cursor_replays_every_dated_row_then_advances(self):
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            for row in (
                {"ts": "2026-01-01T00:00:00", "id": "old", "answer": "no"},
                {"ts": "2026-01-03T00:00:00", "id": "new", "answer": "yes"},
            ):
                fh.write(json.dumps(row) + "\n")
        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("\n \t \n")

        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py"]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.getvalue().splitlines()],
            ["old", "new"],
        )
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-03T00:00:00\n")

        stdout = io.StringIO()
        stderr = io.StringIO()
        sys.argv = ["answers.py"]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "# no new answers\n")


if __name__ == "__main__":
    unittest.main()
