"""The answer poller prints stored Unicode as itself.

`json.dumps` defaults to ASCII escapes. A client matching on the answer
text would miss `café` if the poller switched to that default.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

import answers


class UnicodeStdoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")

    def test_unicode_answers_are_not_escaped_on_stdout(self):
        row = {
            "ts": "2026-01-02T03:04:05+00:00",
            "id": "q1",
            "answer": "café",
            "note": "résumé",
        }
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py"]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        text = stdout.getvalue()
        self.assertIn("café", text)
        self.assertIn("résumé", text)
        self.assertNotIn("\\u00e9", text)
        parsed = json.loads(text.strip())
        self.assertEqual(parsed["answer"], "café")
        self.assertEqual(parsed["note"], "résumé")
        self.assertEqual(stderr.getvalue(), "")
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-02T03:04:05+00:00\n")
