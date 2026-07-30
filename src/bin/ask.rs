//! ask — post a question to Mission Control (agent side). Rust port of ask.py.
//!
//!   ask --project LOAN --type choice --q "Which option?" --opt "A|B|C" [--note "context"]
//!   ask --project INFRA --type yesno --q "Rebooted yet?"
//!   ask --project LTM --type text --q "Anything to add?"
//!   ask --project FAB --type choice --multi --q "Pick all that apply" --opt "X|Y|Z"
//!
//! Types: yesno | choice | multi | dropdown | text. `--multi` upgrades choice
//! to checkboxes. The web page always adds an "Other" fill-in row to
//! choice/multi/dropdown, so do not include Other in --opt. Prints the new id.

use chrono::Local;
use clap::{Parser, ValueEnum};
use serde_json::{json, Value};

use mission_control::mc;

#[derive(Copy, Clone, PartialEq, Eq, ValueEnum)]
enum QType {
    Yesno,
    Choice,
    Multi,
    Dropdown,
    Text,
}

impl QType {
    fn as_str(self) -> &'static str {
        match self {
            QType::Yesno => "yesno",
            QType::Choice => "choice",
            QType::Multi => "multi",
            QType::Dropdown => "dropdown",
            QType::Text => "text",
        }
    }
}

#[derive(Parser)]
#[command(name = "ask", about = "Post a question to Mission Control")]
struct Cli {
    /// project chip, e.g. LOAN
    #[arg(long, required = true)]
    project: String,
    /// question type
    #[arg(long = "type", value_enum, required = true)]
    qtype: QType,
    /// question text (verbatim)
    #[arg(long = "q", required = true)]
    question: String,
    /// pipe-separated options: 'A|B|C'
    #[arg(long, default_value = "")]
    opt: String,
    /// checkboxes instead of radios
    #[arg(long)]
    multi: bool,
    /// context line shown under the question
    #[arg(long, default_value = "")]
    note: String,
}

fn main() {
    let a = Cli::parse();

    let qtype = if a.multi && a.qtype == QType::Choice { "multi" } else { a.qtype.as_str() };
    let options: Vec<String> =
        a.opt.split('|').map(|s| s.trim()).filter(|s| !s.is_empty()).map(|s| s.to_string()).collect();

    if matches!(qtype, "choice" | "multi" | "dropdown") && options.is_empty() {
        eprintln!("error: --opt is required for choice/multi/dropdown");
        std::process::exit(2);
    }
    if matches!(qtype, "yesno" | "text") && !options.is_empty() {
        eprintln!("error: --opt makes no sense for yesno/text");
        std::process::exit(2);
    }

    let base = mc::base_dir();
    let now = Local::now();
    let mut qid = format!("{}-{}", now.format("%Y%m%d-%H%M%S"), mc::slugify(&a.question));
    let created_ts = now.to_rfc3339_opts(chrono::SecondsFormat::Secs, false);

    let _lk = mc::QueueLock::acquire(&base).expect("failed to acquire queue lock");
    let mut questions = mc::read_questions(&base);
    let arr = questions.as_array_mut().expect("questions.json is not an array");

    if arr.iter().any(|q| q.get("id").and_then(|v| v.as_str()) == Some(qid.as_str())) {
        // id collision (same-second post): disambiguate like ask.py.
        let ms = (now.timestamp_millis() % 1000) as u64;
        qid = format!("{}-{:03}", qid, ms);
    }

    let item: Value = json!({
        "id": qid,
        "project": a.project.to_uppercase(),
        "type": qtype,
        "question": a.question,
        "options": options,
        "context": a.note,
        "status": "open",
        "created_ts": created_ts,
    });
    arr.push(item);
    mc::write_questions_atomic(&base, &questions).expect("failed to write questions.json");

    println!("{}", qid);
}
