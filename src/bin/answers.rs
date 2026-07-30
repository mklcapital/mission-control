//! answers — poll Mission Control answers (agent side). Rust port of answers.py.
//!
//! Default run prints answers newer than the cursor (one JSON object per line)
//! and advances the cursor, so each poll only shows what's new:
//!
//!   answers
//!
//! Options:
//!   --peek                        print unread answers WITHOUT advancing the cursor
//!   --since 2026-07-08T00:00:00   everything after a timestamp (cursor untouched)
//!   --all                         entire answer history (cursor untouched)
//!
//! Exit code 0 always; "# no new answers" goes to stderr so stdout stays clean.

use std::io::Write;

use clap::Parser;
use serde_json::Value;

use mission_control::mc;

#[derive(Parser)]
#[command(name = "answers", about = "Poll Mission Control answers")]
struct Cli {
    /// print unread answers WITHOUT advancing the cursor
    #[arg(long)]
    peek: bool,
    /// everything after a timestamp (cursor untouched)
    #[arg(long, default_value = "")]
    since: String,
    /// entire answer history (cursor untouched)
    #[arg(long = "all")]
    all_rows: bool,
}

fn load_rows(base: &std::path::Path) -> Vec<Value> {
    let mut rows = Vec::new();
    if let Ok(text) = std::fs::read_to_string(mc::answers_file(base)) {
        for line in text.lines() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            if let Ok(v) = serde_json::from_str::<Value>(line) {
                rows.push(v);
            }
        }
    }
    rows
}

fn ts_of(row: &Value) -> &str {
    row.get("ts").and_then(|v| v.as_str()).unwrap_or("")
}

fn main() {
    let a = Cli::parse();
    let base = mc::base_dir();
    let cursor_file = base.join(".answers_cursor");

    let rows = load_rows(&base);
    let sel: Vec<&Value> = if a.all_rows {
        rows.iter().collect()
    } else if !a.since.is_empty() {
        rows.iter().filter(|r| ts_of(r) > a.since.as_str()).collect()
    } else {
        let cursor = std::fs::read_to_string(&cursor_file).unwrap_or_default().trim().to_string();
        rows.iter().filter(|r| ts_of(r) > cursor.as_str()).collect()
    };

    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    for r in &sel {
        let _ = writeln!(out, "{}", r.to_string());
    }

    if !sel.is_empty() && !(a.peek || !a.since.is_empty() || a.all_rows) {
        if let Some(newest) = sel.iter().map(|r| ts_of(r)).max() {
            let _ = std::fs::write(&cursor_file, format!("{}\n", newest));
        }
    }

    if sel.is_empty() {
        eprintln!("# no new answers");
    }
}
