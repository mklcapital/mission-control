"""Ledger integrity and the POST-only answer route.

An answer containing a NUL must stay one JSONL record that answers.py can
read back. HEAD /api/answer must not record an answer.
"""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server


class NulAnswerAndHeadRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        server.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        server.LOG_FILE = os.path.join(self.tmp.name, "server.log")
        server.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")
        server.INDEX_FILE = os.path.join(self.tmp.name, "index.html")
        self.queue = [
            {
                "id": "q1",
                "project": "LOAN",
                "question": "SENTINEL-QUESTION-TEXT",
                "status": "open",
                "options": ["yes", "no"],
            },
            {
                "id": "q2",
                "project": "FAB",
                "question": "SIBLING-STAYS-OPEN",
                "status": "open",
            },
        ]
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.queue, fh)
        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.original_queue = fh.read()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.token = server.TOKEN

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        last = None
        for _ in range(50):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                headers = {}
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                payload = resp.read()
                status = resp.status
                header_map = {k.lower(): v for k, v in resp.getheaders()}
                conn.close()
                return status, payload, header_map
            except (ConnectionRefusedError, ConnectionResetError) as exc:
                last = exc
                conn.close()
                time.sleep(0.02)
        raise AssertionError("server did not accept a connection: %r" % (last,))

    def test_nul_answer_stays_one_record_and_head_does_not_answer(self):
        key = quote(self.token, safe="")
        status, body, headers = self.request("HEAD", "/api/answer?key=%s" % key)
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("connection"), "close")
        self.assertEqual(headers.get("content-length"), str(len(b'{"error": "not found"}')))
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.assertEqual(fh.read(), self.original_queue)

        answer = "yes\x00tail"
        payload = json.dumps({
            "id": "q1",
            "answer": answer,
            "project": "FORGED-PROJECT",
            "question": "FORGED-QUESTION",
        }).encode()
        self.assertNotIn(b"\x00", payload)
        status, body, headers = self.request(
            "POST", "/api/answer?key=%s" % key, payload)
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("connection"))
        self.assertNotIn(self.token, body.decode("utf-8"))

        with open(server.ANSWERS_FILE, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\x00", raw)
        lines = raw.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("\\u0000", lines[0])
        row = json.loads(lines[0])
        self.assertEqual(row["answer"], answer)
        self.assertEqual(row["id"], "q1")
        self.assertEqual(row["project"], "LOAN")
        self.assertEqual(row["question"], "SENTINEL-QUESTION-TEXT")
        self.assertNotIn("FORGED-PROJECT", lines[0])
        self.assertNotIn("FORGED-QUESTION", lines[0])
        self.assertNotIn(self.token, lines[0])

        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["answer_summary"], answer)
        self.assertEqual(saved[1]["status"], "open")
        self.assertEqual(saved[1]["id"], "q2")

        again = json.dumps({"id": "q1", "answer": "no"}).encode()
        status, body, headers = self.request(
            "POST", "/api/answer?key=%s" % key, again)
        self.assertEqual(status, 409)
        self.assertEqual(headers.get("connection"), "close")
        self.assertIn(answer, json.loads(body.decode("utf-8"))["answer_summary"])
        with open(server.ANSWERS_FILE, "rb") as fh:
            self.assertEqual(fh.read(), raw)

        with open(server.LOG_FILE, "rb") as fh:
            log = fh.read()
        self.assertIn(b"key=REDACTED", log)
        self.assertNotIn(self.token.encode(), log)
        answer_lines = [ln for ln in log.splitlines() if b"ANSWER " in ln]
        self.assertEqual(len(answer_lines), 1)
        self.assertIn(b"yes\x00tail", answer_lines[0])
        self.assertNotIn(b"FORGED-PROJECT", answer_lines[0])

        cursor = os.path.join(self.tmp.name, ".answers_cursor")
        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import answers\n"
            "answers.ANSWERS_FILE = %r\n"
            "answers.CURSOR_FILE = %r\n"
            "sys.argv = ['answers.py', '--all']\n"
            "answers.main()\n"
        ) % (ROOT, server.ANSWERS_FILE, cursor)
        proc = subprocess.run(
            [sys.executable, "-W", "error", "-c", code],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, b"")
        out_lines = proc.stdout.splitlines()
        self.assertEqual(len(out_lines), 1)
        printed = json.loads(out_lines[0].decode("utf-8"))
        self.assertEqual(printed["answer"], answer)
        self.assertEqual(printed["question"], "SENTINEL-QUESTION-TEXT")
        self.assertNotIn(b"\x00", out_lines[0])
        self.assertFalse(os.path.exists(cursor))


if __name__ == "__main__":
    unittest.main()
