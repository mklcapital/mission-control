#!/usr/bin/env python3
"""Mission Control server — an async decision queue for human-in-the-loop AI work.

stdlib only, no dependencies. Serves one page, two API routes:

  GET  /                → the page (index.html)
  GET  /api/questions   → questions.json (the queue)
  POST /api/answer      → {id, answer, other_text, note} -> appends to
                          answers.jsonl and marks the question answered
                          in questions.json (atomic tmp+rename write)
  GET  /health          → liveness probe (no key needed, no data exposed)

Every data route requires ?key=<token>. The token is generated on first
run and stored in token.txt (0600).

Binds 0.0.0.0:8321 so it is reachable over LAN / Tailscale.
NEVER port-forward this to the internet. See README.md.
"""

import fcntl
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS_FILE = os.path.join(BASE, "questions.json")
ANSWERS_FILE = os.path.join(BASE, "answers.jsonl")
TOKEN_FILE = os.path.join(BASE, "token.txt")
INDEX_FILE = os.path.join(BASE, "index.html")
LOG_FILE = os.path.join(BASE, "server.log")
LOCK_FILE = os.path.join(BASE, ".queue.lock")
PORT = 8321
MAX_BODY = 64 * 1024  # request-body cap for POST /api/answer

_thread_lock = threading.Lock()


def ensure_token():
    tok = ""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
    if not tok:
        tok = secrets.token_urlsafe(24)
        with open(TOKEN_FILE, "w") as f:
            f.write(tok + "\n")
        os.chmod(TOKEN_FILE, 0o600)
    return tok


TOKEN = ensure_token()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log_line(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write("%s %s\n" % (now_iso(), msg))
    except OSError:
        pass


class queue_lock(object):
    """Cross-process lock (server + ask.py) around questions.json."""

    def __enter__(self):
        _thread_lock.acquire()
        self.fh = open(LOCK_FILE, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        _thread_lock.release()
        return False


def read_questions():
    if not os.path.exists(QUESTIONS_FILE):
        return []
    with open(QUESTIONS_FILE) as f:
        return json.load(f)


def write_questions_atomic(questions):
    tmp = QUESTIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(questions, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, QUESTIONS_FILE)


class Handler(BaseHTTPRequestHandler):
    server_version = "MissionControl/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # never write the access token to the log (server.log is readable,
        # backed up, and a candidate for LTM ingestion)
        msg = re.sub(r"key=[^\s&\"']+", "key=REDACTED", fmt % args)
        log_line("%s %s" % (self.address_string(), msg))

    # ---- helpers --------------------------------------------------------
    def _key_ok(self):
        qs = parse_qs(urlparse(self.path).query)
        supplied = (qs.get("key") or [""])[0]
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if ctype.startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; connect-src 'self'; "
                    "img-src 'self' data:; base-uri 'none'; "
                    "form-action 'self'; frame-ancestors 'none'")
            if code >= 400:
                # close after errors so an undrained POST body can never
                # desync the next request on a kept-alive connection
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _deny(self):
        self._send(403, json.dumps({"error": "bad or missing key"}))

    # ---- routes ---------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, json.dumps({"ok": True, "ts": now_iso()}))
            return
        if not self._key_ok():
            self._deny()
            return
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, json.dumps({"error": "index.html missing"}))
        elif path == "/api/questions":
            with queue_lock():
                qs = read_questions()
            self._send(200, json.dumps(qs, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_HEAD(self):
        # same routing/headers as GET; _send suppresses the body for HEAD
        self.do_GET()

    def do_POST(self):
        if not self._key_ok():
            self._deny()
            return
        if urlparse(self.path).path != "/api/answer":
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._send(400, json.dumps({"error": "bad content-length"}))
            return
        if length <= 0:
            self._send(400, json.dumps({"error": "bad content-length"}))
            return
        if length > MAX_BODY:
            self._send(413, json.dumps(
                {"error": "body too large (max %d bytes)" % MAX_BODY}))
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._send(400, json.dumps({"error": "bad json"}))
            return

        qid = str(payload.get("id") or "").strip()
        answer = str(payload.get("answer") or "").strip()
        other_text = str(payload.get("other_text") or "").strip()
        note = str(payload.get("note") or "").strip()
        if not qid or not answer:
            self._send(400, json.dumps({"error": "id and answer required"}))
            return

        ts = now_iso()
        with queue_lock():
            questions = read_questions()
            q = next((x for x in questions if x.get("id") == qid), None)
            if q is None:
                self._send(404, json.dumps({"error": "unknown question id"}))
                return
            if q.get("status") == "answered":
                # stale tab / second device: never record a conflicting answer
                self._send(409, json.dumps({
                    "error": "already answered",
                    "answer_summary": q.get("answer_summary", ""),
                    "answered_ts": q.get("answered_ts", "")}))
                return
            summary = answer + ((" — " + other_text) if other_text else "")
            record = {
                "ts": ts,
                "id": qid,
                "project": q.get("project", ""),
                "question": q.get("question", ""),
                "answer": answer,
                "other_text": other_text,
                "note": note,
            }
            with open(ANSWERS_FILE, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            q["status"] = "answered"
            q["answered_ts"] = ts
            q["answer_summary"] = summary + ((" [note: %s]" % note) if note else "")
            write_questions_atomic(questions)
        log_line("ANSWER %s = %s" % (qid, summary))
        self._send(200, json.dumps({"ok": True, "ts": ts}))


def main():
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    log_line("SERVER START pid=%d port=%d" % (os.getpid(), PORT))
    # stdout lands in server.log under launchd — never print the token there
    print("Mission Control on http://0.0.0.0:%d/?key=<see token.txt>" % PORT, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
