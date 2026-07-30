# AGENTS.md

## Cursor Cloud specific instructions

`mission-control` is a stdlib-only Python HTTP server (`server.py`, port 8321) —
an async human-in-the-loop decision queue. `ask.py` posts questions, `answers.py`
polls answers, `index.html` is the browser UI. Usage is in `README.md`.

Non-obvious notes for this environment:

- **No dependencies / no build step.** Run `python3 server.py` directly; the
  update script installs nothing for this repo.
- **Token-gated.** First run mints `token.txt` (0600). Every data route (the
  page, `/api/questions`, `/api/answer`) requires `?key=<token>`; `/health` is
  open. Open the UI at `http://localhost:8321/?key=$(cat token.txt)`.
- **Runtime files are gitignored** (`questions.json`, `answers.jsonl`,
  `token.txt`, `server.log`, `.answers_cursor`, `.queue.lock`) — they are created
  on first use and hold real questions/answers.
- End-to-end check: `ask.py` post → answer via the UI or
  `POST /api/answer?key=…` → `answers.py` prints it and advances the cursor.

### Project direction: Rust/Swift only

Per the maintainer: this Python app is a **prototype**. The long-term goal is to
port everything to Rust (and Swift for any Apple-platform UI), targeting the
latest iPhone/iPad/macOS technologies. Treat `server.py`/`ask.py`/`answers.py`
as the behavioral spec for the Rust port; write new functionality in Rust/Swift
with a short rationale for the language choice rather than extending the Python.
