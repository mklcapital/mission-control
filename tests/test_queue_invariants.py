"""Queue edges the auth and conflict tests do not lock.

Covers which query-string key wins, an empty queue file, whitespace-only
answer fields, the cross-process file lock ask.py must wait on, and --multi
not changing a yes/no question.
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
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import ask
import server

TOKEN = "unit-test-key"
SENTINEL = "SECRET_QUESTION_DO_NOT_LEAK"


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


class QueueEdgeTests(unittest.TestCase):
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
        self.addCleanup(setattr, server, "QUESTIONS_FILE", server.QUESTIONS_FILE)
        self.addCleanup(setattr, server, "ANSWERS_FILE", server.ANSWERS_FILE)
        self.addCleanup(setattr, server, "LOG_FILE", server.LOG_FILE)
        self.addCleanup(setattr, server, "LOCK_FILE", server.LOCK_FILE)
        self.addCleanup(setattr, server, "INDEX_FILE", server.INDEX_FILE)
        self.addCleanup(setattr, server, "TOKEN", server.TOKEN)
        server.QUESTIONS_FILE = os.path.join(self.dir, "questions.json")
        server.ANSWERS_FILE = os.path.join(self.dir, "answers.jsonl")
        server.LOG_FILE = os.path.join(self.dir, "server.log")
        server.LOCK_FILE = os.path.join(self.dir, ".queue.lock")
        server.INDEX_FILE = os.path.join(self.dir, "index.html")
        server.TOKEN = TOKEN

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body, headers)

    def seed(self, questions):
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(questions, fh)

    def questions(self):
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def answer_lines(self):
        if not os.path.exists(server.ANSWERS_FILE):
            return []
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln]

    def test_the_first_key_query_parameter_wins(self):
        self.seed([{"id": "q1", "question": SENTINEL, "status": "open"}])
        wrong_first = "/api/questions?key=wrong&key=%s" % quote(TOKEN)
        status, body, _hdrs = self.request("GET", wrong_first)
        self.assertEqual(status, 403)
        self.assertNotIn(SENTINEL.encode(), body)
        self.assertNotIn(TOKEN.encode(), body)

        head_status, head_body, _hdrs = self.request("HEAD", wrong_first)
        self.assertEqual(head_status, 403)
        self.assertEqual(head_body, b"")

        status, body, _hdrs = self.request(
            "POST", "/api/answer?key=wrong&key=%s" % quote(TOKEN),
            body=json.dumps({"id": "q1", "answer": "yes"}).encode())
        self.assertEqual(status, 403)
        self.assertEqual(self.answer_lines(), [])
        self.assertEqual(self.questions()[0]["status"], "open")

        status, body, _hdrs = self.request(
            "GET", "/api/questions?key=%s&key=wrong" % quote(TOKEN))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["question"], SENTINEL)

    def test_missing_questions_file_is_an_empty_queue(self):
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE))
        status, body, _hdrs = self.request(
            "GET", "/api/questions?key=%s" % quote(TOKEN))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE))

    def test_blank_other_text_and_note_do_not_decorate_the_summary(self):
        self.seed([{
            "id": "q1",
            "status": "open",
            "project": "LOAN",
            "question": "Ship it?",
        }])
        status, _body, _hdrs = self.request(
            "POST", "/api/answer?key=%s" % quote(TOKEN),
            body=json.dumps({
                "id": "q1",
                "answer": "yes",
                "other_text": "  ",
                "note": " \t ",
            }).encode())
        self.assertEqual(status, 200)
        record = json.loads(self.answer_lines()[0])
        self.assertEqual(record["other_text"], "")
        self.assertEqual(record["note"], "")
        self.assertEqual(self.questions()[0]["answer_summary"], "yes")


class AskLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_ask_waits_for_the_queue_lock_and_appends(self):
        with open(ask.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{"id": "existing", "status": "open"}], fh)
            fh.write("\n")

        held = open(ask.LOCK_FILE, "w")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX)

        done = threading.Event()
        errors = []

        def post():
            old = sys.argv
            sys.argv = [
                "ask.py", "--project", "loan", "--type", "yesno",
                "--multi", "--q", "Ready to ship?",
            ]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    ask.main()
            except Exception as exc:
                errors.append(exc)
            finally:
                sys.argv = old
                done.set()

        thread = threading.Thread(target=post)
        thread.start()
        time.sleep(0.3)
        self.assertFalse(done.is_set(), "ask.py wrote while the queue lock was held")
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            self.assertEqual([item["id"] for item in json.load(fh)], ["existing"])

        fcntl.flock(held, fcntl.LOCK_UN)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(done.is_set())

        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual([item["id"] for item in saved], ["existing", saved[1]["id"]])
        self.assertEqual(saved[1]["type"], "yesno")
        self.assertEqual(saved[1]["project"], "LOAN")
        self.assertEqual(saved[1]["options"], [])
        self.assertEqual(saved[1]["status"], "open")
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))


if __name__ == "__main__":
    unittest.main()
