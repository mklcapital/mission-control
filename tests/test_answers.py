"""Answer polling: corrupt lines, cursor advancement, and clean stdout."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

import answers


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, answers, "ANSWERS_FILE", answers.ANSWERS_FILE)
        self.addCleanup(setattr, answers, "CURSOR_FILE", answers.CURSOR_FILE)
        answers.ANSWERS_FILE = os.path.join(self.tmp.name, "answers.jsonl")
        answers.CURSOR_FILE = os.path.join(self.tmp.name, ".answers_cursor")

    def write_rows(self, rows):
        with open(answers.ANSWERS_FILE, "w", encoding="utf-8") as fh:
            for row in rows:
                if isinstance(row, str):
                    fh.write(row + "\n")
                else:
                    fh.write(json.dumps(row) + "\n")

    def cursor(self):
        if not os.path.exists(answers.CURSOR_FILE):
            return None
        with open(answers.CURSOR_FILE, encoding="utf-8") as fh:
            return fh.read()

    def run_poll(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["answers.py", *args]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                answers.main()
        finally:
            sys.argv = old
        return stdout.getvalue(), stderr.getvalue()

    def test_skips_corrupt_lines_and_keeps_stdout_parseable(self):
        self.write_rows([
            {"ts": "2026-01-02T00:00:00", "id": "a", "answer": "yes"},
            "{not json",
            "",
            {"ts": "2026-01-03T00:00:00", "id": "b", "answer": "no"},
        ])
        stdout, stderr = self.run_poll("--all")
        self.assertEqual(stderr, "")
        parsed = [json.loads(ln) for ln in stdout.splitlines()]
        self.assertEqual([row["id"] for row in parsed], ["a", "b"])
        self.assertIsNone(self.cursor())

    def test_cursor_advances_to_max_timestamp_and_does_not_replay(self):
        self.write_rows([
            {"ts": "2026-01-01T00:00:00", "id": "early"},
            {"ts": "2026-01-05T00:00:00", "id": "newest"},
            {"ts": "2026-01-03T00:00:00", "id": "middle"},
        ])
        stdout, stderr = self.run_poll()
        self.assertEqual(stderr, "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.splitlines()],
            ["early", "newest", "middle"],
        )
        self.assertEqual(self.cursor(), "2026-01-05T00:00:00\n")

        stdout, stderr = self.run_poll()
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "# no new answers\n")
        self.assertEqual(self.cursor(), "2026-01-05T00:00:00\n")

        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("2026-01-03T00:00:00\n")
        stdout, stderr = self.run_poll()
        self.assertEqual(stderr, "")
        self.assertEqual(
            [json.loads(ln)["id"] for ln in stdout.splitlines()],
            ["newest"],
        )
        self.assertEqual(self.cursor(), "2026-01-05T00:00:00\n")

    def test_peek_and_since_do_not_move_the_cursor(self):
        self.write_rows([
            {"ts": "2026-01-02T00:00:00", "id": "old"},
            {"ts": "2026-01-04T00:00:00", "id": "new"},
        ])
        with open(answers.CURSOR_FILE, "w", encoding="utf-8") as fh:
            fh.write("2026-01-02T00:00:00\n")

        stdout, stderr = self.run_poll("--peek")
        self.assertEqual(stderr, "")
        self.assertEqual([json.loads(ln)["id"] for ln in stdout.splitlines()], ["new"])
        self.assertEqual(self.cursor(), "2026-01-02T00:00:00\n")

        stdout, stderr = self.run_poll("--since", "2026-01-04T00:00:00")
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "# no new answers\n")
        self.assertEqual(self.cursor(), "2026-01-02T00:00:00\n")

        stdout, _stderr = self.run_poll("--since", "2026-01-03T00:00:00")
        self.assertEqual([json.loads(ln)["id"] for ln in stdout.splitlines()], ["new"])
        self.assertEqual(self.cursor(), "2026-01-02T00:00:00\n")

    def test_missing_file_is_an_empty_poll(self):
        stdout, stderr = self.run_poll()
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "# no new answers\n")
        self.assertIsNone(self.cursor())


if __name__ == "__main__":
    unittest.main()
