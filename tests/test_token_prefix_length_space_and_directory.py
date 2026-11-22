"""A token prefix does not authorize, spaced lengths still answer, a directory queue is left alone.

Stdlib only. The server binds localhost and every data file is in a temp dir.
"""

import contextlib
import fcntl
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
SENTINEL = "PREFIX-SENTINEL-QUESTION"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


class TokenAndQueueTests(unittest.TestCase):
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
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        server.QUESTIONS_FILE = os.path.join(self.dir, "questions.json")
        server.ANSWERS_FILE = os.path.join(self.dir, "answers.jsonl")
        server.LOG_FILE = os.path.join(self.dir, "server.log")
        server.LOCK_FILE = os.path.join(self.dir, ".queue.lock")
        server.INDEX_FILE = os.path.join(self.dir, "index.html")
        server.TOKEN = TOKEN
        ask.QUESTIONS_FILE = server.QUESTIONS_FILE
        ask.LOCK_FILE = server.LOCK_FILE
        self.queue = [
            {
                "id": "q1",
                "question": SENTINEL,
                "project": "LOAN",
                "status": "open",
                "options": ["yes", "no"],
            },
            {
                "id": "q2",
                "question": "sibling stays open",
                "project": "FAB",
                "status": "open",
            },
        ]
        self._write_queue(server.QUESTIONS_FILE)

    def _write_queue(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.queue, fh)
            fh.write("\n")

    def exchange(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            payload = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, payload, hdrs
        finally:
            conn.close()

    def test_token_prefix_and_suffix_do_not_authorize(self):
        prefix = TOKEN[:-1]
        longer = TOKEN + "x"
        self.assertNotEqual(prefix, TOKEN)
        self.assertTrue(TOKEN.startswith(prefix))
        for supplied in (prefix, longer):
            status, payload, hdrs = self.exchange(
                "GET", "/api/questions?key=" + supplied)
            self.assertEqual(status, 403, payload)
            self.assertEqual(hdrs.get("connection"), "close")
            self.assertNotIn(SENTINEL.encode(), payload)
            self.assertNotIn(TOKEN.encode(), payload)
            self.assertNotIn(b"LOAN", payload)
            self.assertIn(b"bad or missing key", payload)

        status, payload, hdrs = self.exchange(
            "GET", "/api/questions?key=" + TOKEN)
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertIn(SENTINEL.encode(), payload)
        self.assertNotIn(TOKEN.encode(), payload)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(prefix, log)
        self.assertNotIn(longer, log)
        self.assertNotIn(SENTINEL, log)

    def test_surrounding_space_on_content_length_still_answers_once(self):
        body = json.dumps({"id": "q1", "answer": "yes", "project": "FORGED"}).encode()
        status, payload, hdrs = self.exchange(
            "POST",
            "/api/answer?key=" + TOKEN,
            body=body,
            headers={"Content-Length": "  %d" % len(body)},
        )
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(hdrs.get("connection"), "close")
        self.assertNotIn(TOKEN.encode(), payload)
        self.assertNotIn(b"FORGED", payload)

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertEqual(rows[0]["id"], "q1")
        self.assertEqual(rows[0]["project"], "LOAN")
        self.assertEqual(rows[0]["question"], SENTINEL)
        self.assertNotIn(TOKEN, json.dumps(rows))

        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[1]["status"], "open")
        self.assertEqual(saved[1]["id"], "q2")

        again = json.dumps({"id": "q1", "answer": "no"}).encode()
        status, payload, hdrs = self.exchange(
            "POST",
            "/api/answer?key=" + TOKEN,
            body=again,
            headers={"Content-Length": "%d " % len(again)},
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(hdrs.get("connection"), "close")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(sum(1 for line in fh if line.strip()), 1)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertIn("ANSWER q1 = yes", log)

    def test_directory_queue_is_not_replaced(self):
        os.remove(server.QUESTIONS_FILE)
        os.mkdir(server.QUESTIONS_FILE)
        body = json.dumps({
            "id": "q1",
            "answer": "yes-forged",
            "note": TOKEN,
        }).encode()
        # The directory read raises inside the handler. The stdlib server
        # prints a traceback and drops the connection; the process stays up.
        with self.assertRaises((
            ConnectionResetError,
            http.client.RemoteDisconnected,
            http.client.BadStatusLine,
        )):
            self.exchange("POST", "/api/answer?key=" + TOKEN, body=body)

        self.assertTrue(os.path.isdir(server.QUESTIONS_FILE))
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))
        self.assertEqual(os.listdir(server.QUESTIONS_FILE), [])
        with open(server.LOCK_FILE, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)

        stdout = io.StringIO()
        old = sys.argv
        sys.argv = [
            "ask.py", "--project", "loan", "--type", "yesno",
            "--q", "Ship it?",
        ]
        try:
            with self.assertRaises(IsADirectoryError):
                with contextlib.redirect_stdout(stdout):
                    ask.main()
        finally:
            sys.argv = old
        self.assertEqual(stdout.getvalue(), "")
        self.assertTrue(os.path.isdir(server.QUESTIONS_FILE))
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))

        os.rmdir(server.QUESTIONS_FILE)
        self._write_queue(server.QUESTIONS_FILE)
        body = json.dumps({"id": "q1", "answer": "yes"}).encode()
        status, payload, hdrs = self.exchange(
            "POST", "/api/answer?key=" + TOKEN, body=body)
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(hdrs.get("connection"), "close")
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["question"], SENTINEL)
        self.assertEqual(saved[0]["project"], "LOAN")
        self.assertEqual(saved[1]["status"], "open")
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["answer"], "yes")
        self.assertNotIn("yes-forged", json.dumps(rows))
        self.assertNotIn(TOKEN, json.dumps(rows))

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("yes-forged", log)
        self.assertIn("ANSWER q1 = yes", log)


if __name__ == "__main__":
    unittest.main()
