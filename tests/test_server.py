"""Auth, answer-conflict, and log-redaction invariants for the queue server.

These tests bind a localhost server and redirect every data file into a temp
directory. They do not read or write the repo's runtime queue.
"""

import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import quote

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


def raw_request(host, port, raw):
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            buf = sock.recv(4096)
            if not buf:
                break
            chunks.append(buf)
    data = b"".join(chunks)
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, body


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, server, "TOKEN_FILE", server.TOKEN_FILE)
        self.path = os.path.join(self.tmp.name, "token.txt")
        server.TOKEN_FILE = self.path

    def test_fresh_token_is_mode_0600_and_stable(self):
        tok = server.ensure_token()
        self.assertTrue(tok)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), tok + "\n")
        self.assertEqual(server.ensure_token(), tok)

    def test_existing_token_is_reused_not_rotated(self):
        with open(self.path, "w") as fh:
            fh.write("  kept-token \n")
        self.assertEqual(server.ensure_token(), "kept-token")
        with open(self.path) as fh:
            self.assertEqual(fh.read(), "  kept-token \n")

    def test_blank_token_file_is_reminted(self):
        with open(self.path, "w") as fh:
            fh.write(" \n")
        tok = server.ensure_token()
        self.assertTrue(tok.strip())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), tok + "\n")


