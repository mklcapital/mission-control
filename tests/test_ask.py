"""Question intake: option parsing, id collisions, and stored shape."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

import ask


class SlugTests(unittest.TestCase):
    def test_slugify_strips_punctuation_and_caps_words(self):
        self.assertEqual(ask.slugify("What's up?"), "whats-up")
        self.assertEqual(ask.slugify("???"), "q")
        self.assertEqual(
            ask.slugify("one two three four five six"),
            "one-two-three-four-five",
        )


class PostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def run_ask(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        old = sys.argv
        sys.argv = ["ask.py", *args]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ask.main()
        finally:
            sys.argv = old
        return stdout.getvalue().strip(), stderr.getvalue()

    def questions(self):
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def test_posts_normalized_choice_question(self):
        qid, _err = self.run_ask(
            "--project", "loan",
            "--type", "choice",
            "--multi",
            "--q", "What's up?",
            "--opt", " A | B | | C ",
            "--note", "context",
        )
        self.assertTrue(qid.endswith("-whats-up"))
        saved = self.questions()
        self.assertEqual(len(saved), 1)
        item = saved[0]
        self.assertEqual(item["id"], qid)
        self.assertEqual(item["project"], "LOAN")
        self.assertEqual(item["type"], "multi")
        self.assertEqual(item["question"], "What's up?")
        self.assertEqual(item["options"], ["A", "B", "C"])
        self.assertEqual(item["context"], "context")
        self.assertEqual(item["status"], "open")
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))

    def test_rejects_options_that_do_not_match_the_type(self):
        cases = (
            ("--type", "choice", "--q", "Pick?", "--project", "LOAN"),
            ("--type", "yesno", "--q", "Ready?", "--opt", "A|B", "--project", "LOAN"),
            ("--type", "text", "--q", "Notes?", "--opt", "A", "--project", "LOAN"),
            ("--type", "dropdown", "--q", "Which?", "--project", "LOAN"),
        )
        for args in cases:
            stdout = io.StringIO()
            old = sys.argv
            sys.argv = ["ask.py", *args]
            try:
                with self.assertRaises(SystemExit) as caught:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                        ask.main()
            finally:
                sys.argv = old
            self.assertEqual(caught.exception.code, 2, args)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE))

    def test_same_second_collision_gets_a_distinct_id(self):
        class FrozenNow:
            def astimezone(self):
                return self

            def strftime(self, fmt):
                self.fmt = fmt
                return "20261012-150000"

            def isoformat(self, timespec="seconds"):
                return "2026-10-12T15:00:00+00:00"

        class FrozenDateTime:
            @staticmethod
            def now():
                return FrozenNow()

        self.addCleanup(setattr, ask, "datetime", ask.datetime)
        self.addCleanup(setattr, ask.time, "time", ask.time.time)
        ask.datetime = FrozenDateTime
        ask.time.time = lambda: 1700000000.0

        first, _err = self.run_ask(
            "--project", "LOAN", "--type", "yesno", "--q", "Order the appraisal now?")
        second, _err = self.run_ask(
            "--project", "LOAN", "--type", "yesno", "--q", "Order the appraisal now?")
        self.assertEqual(first, "20261012-150000-order-the-appraisal-now")
        self.assertEqual(second, first + "-000")
        ids = [item["id"] for item in self.questions()]
        self.assertEqual(ids, [first, second])
        self.assertEqual(self.questions()[0]["options"], [])
        self.assertEqual(self.questions()[1]["type"], "yesno")


if __name__ == "__main__":
    unittest.main()
