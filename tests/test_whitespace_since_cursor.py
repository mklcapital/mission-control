"""A whitespace --since still filters, and does not move the poll cursor.

Stdlib only. No server.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

import answers


class WhitespaceSinceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")

    def test_whitespace_since_returns_dated_rows_and_leaves_the_cursor(self):
        # The cursor is already past `older`. If a blank --since were treated as
        # "no --since", only `newer` would print and the cursor would advance.
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            for row in (
                {"ts": "2026-01-01T00:00:00", "id": "older", "answer": "no"},
                {"id": "undated", "answer": "skip"},
                {"ts": "2026-01-03T00:00:00", "id": "newer", "answer": "yes"},
            ):
                fh.write(json.dumps(row) + "\n")
        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("2026-01-02T00:00:00\n")

        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py", "--since", " \t "]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old

        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.getvalue().splitlines()],
            ["older", "newer"],
        )
        self.assertNotIn("undated", stdout.getvalue())
        self.assertNotIn("skip", stdout.getvalue())
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-02T00:00:00\n")
