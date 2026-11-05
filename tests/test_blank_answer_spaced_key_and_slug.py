"""Blank answers, spaced query keys, and yes/no options.

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
SENTINEL = "BLANK-SENTINEL-QUESTION"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class BlankAnswerAndSpacedKeyTests(unittest.TestCase):
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
                "question": SENTINEL,
                "project": "LOAN",
                "status": "open",
            }], fh)

    def post(self, payload):
        body = json.dumps(payload).encode()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/api/answer?key=%s" % TOKEN,
                body=body,
                headers={"Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            payload_bytes = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, payload_bytes, hdrs
        finally:
            conn.close()

    def get(self, path):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            payload = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, payload, hdrs
        finally:
            conn.close()

    def queue(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_whitespace_id_or_answer_does_not_record(self):
        for payload in (
            {"id": "  \t", "answer": "yes"},
            {"id": "q1", "answer": " \n\t "},
        ):
            status, body, hdrs = self.post(payload)
            self.assertEqual(status, 400, body)
            self.assertIn(b"id and answer required", body)
            self.assertEqual(hdrs.get("connection"), "close")
            self.assertNotIn(SENTINEL.encode(), body)
            self.assertNotIn(TOKEN.encode(), body)
            self.assertFalse(os.path.exists(server.ANSWERS_FILE))
            self.assertEqual(self.queue()[0]["status"], "open")

        status, body, hdrs = self.post({"id": "q1", "answer": "yes"})
        self.assertEqual(status, 200, body)
        self.assertNotIn("connection", hdrs)
        self.assertEqual(self.queue()[0]["status"], "answered")
        self.assertEqual(self.queue()[0]["answer_summary"], "yes")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["answer"], "yes")
        self.assertNotIn(TOKEN, lines[0])

    def test_whitespace_around_the_query_key_does_not_authorize(self):
        for path in (
            "/api/questions?key=%20" + TOKEN,
            "/api/questions?key=" + TOKEN + "%20",
            "/api/questions?key=%20" + TOKEN + "%20",
            "/api/questions?key=%09" + TOKEN,
        ):
            status, body, hdrs = self.get(path)
            self.assertEqual(status, 403, (path, body))
            self.assertEqual(hdrs.get("connection"), "close")
            self.assertNotIn(SENTINEL.encode(), body)
            self.assertNotIn(TOKEN.encode(), body)

        status, body, hdrs = self.get("/api/questions?key=" + TOKEN)
        self.assertEqual(status, 200, body)
        self.assertNotIn("connection", hdrs)
        self.assertIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(SENTINEL, log)


class YesnoOptAndDigitSlugTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def ask(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["ask.py", *args]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ask.main()
        finally:
            sys.argv = old
        return stdout.getvalue().strip(), stderr.getvalue()

    def test_yesno_and_text_reject_options_and_write_nothing(self):
        cases = (
            ["--project", "loan", "--type", "yesno", "--q", "Ready?", "--opt", "Yes|No"],
            ["--project", "loan", "--type", "text", "--q", "Notes?", "--opt", "x"],
        )
        for args in cases:
            stderr = io.StringIO()
            old = sys.argv
            sys.argv = ["ask.py", *args]
            try:
                with self.assertRaises(SystemExit) as caught:
                    with contextlib.redirect_stderr(stderr):
                        ask.main()
            finally:
                sys.argv = old
            self.assertEqual(caught.exception.code, 2, args)
            self.assertIn("--opt", stderr.getvalue())
            self.assertFalse(os.path.exists(ask.QUESTIONS_FILE), args)
            self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"), args)

    def test_slug_keeps_digits_and_collapses_repeated_spaces(self):
        question = "Q  2  ready"
        qid, err = self.ask("--project", "loan", "--type", "yesno", "--q", question)
        self.assertEqual(err, "")
        suffix = qid.split("-", 2)[-1]
        self.assertEqual(suffix, "q-2-ready", qid)
        self.assertNotIn(" ", suffix)
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], qid)
        self.assertEqual(stored[0]["question"], question)
        self.assertEqual(stored[0]["project"], "LOAN")
        self.assertEqual(stored[0]["type"], "yesno")
        self.assertEqual(stored[0]["options"], [])


if __name__ == "__main__":
    unittest.main()
