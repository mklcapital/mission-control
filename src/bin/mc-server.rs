//! Mission Control server — an async decision queue for human-in-the-loop AI
//! work. Rust reimplementation of `server.py`, sharing the identical on-disk
//! protocol so it serves the existing `index.html` and interoperates with the
//! `ask` / `answers` tools unchanged.
//!
//! Routes:
//!   GET  /                → the page (index.html)
//!   GET  /api/questions   → questions.json (the queue)
//!   POST /api/answer      → {id, answer, other_text, note}
//!   GET  /health          → liveness probe (no key needed, no data exposed)
//!
//! Every data route requires ?key=<token>. Binds 0.0.0.0:8321. NEVER
//! port-forward this to the internet — see README.md.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::sync::{Arc, Mutex};

use constant_time_eq::constant_time_eq;
use serde_json::{json, Value};
use tiny_http::{Header, Method, Request, Response, Server};

use mission_control::mc;

const PORT: u16 = 8321;
const MAX_BODY: usize = 64 * 1024;
const NUM_WORKERS: usize = 8;

/// Shared, immutable server context handed to every worker thread.
struct Ctx {
    base: std::path::PathBuf,
    token: String,
    /// Process-wide guard paired with the cross-process `flock` in
    /// `mc::QueueLock`, mirroring server.py's thread-lock + fcntl.flock combo.
    qlock: Mutex<()>,
}

fn log_line(base: &Path, msg: &str) {
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(base.join("server.log")) {
        let _ = writeln!(f, "{} {}", mc::now_iso(), msg);
    }
}

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
        h.push(header("Connection", "close"));
    }
    h
}

fn respond(request: Request, code: u16, body: Vec<u8>, ctype: &str) {
    let is_head = *request.method() == Method::Head;
    let headers = base_headers(ctype, code);
    if is_head {
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

fn redact_key(url: &str) -> String {
    match url.split_once("key=") {
        Some((head, tail)) => match tail.splitn(2, '&').nth(1) {
            Some(r) => format!("{}key=REDACTED&{}", head, r),
            None => format!("{}key=REDACTED", head),
        },
        None => url.to_string(),
    }
}

fn handle(request: Request, ctx: &Ctx) {
    let url = request.url().to_string();
    let path = url.splitn(2, '?').next().unwrap_or("/").to_string();
    let method = request.method().clone();

    let addr = request.remote_addr().map(|a| a.to_string()).unwrap_or_else(|| "-".into());
    log_line(&ctx.base, &format!("{} {} {}", addr, method, redact_key(&url)));

    if path == "/health" {
        respond_json(request, 200, &json!({"ok": true, "ts": mc::now_iso()}));
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
                    let _g = ctx.qlock.lock().unwrap_or_else(|e| e.into_inner());
                    let _lk = match mc::QueueLock::acquire(&ctx.base) {
                        Ok(l) => l,
                        Err(_) => {
                            respond_json(request, 500, &json!({"error": "lock failed"}));
                            return;
                        }
                    };
                    mc::read_questions(&ctx.base)
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

    let ts = mc::now_iso();
    let response: (u16, Value) = {
        let _g = ctx.qlock.lock().unwrap_or_else(|e| e.into_inner());
        let _lk = match mc::QueueLock::acquire(&ctx.base) {
            Ok(l) => l,
            Err(_) => {
                respond_json(request, 500, &json!({"error": "lock failed"}));
                return;
            }
        };
        let mut questions = mc::read_questions(&ctx.base);
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
            Some(i) if arr[i].get("status").and_then(|v| v.as_str()) == Some("answered") => (
                409,
                json!({
                    "error": "already answered",
                    "answer_summary": arr[i].get("answer_summary").and_then(|v| v.as_str()).unwrap_or(""),
                    "answered_ts": arr[i].get("answered_ts").and_then(|v| v.as_str()).unwrap_or(""),
                }),
            ),
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
                if mc::append_answer(&ctx.base, &record).is_err() {
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
                if mc::write_questions_atomic(&ctx.base, &questions).is_err() {
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

fn main() {
    let base = mc::base_dir();
    let token = mc::ensure_token(&base).expect("could not read/create token.txt");
    let ctx = Arc::new(Ctx { base: base.clone(), token, qlock: Mutex::new(()) });

    let server = Arc::new(Server::http(("0.0.0.0", PORT)).unwrap_or_else(|e| {
        eprintln!("failed to bind 0.0.0.0:{PORT}: {e}");
        std::process::exit(1);
    }));
    log_line(&base, &format!("SERVER START pid={} port={}", std::process::id(), PORT));
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
