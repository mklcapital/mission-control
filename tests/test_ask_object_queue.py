"""ask.py must not replace a queue that is a JSON object.

stdlib only. The HTTP server is never started.
"""

import fcntl
import os
import sys
import tempfile
import unittest

import ask


class AskObjectQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, ask, "QUESTIONS_FILE", ask.QUESTIONS_FILE)
        self.addCleanup(setattr, ask, "LOCK_FILE", ask.LOCK_FILE)
        ask.QUESTIONS_FILE = os.path.join(self.tmp.name, "questions.json")
        ask.LOCK_FILE = os.path.join(self.tmp.name, ".queue.lock")

    def test_a_json_object_queue_is_left_unchanged(self):
        # A valid object is not a list. append must fail before the temp file
        # is written, and the lock must be releasable afterwards.
        original = '{"id": "q1", "status": "open", "question": "KEEP-ME"}\n'
        with open(ask.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            fh.write(original)
        sys.argv = [
            "ask.py", "--project", "LOAN", "--type", "yesno", "--q", "Ship it?",
        ]
        with self.assertRaises(AttributeError):
            ask.main()

        with open(ask.QUESTIONS_FILE, encoding="utf-8") as fh:
            kept = fh.read()
        self.assertEqual(kept, original)
        self.assertNotIn("Ship it?", kept)
        self.assertNotIn("LOAN", kept)
        self.assertFalse(os.path.exists(ask.QUESTIONS_FILE + ".tmp"))
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.tmp.name)))

        held = open(ask.LOCK_FILE, "a")
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(held, fcntl.LOCK_UN)
        finally:
            held.close()
