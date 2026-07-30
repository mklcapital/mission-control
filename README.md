# Mission Control

An async decision queue for human-in-the-loop AI work. AI agents post questions;
you keep a browser tab open and answer them with radio buttons / checkboxes /
dropdowns / fill-ins; answers land in `answers.jsonl` where the agent reads them
back. Boring, robust, stdlib-only Python — no frameworks, no build step.

## Why

I run a real-estate operation and use AI agents for a lot of the admin around it.
Agents constantly need small decisions from me — "order the appraisal now or after
the term sheet?" — and interrupting whatever I'm doing to answer a chat prompt was
the worst part of the loop. Mission Control decouples that: agents drop questions
into a queue, I answer them in batches from any device on my network, and the
agents pick the answers up on their next poll. It has run every day since I built
it.

**Status:** in-progress, shared to show the approach. It works, I use it daily,
and it is deliberately small.

## How it works

```
agent (CLI)                      you (browser)
   │ ask.py --project LOAN ...      │  answer cards, per-card Submit
   ▼                                ▼
questions.json  ◄────────────  server.py  (stdlib HTTP, port 8321)
   │                                │
   └── status: open → answered      └── appends to answers.jsonl
                                          ▲
agent (CLI): answers.py ──────────────────┘  (cursor in .answers_cursor)
```

| File | Purpose |
|---|---|
| `server.py` | stdlib HTTP server (port 8321). GET `/` page, GET `/api/questions`, POST `/api/answer`, GET `/health` |
| `index.html` | the page — vanilla JS, warm cream/terracotta/teal theme, light+dark, per-card Submit, 60s auto-refresh |
| `ask.py` | CLI for agents to post questions |
| `answers.py` | CLI for agents to poll new answers (cursor in `.answers_cursor`) |

Runtime files (`questions.json`, `answers.jsonl`, `token.txt`, `server.log`,
`.answers_cursor`, `.queue.lock`) are created on first use and are gitignored —
they contain your questions and answers.

## Quickstart

```bash
python3 server.py
# first run mints a token into token.txt (0600); open:
#   http://localhost:8321/?key=<contents of token.txt>
```

### Rust server (the direction of travel)

The server is being reimplemented in Rust (`src/main.rs`, binary `mc-server`).
It speaks the identical on-disk protocol (`token.txt`, `questions.json`,
`answers.jsonl`, `.answers_cursor`, `.queue.lock`), so it serves the same
`index.html` and interoperates with the `ask.py` / `answers.py` CLIs unchanged.

```bash
cargo run --release            # serves on 0.0.0.0:8321, same as server.py
# run against a specific queue directory:
#   MC_BASE=/path/to/dir cargo run --release
```

The Python files remain as the behavioral spec during the migration.

Post a question (from an agent or your shell):

```bash
python3 ask.py --project LOAN --type choice \
  --q "Order the appraisal now?" --opt "A — Yes, now|B — After term sheet" \
  --note "PCA is the longest lead item."
```

Types: `yesno` | `choice` | `multi` | `dropdown` | `text`.
`--multi` upgrades choice to checkboxes. The page always adds an
"Other" fill-in row to choice/multi/dropdown — don't put Other in `--opt`.

Read answers (the agent's ritual — one line at the start of any session):

```bash
python3 answers.py          # prints answers newer than the cursor, advances it
python3 answers.py --peek   # look without advancing
python3 answers.py --all    # full history
```

## Design notes

- **Concurrency-safe by construction**: a cross-process `flock` around
  `questions.json` (server and `ask.py` both take it), atomic tmp+rename writes,
  fsync on the append-only answer log, and a 409 on double-answer so a stale tab
  on a second device can never record a conflicting answer.
- **Token-gated**: every data route requires `?key=<token>`; the token is minted
  on first run (0600) and compared constant-time. The access log redacts it.
- **Hardened headers**: CSP, no-referrer, nosniff, frame-deny, no-store; body-size
  cap on POST; connections closed after errors so an undrained body can't desync
  keep-alive.
- **The page survives refresh races**: cards you are mid-answer on are kept live
  (typed text and selections preserved) while new questions stream in around them.

## Security

The server binds `0.0.0.0:8321` so it is reachable over your LAN or a private
mesh (e.g. Tailscale). It must **never be port-forwarded to the internet** — the
shared-secret `?key=` token is protection against casual LAN snooping, not an
internet-grade auth system.

## Deployment note (macOS)

I run it as a launchd LaunchAgent (`RunAtLoad` + `KeepAlive`) on an always-on
Mac mini. One macOS gotcha worth knowing: TCC folder protection blocks
launchd-spawned processes from reading `~/Documents`, so put the working
directory somewhere like `~/Library/<YourDir>` and symlink it into `~/Documents`
if you want it reachable from there.

## License

MIT — see [LICENSE](LICENSE).
