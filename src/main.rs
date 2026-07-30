//! Mission Control server — an async decision queue for human-in-the-loop AI work.
//!
//! Rust reimplementation of the original `server.py` prototype. It speaks the
//! exact same on-disk protocol (token.txt, questions.json, answers.jsonl,
//! .answers_cursor, .queue.lock), so it drops in alongside the existing
//! `index.html` page and the `ask.py` / `answers.py` CLIs without changing any
//! of them — a Rust server and the Python tools can drive the same queue.
//!
//! Routes:
//!   GET  /                → the page (index.html)
//!   GET  /api/questions   → questions.json (the queue)
//!   POST /api/answer      → {id, answer, other_text, note} → appends to
//!                           answers.jsonl and marks the question answered in
//!                           questions.json (atomic tmp+rename write)
//!   GET  /health          → liveness probe (no key needed, no data exposed)
//!
//! Every data route requires ?key=<token>. The token is generated on first run
//! and stored in token.txt (0600). Binds 0.0.0.0:8321. NEVER port-forward this
//! to the internet — see README.md.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use base64::Engine;
use chrono::SecondsFormat;
use constant_time_eq::constant_time_eq;
use fs2::FileExt;
use serde_json::{json, Value};
use tiny_http::{Header, Method, Request, Response, Server};

const PORT: u16 = 8321;
const MAX_BODY: usize = 64 * 1024; // request-body cap for POST /api/answer
const NUM_WORKERS: usize = 8;

/// Shared, immutable server context handed to every worker thread.
struct Ctx {
    base: PathBuf,
    token: String,
    /// Process-wide guard paired with a cross-process flock on `.queue.lock`,
    /// mirroring the Python server's thread-lock + fcntl.flock combination.
    qlock: Mutex<()>,
}

fn base_dir() -> PathBuf {
    // MC_BASE lets you run the binary from anywhere against a chosen queue dir;
    // otherwise the current working directory holds the runtime files (matching
    // how server.py is launched from the repo directory).
    std::env::var_os("MC_BASE")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::current_dir().expect("cwd"))
}

fn now_iso() -> String {
    // Local time with offset, seconds precision — matches Python's
    // datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds").
    chrono::Local::now().to_rfc3339_opts(SecondsFormat::Secs, false)
}

fn log_line(base: &Path, msg: &str) {
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(base.join("server.log")) {
        let _ = writeln!(f, "{} {}", now_iso(), msg);
    }
}

/// Read the token, minting a fresh urlsafe one (0600) on first run — the same
/// shape as Python's `secrets.token_urlsafe(24)` (24 random bytes, base64url,
/// no padding → 32 chars).
fn ensure_token(base: &Path) -> std::io::Result<String> {
    let path = base.join("token.txt");
    if let Ok(s) = std::fs::read_to_string(&path) {
        let t = s.trim().to_string();
        if !t.is_empty() {
            return Ok(t);
        }
    }
    let mut buf = [0u8; 24];
    getrandom::getrandom(&mut buf).expect("getrandom");
    let tok = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(buf);
    let mut f = OpenOptions::new().create(true).write(true).truncate(true).open(&path)?;
    writeln!(f, "{}", tok)?;
    set_owner_only(&path);
    Ok(tok)
}

#[cfg(unix)]
fn set_owner_only(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}
#[cfg(not(unix))]
fn set_owner_only(_path: &Path) {}

/// RAII cross-process + cross-thread lock around questions.json, held for the
/// duration of a read-modify-write. Acquires the process mutex first, then an
/// exclusive flock on `.queue.lock` (compatible with ask.py's fcntl.flock).
struct QueueLock<'a> {
    _guard: std::sync::MutexGuard<'a, ()>,
    file: File,
}

impl<'a> QueueLock<'a> {
    fn acquire(ctx: &'a Ctx) -> std::io::Result<Self> {
        let guard = ctx.qlock.lock().unwrap_or_else(|e| e.into_inner());
        let file = OpenOptions::new()
            .create(true)
            .write(true)
            .open(ctx.base.join(".queue.lock"))?;
        file.lock_exclusive()?;
        Ok(QueueLock { _guard: guard, file })
    }
}

impl Drop for QueueLock<'_> {
    fn drop(&mut self) {
        let _ = self.file.unlock();
    }
}

fn read_questions(base: &Path) -> Value {
    match std::fs::read(base.join("questions.json")) {
        Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_else(|_| json!([])),
        Err(_) => json!([]),
    }
}

fn write_questions_atomic(base: &Path, questions: &Value) -> std::io::Result<()> {
    let final_path = base.join("questions.json");
    let tmp = base.join("questions.json.tmp");
    {
        let mut f = OpenOptions::new().create(true).write(true).truncate(true).open(&tmp)?;
        let mut s = serde_json::to_string_pretty(questions).unwrap_or_else(|_| "[]".into());
        s.push('\n');
        f.write_all(s.as_bytes())?;
        f.flush()?;
        f.sync_all()?;
    }
    std::fs::rename(&tmp, &final_path)
}

