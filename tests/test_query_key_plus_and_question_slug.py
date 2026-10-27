"""A plus in the query key is a space, and question ids stay a safe slug.

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


TOKEN = "unit+test-key"
SENTINEL = "PLUS-SENTINEL-QUESTION"


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return host, port, httpd


def http_request(host, port, method, path):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        payload = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, payload, hdrs
    finally:
        conn.close()


class QueryKeyPlusTests(unittest.TestCase):
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
        with open(server.INDEX_FILE, "w", encoding="utf-8") as fh:
            fh.write("<html>queue</html>\n")
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([
                {"id": "q1", "question": SENTINEL, "project": "LOAN", "status": "open"},
            ], fh)

    def request(self, path):
        return http_request(self.host, self.port, "GET", path)

    def test_a_plus_in_the_query_key_is_a_space(self):
        # application/x-www-form-urlencoded decodes '+' as a space. A token
        # that contains '+' must be percent-encoded or it authorizes nothing.
        status, body, hdrs = self.request("/api/questions?key=unit+test-key")
        self.assertEqual(status, 403, body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        status, body, hdrs = self.request("/api/questions?key=unit%20test-key")
        self.assertEqual(status, 403, body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(SENTINEL.encode(), body)

        for encoded in ("unit%2Btest-key", "unit%2btest-key"):
            status, body, hdrs = self.request("/api/questions?key=" + encoded)
            self.assertEqual(status, 200, (encoded, body))
            self.assertNotIn("connection", hdrs)
            self.assertIn(SENTINEL.encode(), body)
            self.assertNotIn(TOKEN.encode(), body)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn(TOKEN, log)
        self.assertIn("key=REDACTED", log)
        self.assertEqual(self.request("/api/questions?key=unit+test-key")[0], 403)


class QuestionSlugTests(unittest.TestCase):
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

    def test_the_stored_id_drops_punctuation_and_the_sixth_word(self):
        question = "A/B t;wo three four five six\nseven"
        qid = self.ask("--project", "loan", "--type", "text", "--q", question)
        punct = self.ask("--project", "loan", "--type", "text", "--q", "!!!")

        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual([item["id"] for item in saved], [qid, punct])

        self.assertTrue(qid.endswith("-ab-two-three-four-five"), qid)
        for leaked in ("/", "\n", ";", "six", "seven", "A/B"):
            self.assertNotIn(leaked, qid)
        self.assertEqual(saved[0]["question"], question)
        self.assertEqual(saved[0]["project"], "LOAN")
        self.assertEqual(saved[0]["type"], "text")
        self.assertEqual(saved[0]["options"], [])
        self.assertEqual(saved[0]["status"], "open")
        self.assertIn("six\nseven", saved[0]["question"])

        self.assertTrue(punct.endswith("-q"), punct)
        self.assertNotIn("!", punct)
        self.assertEqual(saved[1]["question"], "!!!")
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))


if __name__ == "__main__":
    unittest.main()