class QueueTests(unittest.TestCase):
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
        with open(server.INDEX_FILE, "w", encoding="utf-8") as fh:
            fh.write("<html>queue</html>\n")

    def request(self, method, path, body=None, headers=None):
        return http_request(self.host, self.port, method, path, body, headers)

    def authed(self, path):
        sep = "&" if "?" in path else "?"
        return "%s%skey=%s" % (path, sep, quote(TOKEN))

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

    def log_text(self):
        if not os.path.exists(server.LOG_FILE):
            return ""
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_health_is_open_and_leaks_no_queue_data(self):
        self.seed([{"id": "q1", "question": SENTINEL, "status": "open"}])
        status, body, _hdrs = self.request("GET", "/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["ok"], True)
        self.assertNotIn(SENTINEL, body.decode())
        self.assertNotIn(TOKEN, body.decode())
        self.assertNotIn(b"question", body)

    def test_data_routes_reject_missing_and_wrong_keys(self):
        self.seed([{"id": "q1", "question": SENTINEL, "status": "open"}])
        for path in ("/api/questions", "/api/questions?key=", "/api/questions?key=wrong-secret",
                     "/api/questions?key=" + TOKEN[:-1], "/"):
            status, body, hdrs = self.request("GET", path)
            self.assertEqual(status, 403, path)
            self.assertEqual(json.loads(body)["error"], "bad or missing key")
            self.assertNotIn(SENTINEL.encode(), body)
            self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn("wrong-secret", self.log_text())
        self.assertIn("key=REDACTED", self.log_text())

    def test_valid_key_returns_questions_and_redacts_log(self):
        self.seed([{"id": "q1", "question": "café?", "status": "open"}])
        path = "/api/questions?foo=1&key=%s&bar=1" % quote(TOKEN)
        status, body, hdrs = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["question"], "café?")
        self.assertEqual(hdrs["cache-control"], "no-store")
        self.assertEqual(hdrs["x-frame-options"], "DENY")
        self.assertEqual(hdrs["x-content-type-options"], "nosniff")
        self.assertEqual(hdrs["referrer-policy"], "no-referrer")
        log = self.log_text()
        self.assertNotIn(TOKEN, log)
        self.assertIn("key=REDACTED", log)
        self.assertIn("bar=1", log)

        head_status, head_body, head_hdrs = self.request("HEAD", path)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")
        self.assertEqual(head_hdrs["content-length"], hdrs["content-length"])

    def test_page_requires_key_and_sends_csp(self):
        status, body, _hdrs = self.request("GET", "/")
        self.assertEqual(status, 403)
        self.assertNotIn(b"queue", body)

        status, body, hdrs = self.request("GET", self.authed("/"))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<html>queue</html>\n")
        self.assertIn("frame-ancestors 'none'", hdrs["content-security-policy"])
        self.assertTrue(hdrs["content-type"].startswith("text/html"))

        server.INDEX_FILE = os.path.join(self.dir, "missing.html")
        status, body, _hdrs = self.request("GET", self.authed("/"))
        self.assertEqual(status, 500)
        self.assertIn(b"index.html missing", body)

    def test_post_rejects_bad_length_and_payload(self):
        self.seed([{"id": "q1", "status": "open", "question": "Q", "project": "LOAN"}])
        status, body = raw_request(self.host, self.port, (
            "POST /api/answer?key=%s HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % TOKEN
        ).encode())
        self.assertEqual(status, 400)
        self.assertIn(b"bad content-length", body)

        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"), body=b"",
            headers={"Content-Length": "0"})
        self.assertEqual(status, 400)
        self.assertIn(b"bad content-length", body)

        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"), body=b"",
            headers={"Content-Length": "nope"})
        self.assertEqual(status, 400)
        self.assertIn(b"bad content-length", body)

        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"), body=b"{")
        self.assertEqual(status, 400)
        self.assertIn(b"bad json", body)

        for payload in ({"answer": "yes"}, {"id": "q1", "answer": "  "}, {"id": "  ", "answer": "yes"}):
            status, body, _hdrs = self.request(
                "POST", self.authed("/api/answer"),
                body=json.dumps(payload).encode())
            self.assertEqual(status, 400, payload)
            self.assertIn(b"id and answer required", body)
        self.assertEqual(self.answer_lines(), [])
        self.assertEqual(self.questions()[0]["status"], "open")

    def test_oversize_body_is_rejected_before_it_is_applied(self):
        self.seed([{"id": "q1", "status": "open", "question": "Q", "project": "LOAN"}])
        status, body, hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=b"x" * (server.MAX_BODY + 1))
        self.assertEqual(status, 413)
        self.assertIn(b"body too large", body)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertEqual(self.answer_lines(), [])
        self.assertEqual(self.questions()[0]["status"], "open")

        exact = _body_of_length("q1", server.MAX_BODY)
        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"), body=exact)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.answer_lines()), 1)

        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=b"y" * (server.MAX_BODY + 1))
        self.assertEqual(status, 413)
        self.assertEqual(len(self.answer_lines()), 1)

    def test_unknown_question_is_not_recorded(self):
        self.seed([])
        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": "missing", "answer": "yes"}).encode())
        self.assertEqual(status, 404)
        self.assertIn(b"unknown question id", body)
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))

        status, body, _hdrs = self.request(
            "POST", self.authed("/api/nope"),
            body=json.dumps({"id": "missing", "answer": "yes"}).encode())
        self.assertEqual(status, 404)
        self.assertIn(b"not found", body)

    def test_answer_records_stored_question_and_ignores_payload_identity(self):
        self.seed([{
            "id": "q1",
            "status": "open",
            "project": "LOAN",
            "question": "Order the appraisal?",
        }])
        payload = {
            "id": "q1",
            "answer": "Approved",
            "other_text": "suite B",
            "note": "call the lender",
            "project": "EVIL",
            "question": "hijacked",
        }
        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=json.dumps(payload).encode())
        self.assertEqual(status, 200)
        record = json.loads(self.answer_lines()[0])
        self.assertEqual(record["project"], "LOAN")
        self.assertEqual(record["question"], "Order the appraisal?")
        self.assertEqual(record["answer"], "Approved")
        self.assertEqual(record["other_text"], "suite B")
        self.assertEqual(record["note"], "call the lender")
        self.assertNotIn("EVIL", json.dumps(record))

        saved = self.questions()[0]
        self.assertEqual(saved["status"], "answered")
        self.assertEqual(saved["answer_summary"], "Approved — suite B [note: call the lender]")
        self.assertEqual(saved["answered_ts"], record["ts"])
        self.assertFalse(os.path.exists(server.QUESTIONS_FILE + ".tmp"))

        log = self.log_text()
        self.assertNotIn(TOKEN, log)
        answer_logs = [ln for ln in log.splitlines() if "ANSWER " in ln]
        self.assertEqual(len(answer_logs), 1)
        self.assertTrue(answer_logs[0].endswith(" ANSWER q1 = Approved — suite B"))

    def test_second_answer_conflicts_and_does_not_append(self):
        self.seed([{
            "id": "q1",
            "status": "answered",
            "project": "LOAN",
            "question": "Order the appraisal?",
            "answer_summary": "original",
            "answered_ts": "2026-01-01T00:00:00+00:00",
        }])
        with open(server.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            fh.write('{"id": "q1", "answer": "original"}\n')
        status, body, _hdrs = self.request(
            "POST", self.authed("/api/answer"),
            body=json.dumps({"id": "q1", "answer": "conflicting"}).encode())
        self.assertEqual(status, 409)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "already answered")
        self.assertEqual(payload["answer_summary"], "original")
        self.assertEqual(self.answer_lines(), ['{"id": "q1", "answer": "original"}'])
        self.assertEqual(self.questions()[0]["answer_summary"], "original")
        self.assertNotIn("conflicting", self.log_text())

    def test_concurrent_submits_record_one_answer(self):
        self.seed([{
            "id": "q-race",
            "status": "open",
            "project": "LOAN",
            "question": "Ship it?",
        }])
        original = server.read_questions

        def slow_read():
            questions = original()
            import time
            time.sleep(0.2)
            return questions

        server.read_questions = slow_read
        self.addCleanup(setattr, server, "read_questions", original)
        barrier = threading.Barrier(2)
        results = []

        def submit():
            barrier.wait(timeout=5)
            results.append(self.request(
                "POST", self.authed("/api/answer"),
                body=json.dumps({"id": "q-race", "answer": "yes"}).encode()))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        statuses = sorted(status for status, _body, _hdrs in results)
        self.assertEqual(statuses, [200, 409])
        self.assertEqual(len(self.answer_lines()), 1)
        self.assertEqual(json.loads(self.answer_lines()[0])["answer"], "yes")
        self.assertEqual(self.questions()[0]["status"], "answered")


def _body_of_length(qid, target):
    bare = json.dumps({"id": qid, "answer": "yes", "note": ""}).encode()
    pad = target - len(bare)
    if pad < 0:
        raise AssertionError("base payload longer than target: %d" % len(bare))
    body = json.dumps({"id": qid, "answer": "yes", "note": "n" * pad}).encode()
    if len(body) != target:
        raise AssertionError("sized body is %d bytes, wanted %d" % (len(body), target))
    return body


if __name__ == "__main__":
    unittest.main()
