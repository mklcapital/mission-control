//! Shared building blocks for the Mission Control binaries (`mc-server`,
//! `ask`, `answers`). These mirror the primitives the Python prototype used —
//! the same file layout, the same cross-process `flock`, atomic writes, and
//! local-time ISO timestamps — so the Rust tools and any remaining Python
//! tools drive one and the same queue.

pub mod mc {
    use std::fs::{File, OpenOptions};
    use std::io::Write;
    use std::path::{Path, PathBuf};

    use base64::Engine;
    use chrono::SecondsFormat;
    use fs2::FileExt;
    use serde_json::{json, Value};

    /// Base directory holding the runtime files. `MC_BASE` overrides it;
    /// otherwise the current working directory is used (matching how the
    /// Python scripts resolve paths relative to their own location).
    pub fn base_dir() -> PathBuf {
        std::env::var_os("MC_BASE")
            .map(PathBuf::from)
            .unwrap_or_else(|| std::env::current_dir().expect("cwd"))
    }

    pub fn questions_file(base: &Path) -> PathBuf {
        base.join("questions.json")
    }
    pub fn answers_file(base: &Path) -> PathBuf {
        base.join("answers.jsonl")
    }
    pub fn token_file(base: &Path) -> PathBuf {
        base.join("token.txt")
    }
    pub fn lock_file(base: &Path) -> PathBuf {
        base.join(".queue.lock")
    }

    /// Local time with offset, seconds precision — matches Python's
    /// `datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")`.
    pub fn now_iso() -> String {
        chrono::Local::now().to_rfc3339_opts(SecondsFormat::Secs, false)
    }

    #[cfg(unix)]
    pub fn set_owner_only(path: &Path) {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    pub fn set_owner_only(_path: &Path) {}

    /// Read the token, minting a fresh urlsafe one (0600) on first run — the
    /// same shape as `secrets.token_urlsafe(24)` (24 random bytes, base64url,
    /// no padding → 32 chars).
    pub fn ensure_token(base: &Path) -> std::io::Result<String> {
        let path = token_file(base);
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

    /// Cross-process exclusive lock around `questions.json`, compatible with
    /// the Python side's `fcntl.flock` on the same `.queue.lock` file. Held for
    /// the duration of a read-modify-write and released on drop.
    pub struct QueueLock {
        file: File,
    }

    impl QueueLock {
        pub fn acquire(base: &Path) -> std::io::Result<Self> {
            let file = OpenOptions::new().create(true).write(true).open(lock_file(base))?;
            file.lock_exclusive()?;
            Ok(QueueLock { file })
        }
    }

    impl Drop for QueueLock {
        fn drop(&mut self) {
            let _ = self.file.unlock();
        }
    }

    pub fn read_questions(base: &Path) -> Value {
        match std::fs::read(questions_file(base)) {
            Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_else(|_| json!([])),
            Err(_) => json!([]),
        }
    }

    /// Atomic tmp+rename write with fsync, mirroring the Python writer.
    pub fn write_questions_atomic(base: &Path, questions: &Value) -> std::io::Result<()> {
        let final_path = questions_file(base);
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

    /// Append one JSON record to the fsync'd append-only answer log.
    pub fn append_answer(base: &Path, record: &Value) -> std::io::Result<()> {
        let mut f = OpenOptions::new().create(true).append(true).open(answers_file(base))?;
        let mut line = record.to_string();
        line.push('\n');
        f.write_all(line.as_bytes())?;
        f.flush()?;
        f.sync_all()?;
        Ok(())
    }

    /// Slugify for question ids: drop everything but `[a-z0-9 ]` from the
    /// lowercased text, then join the first five whitespace-separated words
    /// with '-' (matches ask.py's `slugify`).
    pub fn slugify(text: &str) -> String {
        let lower = text.to_lowercase();
        let cleaned: String = lower
            .chars()
            .filter(|c| c.is_ascii_digit() || ('a'..='z').contains(c) || *c == ' ')
            .collect();
        let words: Vec<&str> = cleaned.split_whitespace().take(5).collect();
        if words.is_empty() {
            "q".to_string()
        } else {
            words.join("-")
        }
    }
}
