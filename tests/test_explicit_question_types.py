"""Explicit multi and dropdown questions stay those types.

`--multi` upgrades choice only. Passing the type itself must not depend on
that flag, and must not collapse back to choice.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

import ask


class ExplicitTypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

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

    def test_multi_and_dropdown_do_not_require_the_upgrade_flag(self):
        multi_id = self.run_ask(
            "--project", "loan",
            "--type", "multi",
            "--q", "Pick any",
            "--opt", " A | | B ",
        )
        drop_id = self.run_ask(
            "--project", "fab",
            "--type", "dropdown",
            "--q", "Which site?",
            "--opt", "North|South",
            "--note", "wing",
        )
        saved = self.questions()
        self.assertEqual(len(saved), 2)
        multi, drop = saved
        self.assertEqual(multi["id"], multi_id)
        self.assertEqual(multi["type"], "multi")
        self.assertNotEqual(multi["type"], "choice")
        self.assertEqual(multi["project"], "LOAN")
        self.assertEqual(multi["options"], ["A", "B"])
        self.assertEqual(multi["question"], "Pick any")
        self.assertEqual(multi["status"], "open")
        self.assertEqual(drop["id"], drop_id)
        self.assertEqual(drop["type"], "dropdown")
        self.assertEqual(drop["project"], "FAB")
        self.assertEqual(drop["options"], ["North", "South"])
        self.assertEqual(drop["context"], "wing")
        self.assertEqual(drop["question"], "Which site?")
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))
