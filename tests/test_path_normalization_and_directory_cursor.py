"""Routing and cursor boundaries.

Unnormalized paths must not serve the queue or record an answer. A cursor
that is a directory must not be treated as "no cursor" and dump the ledger.
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


class PathNormalizationTest(unittest.TestCase):
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
            }
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

    def test_unnormalized_paths_do_not_serve_or_answer(self):
        key = quote(self.token, safe="")
        blocked = [
            ("internal-slash", "/api//questions?key=%s" % key),
            ("case", "/API/questions?key=%s" % key),
            ("encoded-slash", "/api%%2Fquestions?key=%s" % key),
            ("encoded-slash-lower", "/api%%2fquestions?key=%s" % key),
            ("dot-segment", "/./api/questions?key=%s" % key),
        ]
        for label, path in blocked:
            status, body, headers = self.request("GET", path)
            text = body.decode("utf-8", "replace")
            self.assertEqual(status, 404, label)
            self.assertEqual(headers.get("connection"), "close", label)
            self.assertNotIn(b"SENTINEL-QUESTION-TEXT", body)
            self.assertNotIn(self.token, text)
            self.assertNotIn(b"yes", body)

        forged = json.dumps({
            "id": "q1",
            "answer": "yes",
            "note": "NOTE-SECRET",
            "project": "FORGED-PROJECT",
        }).encode()
        status, body, headers = self.request(
            "POST", "/api//answer?key=%s" % key, forged)
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("connection"), "close")
        self.assertFalse(os.path.exists(server.ANSWERS_FILE))
        with open(server.QUESTIONS_FILE, "rb") as fh:
            self.assertEqual(fh.read(), self.original_queue)
        self.assertNotIn(b"NOTE-SECRET", body)
        self.assertNotIn(self.token, body.decode("utf-8", "replace"))

        ok = json.dumps({"id": "q1", "answer": "yes"}).encode()
        status, body, headers = self.request(
            "POST", "/api/answer?key=%s" % key, ok)
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("connection"))
        with open(server.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[0]["status"], "answered")
        self.assertEqual(saved[0]["question"], "SENTINEL-QUESTION-TEXT")
        self.assertEqual(saved[0]["project"], "LOAN")
        with open(server.ANSWERS_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["id"], "q1")
        self.assertEqual(row["answer"], "yes")
        self.assertEqual(row["project"], "LOAN")
        self.assertEqual(row["question"], "SENTINEL-QUESTION-TEXT")
        self.assertNotIn("FORGED-PROJECT", lines[0])
        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(self.token, log)
        self.assertNotIn("NOTE-SECRET", log)


class DirectoryCursorTest(unittest.TestCase):
    def test_directory_cursor_does_not_dump_the_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        answers_path = os.path.join(tmp.name, "answers.jsonl")
        cursor_dir = os.path.join(tmp.name, ".answers_cursor")
        os.mkdir(cursor_dir)
        line = json.dumps({
            "ts": "2026-01-02T00:00:00-05:00",
            "id": "q1",
            "project": "LOAN",
            "question": "Ship it?",
            "answer": "yes",
            "other_text": "",
            "note": "LEDGER-SECRET-NOTE",
        }, ensure_ascii=False) + "\n"
        with open(answers_path, "w", encoding="utf-8") as fh:
            fh.write(line)
        with open(answers_path, "rb") as fh:
            before = fh.read()
        code = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import answers\n"
            "answers.ANSWERS_FILE = %r\n"
            "answers.CURSOR_FILE = %r\n"
            "sys.argv = ['answers.py']\n"
            "try:\n"
            "    answers.main()\n"
            "except IsADirectoryError:\n"
            "    sys.stderr.write('RAISED IsADirectoryError\\n')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(2)\n"
        ) % (ROOT, answers_path, cursor_dir)
        proc = subprocess.run(
            [sys.executable, "-W", "error", "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RAISED IsADirectoryError", proc.stderr)
        self.assertNotIn("LEDGER-SECRET-NOTE", proc.stdout)
        self.assertNotIn("Ship it?", proc.stdout)
        self.assertEqual(proc.stdout, "")
        with open(answers_path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(os.listdir(cursor_dir), [])
        self.assertTrue(os.path.isdir(cursor_dir))


if __name__ == "__main__":
    unittest.main()