/// Parse `?key=…` from a raw URL and constant-time compare it to the token.
fn key_ok(url: &str, token: &str) -> bool {
    let query = url.splitn(2, '?').nth(1).unwrap_or("");
    let mut supplied = String::new();
    for pair in query.split('&') {
        if let Some(v) = pair.strip_prefix("key=") {
            supplied = percent_decode(v);
            break;
        }
    }
    !supplied.is_empty() && constant_time_eq(supplied.as_bytes(), token.as_bytes())
}

/// Minimal percent-decoding for query values (tokens are urlsafe base64, but a
/// browser may still encode nothing/`%`-escapes — handle them faithfully).
fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hi = (bytes[i + 1] as char).to_digit(16);
                let lo = (bytes[i + 2] as char).to_digit(16);
                if let (Some(h), Some(l)) = (hi, lo) {
                    out.push((h * 16 + l) as u8);
                    i += 3;
                    continue;
                }
                out.push(bytes[i]);
                i += 1;
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn header(name: &str, value: &str) -> Header {
    Header::from_bytes(name.as_bytes(), value.as_bytes()).expect("valid header")
}

/// Build the common response headers, mirroring server.py's hardened set.
fn base_headers(ctype: &str, code: u16) -> Vec<Header> {
    let mut h = vec![
        header("Content-Type", ctype),
        header("Cache-Control", "no-store"),
        header("Referrer-Policy", "no-referrer"),
        header("X-Content-Type-Options", "nosniff"),
        header("X-Frame-Options", "DENY"),
    ];
    if ctype.starts_with("text/html") {
        h.push(header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; \
             style-src 'unsafe-inline'; connect-src 'self'; \
             img-src 'self' data:; base-uri 'none'; \
             form-action 'self'; frame-ancestors 'none'",
        ));
    }
    if code >= 400 {
        // Close after errors so an undrained POST body can't desync keep-alive.
        h.push(header("Connection", "close"));
    }
    h
}

fn respond(request: Request, code: u16, body: Vec<u8>, ctype: &str) {
    let is_head = *request.method() == Method::Head;
    let headers = base_headers(ctype, code);
    if is_head {
        // Same headers as GET, but no body; advertise the length explicitly.
        let len = body.len();
        let mut resp = Response::empty(code).with_header(header("Content-Length", &len.to_string()));
        for hd in headers {
            resp.add_header(hd);
        }
        let _ = request.respond(resp);
    } else {
        let mut resp = Response::from_data(body).with_status_code(code);
        for hd in headers {
            resp.add_header(hd);
        }
        let _ = request.respond(resp);
    }
}

fn respond_json(request: Request, code: u16, value: &Value) {
    respond(request, code, value.to_string().into_bytes(), "application/json; charset=utf-8");
}

fn deny(request: Request) {
    respond_json(request, 403, &json!({"error": "bad or missing key"}));
}

fn handle(request: Request, ctx: &Ctx) {
    let url = request.url().to_string();
    let path = url.splitn(2, '?').next().unwrap_or("/").to_string();
    let method = request.method().clone();

    // Redact the key before logging, exactly like the Python access log.
    let logged = redact_key(&url);
    log_line(&ctx.base, &format!("{} {} {}", request.remote_addr().map(|a| a.to_string()).unwrap_or_else(|| "-".into()), method, logged));

    // /health is open (no key, no data).
    if path == "/health" {
        respond_json(request, 200, &json!({"ok": true, "ts": now_iso()}));
        return;
    }

    if !key_ok(&url, &ctx.token) {
        deny(request);
        return;
    }

    match method {
        Method::Get | Method::Head => {
            if path == "/" || path == "/index.html" {
                match std::fs::read(ctx.base.join("index.html")) {
                    Ok(bytes) => respond(request, 200, bytes, "text/html; charset=utf-8"),
                    Err(_) => respond_json(request, 500, &json!({"error": "index.html missing"})),
                }
            } else if path == "/api/questions" {
                let questions = {
                    let _lk = match QueueLock::acquire(ctx) {
                        Ok(l) => l,
                        Err(_) => {
                            respond_json(request, 500, &json!({"error": "lock failed"}));
                            return;
                        }
                    };
                    read_questions(&ctx.base)
                };
                respond_json(request, 200, &questions);
            } else {
                respond_json(request, 404, &json!({"error": "not found"}));
            }
        }
        Method::Post => handle_post(request, ctx, &path),
        _ => respond_json(request, 404, &json!({"error": "not found"})),
    }
}

fn redact_key(url: &str) -> String {
    match url.split_once("key=") {
        Some((head, tail)) => {
            let rest = tail.splitn(2, '&').nth(1);
            match rest {
                Some(r) => format!("{}key=REDACTED&{}", head, r),
                None => format!("{}key=REDACTED", head),
            }
        }
        None => url.to_string(),
    }
}

