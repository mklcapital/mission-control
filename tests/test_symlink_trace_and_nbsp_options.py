"""A queue symlink is replaced, TRACE does not answer, NBSP is not an option.

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
SENTINEL = "SYMLINK-SENTINEL-QUESTION"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class SymlinkAndTraceTests(unittest.TestCase):
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
        self.queue = [{
            "id": "q1",
            "question": SENTINEL,
            "project": "LOAN",
            "status": "open",
            "options": ["yes", "no"],
        }]
        self._write_queue(server.QUESTIONS_FILE)

    def _write_queue(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.queue, fh)
            fh.write("\n")

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

    def test_answering_replaces_a_symlink_and_does_not_write_through_it(self):
        # os.replace must swap the directory entry. Following the symlink would
        # overwrite whatever file an attacker pointed questions.json at.
        target = os.path.join(self.dir, "real-queue.json")
        os.remove(server.QUESTIONS_FILE)
        self._write_queue(target)
        with open(target, "rb") as fh:
            original = fh.read()
        os.symlink(target, server.QUESTIONS_FILE)
        self.assertTrue(os.path.islink(server.QUESTIONS_FILE))

        body = json.dumps({
            "id": "q1",
            "answer": "yes",
            "project": "FORGED-PROJECT",
            "question": "FORGED-QUESTION",
        }).encode()
        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body)
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(b"FORGED-PROJECT", payload)

        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertIn(b'"status": "open"', original)
        self.assertFalse(os.path.islink(server.QUESTIONS_FILE))

        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["question"], SENTINEL)
        self.assertEqual(saved[0]["project"], "LOAN")
        self.assertEqual(saved[0]["options"], ["yes", "no"])
        self.assertNotIn("FORGED-PROJECT", json.dumps(saved))
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertEqual(rows[0]["question"], SENTINEL)
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertNotIn(TOKEN, json.dumps(rows))

    def test_trace_does_not_answer_or_echo_the_token(self):
        with open(server.QUESTIONS_FILE, "rb") as fh:
            before = fh.read()
        body = json.dumps({
            "id": "q1",
            "answer": "yes-forged",
            "note": TOKEN,
        }).encode()
        status, payload, _hdrs = self.exchange(
            "TRACE", "/api/answer?key=" + TOKEN, body)
        self.assertEqual(status, 501, payload)
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(SENTINEL.encode(), payload)
        self.assertNotIn(b"yes-forged", payload)
        self.assertNotIn(b'"ok"', payload)

        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(SENTINEL, log)
        self.assertNotIn("yes-forged", log)


class NbspOptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def ask(self, *args):
        stdout = io.StringIO()
        old = sys.argv
        sys.argv = ["ask.py", *args]
        try:
            with contextlib.redirect_stdout(stdout):
                ask.main()
        finally:
            sys.argv = old
        return stdout.getvalue().strip()

    def test_nbsp_around_options_is_stripped_and_a_blank_one_is_dropped(self):
        question = "Ready\u00a0now?"
        opt = "\u00a0A\u00a0|\u00a0|\u00a0B\u00a0"
        qid = self.ask(
            "--project", "loan", "--type", "choice", "--opt", opt, "--q", question)

        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["id"], qid)
        self.assertEqual(saved[0]["options"], ["A", "B"])
        self.assertNotIn("\u00a0", "".join(saved[0]["options"]))
        self.assertEqual(saved[0]["question"], question)
        self.assertIn("\u00a0", saved[0]["question"])
        self.assertEqual(saved[0]["type"], "choice")
        self.assertEqual(saved[0]["project"], "LOAN")
        self.assertEqual(saved[0]["status"], "open")
        self.assertTrue(qid.endswith("-readynow"), qid)
        self.assertNotIn("\u00a0", qid)
        self.assertNotIn("Ready", qid)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))


if __name__ == "__main__":
    unittest.main()
