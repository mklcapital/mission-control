"""ask.py appends a question without rewriting the rows already in the queue.

Stdlib only. The server is not started.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

import ask


class FrozenNow:
    def astimezone(self):
        return self

    def strftime(self, _fmt):
        return "20261113-100000"

    def isoformat(self, timespec="seconds"):
        return "2026-11-13T10:00:00+00:00"


class FrozenDateTime:
    @staticmethod
    def now():
        return FrozenNow()


class AppendPreservesQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        self.addCleanup(setattr, ask, "datetime", ask.datetime)
        self.addCleanup(setattr, ask.time, "time", ask.time.time)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")
        ask.datetime = FrozenDateTime
        ask.time.time = lambda: 1700000000.0

    def run_ask(self, *args):
        stdout = io.StringIO()
        old = sys.argv
        sys.argv = ["ask.py", *args]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                ask.main()
        finally:
            sys.argv = old
        return stdout.getvalue().strip()

    def questions(self):
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)

    def raw(self):
        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            return fh.read()

    def test_append_keeps_an_answered_sibling_and_internal_option_spaces(self):
        sibling = {
            "id": "q-answered",
            "project": "LOAN",
            "type": "yesno",
            "question": "Already café?",
            "options": ["no-options-but-kept"],
            "context": "keep-me",
            "status": "answered",
            "created_ts": "2026-01-01T00:00:00+00:00",
            "answered_ts": "2026-01-02T00:00:00+00:00",
            "answer_summary": "yes — detail",
            "custom_flag": "KEEP-FLAG",
        }
        with open(ask.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([sibling], fh)

        qid = self.run_ask(
            "--project", "fab",
            "--type", "dropdown",
            "--q", "Which wing?",
            "--opt", " North  wing |South wing ",
        )
        self.assertEqual(qid, "20261113-100000-which-wing")
        saved = self.questions()
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0], sibling)
        item = saved[1]
        self.assertEqual(item["id"], qid)
        self.assertEqual(item["project"], "FAB")
        self.assertEqual(item["type"], "dropdown")
        self.assertEqual(item["question"], "Which wing?")
        self.assertEqual(item["options"], ["North  wing", "South wing"])
        self.assertEqual(item["context"], "")
        self.assertEqual(item["status"], "open")
        self.assertNotIn("answer_summary", item)
        self.assertNotIn("custom_flag", item)
        self.assertNotIn("-000", qid)
        blob = self.raw()
        self.assertIn("café", blob)
        self.assertIn("yes — detail", blob)
        self.assertNotIn("\\u00e9", blob)
        self.assertNotIn("\\u2014", blob)
        self.assertTrue(blob.endswith("\n"))
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))

    def test_distinct_questions_in_the_same_second_do_not_get_a_collision_suffix(self):
        first = self.run_ask(
            "--project", "LOAN", "--type", "dropdown",
            "--q", "Which wing?", "--opt", "A|B",
        )
        second = self.run_ask(
            "--project", "LOAN", "--type", "yesno", "--q", "Ready yet?",
        )
        self.assertEqual(first, "20261113-100000-which-wing")
        self.assertEqual(second, "20261113-100000-ready-yet")
        saved = self.questions()
        self.assertEqual([item["id"] for item in saved], [first, second])
        self.assertEqual(saved[0]["type"], "dropdown")
        self.assertEqual(saved[0]["options"], ["A", "B"])
        self.assertEqual(saved[1]["type"], "yesno")
        self.assertEqual(saved[1]["options"], [])
        self.assertEqual(saved[1]["status"], "open")
        self.assertNotIn("-000", first)
        self.assertNotIn("-000", second)


if __name__ == "__main__":
    unittest.main()
