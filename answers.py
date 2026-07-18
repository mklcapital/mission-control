#!/usr/bin/env python3
"""answers.py — Claude's polling helper for Mission Control answers.

Default run prints answers newer than the cursor (one JSON object per
line) and advances the cursor, so each poll only shows what's new:

  python3 answers.py

Options:
  --peek                 print unread answers WITHOUT advancing the cursor
  --since 2026-07-08T00:00:00   everything after a timestamp (cursor untouched)
  --all                  entire answer history (cursor untouched)

Exit code 0 always; "# no new answers" goes to stderr so stdout stays
clean for parsing.
"""

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ANSWERS_FILE = os.path.join(BASE, "answers.jsonl")
CURSOR_FILE = os.path.join(BASE, ".answers_cursor")


def load_rows():
    rows = []
    if os.path.exists(ANSWERS_FILE):
        with open(ANSWERS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--peek", action="store_true")
    p.add_argument("--since", default="")
    p.add_argument("--all", action="store_true", dest="all_rows")
    a = p.parse_args()

    rows = load_rows()
    if a.all_rows:
        sel = rows
    elif a.since:
        sel = [r for r in rows if r.get("ts", "") > a.since]
    else:
        cursor = ""
        if os.path.exists(CURSOR_FILE):
            with open(CURSOR_FILE) as f:
                cursor = f.read().strip()
        sel = [r for r in rows if r.get("ts", "") > cursor]

    for r in sel:
        print(json.dumps(r, ensure_ascii=False))

    if sel and not (a.peek or a.since or a.all_rows):
        newest = max(r.get("ts", "") for r in sel)
        with open(CURSOR_FILE, "w") as f:
            f.write(newest + "\n")

    if not sel:
        print("# no new answers", file=sys.stderr)


if __name__ == "__main__":
    main()
