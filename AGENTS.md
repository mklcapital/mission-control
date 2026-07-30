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

### Project direction (per maintainer)

- **This is a Linux server app and must be Rust only.** This Python
  implementation is a **prototype** — treat `server.py` / `ask.py` /
  `answers.py` as the behavioral spec for a Rust reimplementation; write new
  functionality in Rust, not Python.
- **Swift is only for genuinely native Apple client apps** (macOS / iPhone /
  iPad / Apple Silicon viewers) that talk to this server — never for the server
  itself. All heavy lifting, including GUI rendering, stays server-side and is
  exposed over web tech (and optional Apple-native viewers/browsers).
