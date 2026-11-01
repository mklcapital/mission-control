"""Choice options keep interior spaces, and the answer cursor is a string compare.

Stdlib only. No server is started.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

import answers
import ask


class SpacedOptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_choice_options_keep_interior_spaces(self):
        # Splitting on whitespace as well as `|` would turn two wings into four
        # buttons. `--multi` is not set, so the type stays choice.
        stdout = io.StringIO()
        old = sys.argv
        sys.argv = [
            "ask.py", "--project", "loan", "--type", "choice",
            "--q", "Which wing?", "--opt", "North wing|South wing",
        ]
        try:
            with contextlib.redirect_stdout(stdout):
                ask.main()
        finally:
            sys.argv = old
        qid = stdout.getvalue().strip()
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved), 1)
        item = saved[0]
        self.assertEqual(item["id"], qid)
        self.assertEqual(item["type"], "choice")
        self.assertEqual(item["project"], "LOAN")
        self.assertEqual(item["question"], "Which wing?")
        self.assertEqual(item["options"], ["North wing", "South wing"])
        self.assertNotIn("North", item["options"])
        self.assertNotIn("wing", item["options"])
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))


class DateCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")

    def test_a_full_timestamp_cursor_hides_a_date_only_row(self):
        # Comparison is lexical. `2026-01-02` is a prefix of the cursor, so it
        # is older, not "that whole day". The next calendar day is still new.
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": "2026-01-02", "id": "date-only", "answer": "yes",
            }) + "\n")
            fh.write(json.dumps({
                "ts": "2026-01-03T00:00:00", "id": "next-day", "answer": "no",
            }) + "\n")
        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("2026-01-02T03:04:05\n")

        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py"]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old

        self.assertEqual(stderr.getvalue(), "")
        rows = [json.loads(ln) for ln in stdout.getvalue().splitlines()]
        self.assertEqual([row["id"] for row in rows], ["next-day"])
        self.assertNotIn("date-only", stdout.getvalue())
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "2026-01-03T00:00:00\n")
        with open(answers.ANSWERS_FILE, encoding="utf-8") as fh:
            self.assertIn("date-only", fh.read())


if __name__ == "__main__":
    unittest.main()
