#!/usr/bin/env python3
"""ask.py — post a question to Mission Control (Claude/agent side).

Usage:
  ask.py --project LOAN --type choice --q "Which option?" --opt "A|B|C" [--note "context"]
  ask.py --project INFRA --type yesno --q "Rebooted yet?"
  ask.py --project LTM --type text --q "Anything to add?"
  ask.py --project FAB --type choice --multi --q "Pick all that apply" --opt "X|Y|Z"

Types: yesno | choice | multi | dropdown | text
  --multi upgrades choice -> multi (checkboxes).
  The web page ALWAYS adds an "Other" fill-in row to choice/multi/dropdown,
  so do not include Other in --opt.

Prints the new question id on success.
"""

import argparse
import fcntl
import json
import os
import re
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS_FILE = os.path.join(BASE, "questions.json")
LOCK_FILE = os.path.join(BASE, ".queue.lock")

VALID_TYPES = ("yesno", "choice", "multi", "dropdown", "text")


def slugify(text, maxwords=5):
    words = re.sub(r"[^a-z0-9 ]+", "", text.lower()).split()[:maxwords]
    return "-".join(words) or "q"


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True, help="project chip, e.g. LOAN")
    p.add_argument("--type", required=True, choices=VALID_TYPES, dest="qtype")
    p.add_argument("--q", required=True, dest="question", help="question text (verbatim)")
    p.add_argument("--opt", default="", help="pipe-separated options: 'A|B|C'")
    p.add_argument("--multi", action="store_true", help="checkboxes instead of radios")
    p.add_argument("--note", default="", help="context line shown under the question")
    a = p.parse_args()

    qtype = "multi" if (a.multi and a.qtype == "choice") else a.qtype
    options = [o.strip() for o in a.opt.split("|") if o.strip()]
    if qtype in ("choice", "multi", "dropdown") and not options:
        p.error("--opt is required for choice/multi/dropdown")
    if qtype in ("yesno", "text") and options:
        p.error("--opt makes no sense for yesno/text")

    now = datetime.now().astimezone()
    qid = now.strftime("%Y%m%d-%H%M%S") + "-" + slugify(a.question)
    item = {
        "id": qid,
        "project": a.project.upper(),
        "type": qtype,
        "question": a.question,
        "options": options,
        "context": a.note,
        "status": "open",
        "created_ts": now.isoformat(timespec="seconds"),
    }

    with open(LOCK_FILE, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        questions = []
        if os.path.exists(QUESTIONS_FILE):
            with open(QUESTIONS_FILE) as f:
                questions = json.load(f)
        if any(x.get("id") == qid for x in questions):
            qid = "%s-%03d" % (qid, int(time.time() * 1000) % 1000)
            item["id"] = qid
        questions.append(item)
        tmp = QUESTIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(questions, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, QUESTIONS_FILE)

    print(qid)


if __name__ == "__main__":
    main()