fn handle_post(mut request: Request, ctx: &Ctx, path: &str) {
    if path != "/api/answer" {
        respond_json(request, 404, &json!({"error": "not found"}));
        return;
    }

    let length = match request
        .headers()
        .iter()
        .find(|h| h.field.equiv("Content-Length"))
        .and_then(|h| h.value.as_str().parse::<i64>().ok())
    {
        Some(n) => n,
        None => {
            respond_json(request, 400, &json!({"error": "bad content-length"}));
            return;
        }
    };
    if length <= 0 {
        respond_json(request, 400, &json!({"error": "bad content-length"}));
        return;
    }
    if length as usize > MAX_BODY {
        respond_json(request, 413, &json!({"error": format!("body too large (max {} bytes)", MAX_BODY)}));
        return;
    }

    let mut buf = vec![0u8; length as usize];
    if request.as_reader().read_exact(&mut buf).is_err() {
        respond_json(request, 400, &json!({"error": "bad body"}));
        return;
    }
    let payload: Value = match serde_json::from_slice(&buf) {
        Ok(v) => v,
        Err(_) => {
            respond_json(request, 400, &json!({"error": "bad json"}));
            return;
        }
    };

    let sget = |k: &str| payload.get(k).and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    let qid = sget("id");
    let answer = sget("answer");
    let other_text = sget("other_text");
    let note = sget("note");
    if qid.is_empty() || answer.is_empty() {
        respond_json(request, 400, &json!({"error": "id and answer required"}));
        return;
    }

    let ts = now_iso();
    let response: (u16, Value) = {
        let _lk = match QueueLock::acquire(ctx) {
            Ok(l) => l,
            Err(_) => {
                respond_json(request, 500, &json!({"error": "lock failed"}));
                return;
            }
        };
        let mut questions = read_questions(&ctx.base);
        let arr = match questions.as_array_mut() {
            Some(a) => a,
            None => {
                respond_json(request, 500, &json!({"error": "corrupt queue"}));
                return;
            }
        };
        let idx = arr.iter().position(|q| q.get("id").and_then(|v| v.as_str()) == Some(qid.as_str()));
        match idx {
            None => (404, json!({"error": "unknown question id"})),
            Some(i) if arr[i].get("status").and_then(|v| v.as_str()) == Some("answered") => {
                // stale tab / second device: never record a conflicting answer
                (
                    409,
                    json!({
                        "error": "already answered",
                        "answer_summary": arr[i].get("answer_summary").and_then(|v| v.as_str()).unwrap_or(""),
                        "answered_ts": arr[i].get("answered_ts").and_then(|v| v.as_str()).unwrap_or(""),
                    }),
                )
            }
            Some(i) => {
                let project = arr[i].get("project").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let question = arr[i].get("question").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let summary = if other_text.is_empty() {
                    answer.clone()
                } else {
                    format!("{} — {}", answer, other_text)
                };
                let record = json!({
                    "ts": ts, "id": qid, "project": project, "question": question,
                    "answer": answer, "other_text": other_text, "note": note,
                });
                if let Err(_) = append_answer(&ctx.base, &record) {
                    respond_json(request, 500, &json!({"error": "write failed"}));
                    return;
                }
                arr[i]["status"] = json!("answered");
                arr[i]["answered_ts"] = json!(ts);
                let answer_summary = if note.is_empty() {
                    summary.clone()
                } else {
                    format!("{} [note: {}]", summary, note)
                };
                arr[i]["answer_summary"] = json!(answer_summary);
                if write_questions_atomic(&ctx.base, &questions).is_err() {
                    respond_json(request, 500, &json!({"error": "write failed"}));
                    return;
                }
                log_line(&ctx.base, &format!("ANSWER {} = {}", qid, summary));
                (200, json!({"ok": true, "ts": ts}))
            }
        }
    };
    respond_json(request, response.0, &response.1);
}

fn append_answer(base: &Path, record: &Value) -> std::io::Result<()> {
    let mut f = OpenOptions::new().create(true).append(true).open(base.join("answers.jsonl"))?;
    let mut line = record.to_string();
    line.push('\n');
    f.write_all(line.as_bytes())?;
    f.flush()?;
    f.sync_all()?;
    Ok(())
}

fn main() {
    let base = base_dir();
    let token = ensure_token(&base).expect("could not read/create token.txt");
    let ctx = Arc::new(Ctx { base: base.clone(), token, qlock: Mutex::new(()) });

    let server = Arc::new(Server::http(("0.0.0.0", PORT)).unwrap_or_else(|e| {
        eprintln!("failed to bind 0.0.0.0:{PORT}: {e}");
        std::process::exit(1);
    }));
    log_line(&base, &format!("SERVER START pid={} port={}", std::process::id(), PORT));
    // stdout must never carry the token (server.log is ingestible by the LTM).
    println!("Mission Control on http://0.0.0.0:{PORT}/?key=<see token.txt>");

    let mut workers = Vec::new();
    for _ in 0..NUM_WORKERS {
        let server = Arc::clone(&server);
        let ctx = Arc::clone(&ctx);
        workers.push(std::thread::spawn(move || loop {
            match server.recv() {
                Ok(request) => handle(request, &ctx),
                Err(_) => break,
            }
        }));
    }
    for w in workers {
        let _ = w.join();
    }
}
