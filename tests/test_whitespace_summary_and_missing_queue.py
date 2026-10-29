"""Whitespace does not decorate an answer, and a missing queue is not created.

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


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def http_request(host, port, method, path, body=None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        payload = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, payload, hdrs
    finally:
        conn.close()


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

    def request(self, method, path, body=None):
        return http_request(self.host, self.port, method, path, body)

    def test_whitespace_other_text_and_note_do_not_decorate_the_summary(self):
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{
                "id": "q1",
                "question": "Order the appraisal?",
                "project": "LOAN",
                "status": "open",
            }], fh)
        payload = {
            "id": "q1",
            "answer": "Approved",
            "other_text": " \n ",
            "note": "\t",
        }
        status, body, _hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN,
            body=json.dumps(payload).encode())
        self.assertEqual(status, 200, body)

        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            record = json.loads(fh.read())
        self.assertEqual(record["answer"], "Approved")
        self.assertEqual(record["other_text"], "")
        self.assertEqual(record["note"], "")
        self.assertEqual(record["question"], "Order the appraisal?")
        self.assertNotIn("\n", record["other_text"])

        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)[0]
        self.assertEqual(saved["status"], "answered")
        self.assertEqual(saved["answer_summary"], "Approved")
        self.assertNotIn("—", saved["answer_summary"])
        self.assertNotIn("note", saved["answer_summary"])

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        answer_logs = [ln for ln in log.splitlines() if "ANSWER " in ln]
        self.assertEqual(len(answer_logs), 1)
        self.assertTrue(answer_logs[0].endswith(" ANSWER q1 = Approved"))
        self.assertNotIn(TOKEN, log)

        status, body, hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN,
            body=json.dumps({"id": "q1", "answer": "Again"}).encode())
        self.assertEqual(status, 409)
        self.assertEqual(hdrs.get("connection"), "close")
        conflict = json.loads(body)
        self.assertEqual(conflict["answer_summary"], "Approved")
        self.assertNotIn("Again", body.decode())
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)

    def test_missing_questions_file_is_not_created_by_a_post(self):
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE))
        status, body, hdrs = self.request(
            "POST", "/api/answer?key=" + TOKEN,
            body=json.dumps({"id": "q1", "answer": "Approved"}).encode())
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "unknown question id")
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE))
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("Approved", log)
        self.assertIn("key=REDACTED", log)


if __name__ == "__main__":
    unittest.main()
