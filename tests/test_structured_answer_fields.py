"""A list or object answer is stored as text, not unwrapped into a choice.

Stdlib only. The server binds localhost and every data file is in a temp dir.
"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import server


TOKEN = "unit-test-key"


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


class StructuredAnswerTests(unittest.TestCase):
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
                "options": ["yes", "no"],
            }, {
                "id": "q2",
                "project": "FAB",
                "question": "sibling stays open",
                "status": "open",
            }], fh)

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body, headers)

    def test_list_and_object_fields_are_not_unwrapped(self):
        # str() of a list is not the first choice. An object note must not
        # become its inner value, and the payload cannot rename the question.
        body = json.dumps({
            "id": "q1",
            "answer": ["yes", "no"],
            "other_text": ["wing"],
            "note": {"k": "n"},
            "project": "FORGED-PROJECT",
            "question": "FORGED-QUESTION",
        }).encode()
        path = "/api/answer?key=%s" % quote(TOKEN)
        status, payload, headers = self.request(
            "POST", path, body, {"Content-Type": "application/json", "Content-Length": str(len(body))})
        self.assertEqual(status, 200)
        self.assertNotIn("connection", headers)
        stored = json.loads(payload)
        self.assertEqual(stored["ok"], True)
        answer = "['yes', 'no']"
        other = "['wing']"
        note = "{'k': 'n'}"
        summary = "%s — %s" % (answer, other)
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            queue = json.load(fh)
        self.assertEqual(queue[0]["status"], "answered")
        self.assertEqual(queue[0]["answer_summary"], "%s [note: %s]" % (summary, note))
        self.assertEqual(queue[0]["options"], ["yes", "no"])
        self.assertEqual(queue[0]["project"], "LOAN")
        self.assertEqual(queue[0]["question"], self.question)
        self.assertNotEqual(queue[0]["answer_summary"], "yes")
        self.assertEqual(queue[1]["status"], "open")
        self.assertEqual(queue[1]["question"], "sibling stays open")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln]
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(list(row), ["ts", "id", "project", "question", "answer", "other_text", "note"])
        self.assertIsInstance(row["answer"], str)
        self.assertEqual(row["answer"], answer)
        self.assertNotEqual(row["answer"], "yes")
        self.assertNotEqual(row["answer"], "no")
        self.assertEqual(row["other_text"], other)
        self.assertNotEqual(row["other_text"], "wing")
        self.assertEqual(row["note"], note)
        self.assertNotEqual(row["note"], "n")
        self.assertEqual(row["project"], "LOAN")
        self.assertEqual(row["question"], self.question)
        self.assertNotIn("FORGED-PROJECT", lines[0])
        self.assertNotIn("FORGED-QUESTION", lines[0])
        self.assertNotIn(TOKEN, lines[0])
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertIn("ANSWER q1 = %s" % summary, log)
        self.assertNotIn(" [note:", log)
        self.assertNotIn(note, log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("FORGED-PROJECT", log)
        self.assertNotIn("FORGED-QUESTION", log)

        status, payload, headers = self.request(
            "POST", path, body, {"Content-Type": "application/json", "Content-Length": str(len(body))})
        self.assertEqual(status, 409)
        self.assertEqual(headers.get("connection"), "close")
        conflict = json.loads(payload)
        self.assertEqual(conflict["answer_summary"], "%s [note: %s]" % (summary, note))
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(len([ln for ln in fh.read().splitlines() if ln]), 1)


if __name__ == "__main__":
    unittest.main()
