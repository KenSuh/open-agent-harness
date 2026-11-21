// review 기록 사례 어댑터: 사례가 설명하는 한 커밋짜리 합성 Git 저장소를 새 임시 폴더에 만들고, 실제 LocalGit 수집기로 검토해 기록을 직렬화한다.
//! The eleven `review` record cases. Each `upstream` becomes a one-commit repository in a new temporary folder,
//! written by git with the system and global configuration, the user's attribute, ignore and hook files and every
//! inherited `GIT_*` variable kept out, and with no remote. The real bounded read-only collector
//! (`oah_core::registry::local::LocalGit`) then reads it for `oah_core::review::review`, and the run fails if the
//! review changed any byte of the repository. Mapped from `oah-core/tests/review_core/fixtures.rs`.

use super::{ok, only_keys, refused, text, texts};
use crate::runner::safe_relative;
use oah_core::paths::Env;
use oah_core::registry::host_git;
use oah_core::registry::local::LocalGit;
use oah_core::review::{ReviewRequest, review};
use serde_json::{Map, Value as Json};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// The largest fixture file this adapter writes; the review's own file limit is 1 MiB.
const FIXTURE_FILE_LIMIT: u64 = 4 << 20;
const UPSTREAM_KEYS: [&str; 6] = ["author", "branch", "date", "email", "files", "message"];

pub(super) fn run(input: &Map<String, Json>) -> Result<Json, String> {
    only_keys(
        input,
        &["repo", "path", "allow", "now", "candidate", "upstream"],
        "a record case",
    )?;
    let upstream = input
        .get("upstream")
        .and_then(Json::as_object)
        .ok_or("a record case has no upstream object")?;
    let env = Env::from_process()
        .map_err(|error| format!("the process environment: {}", error.message()))?;
    let git = host_git(&env).ok_or("the record cases need git on PATH")?;
    let scratch = tempfile::tempdir()
        .map_err(|error| format!("cannot create a temporary folder: {error}"))?;
    let repository = scratch.path().join("upstream");
    let fixture = Fixture::new(&git, &env, scratch.path())?;
    fixture.build(&repository, upstream)?;

    let repo = text(input, "repo")?;
    let locator = format!(
        "https://github.com/{}",
        repo.trim_end_matches(".git").to_ascii_lowercase()
    );
    let allow = texts(input.get("allow"), "allow")?;
    let candidate = input
        .get("candidate")
        .and_then(Json::as_object)
        .ok_or("a record case has no candidate object")?;
    let request = ReviewRequest {
        repo,
        path: text(input, "path")?,
        candidate,
        allow: &allow,
        now: utc(text(input, "now")?)?,
    };
    let before = files_below(&repository)?;
    let collector = LocalGit::new(&git, &repository, locator, &env);
    let result = review(&collector, &request);
    if files_below(&repository)? != before {
        return Err("reviewing changed the fixture repository".into());
    }
    Ok(match result {
        Ok(record) => ok(record.to_json()),
        Err(error) => refused(&error),
    })
}

/// How fixture repositories are written: git with no personal or system configuration.
struct Fixture<'a> {
    git: &'a Path,
    env: &'a Env,
    /// An empty folder standing in for the home and configuration folders.
    home: PathBuf,
}

impl<'a> Fixture<'a> {
    fn new(git: &'a Path, env: &'a Env, scratch: &Path) -> Result<Fixture<'a>, String> {
        let home = scratch.join("home");
        std::fs::create_dir(&home).map_err(|error| format!("cannot create a folder: {error}"))?;
        Ok(Fixture { git, env, home })
    }

    /// Runs git in `dir` and returns its standard output.
    fn git(&self, dir: &Path, args: &[&str], extra: &[(&str, &str)]) -> Result<Vec<u8>, String> {
        let kept = self.env.vars().filter(|(name, _)| {
            let name = name.to_string_lossy().to_ascii_uppercase();
            !name.starts_with("GIT_")
                && !["HOME", "XDG_CONFIG_HOME", "USERPROFILE"].contains(&name.as_str())
        });
        let null = if cfg!(windows) { "nul" } else { "/dev/null" };
        let output = Command::new(self.git)
            .env_clear()
            .envs(kept)
            .env("HOME", &self.home)
            .env("USERPROFILE", &self.home)
            .env("XDG_CONFIG_HOME", &self.home)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", null)
            .env("GIT_TERMINAL_PROMPT", "0")
            .envs(extra.iter().copied())
            .arg("-C")
            .arg(dir)
            .args(["-c", "core.autocrlf=false", "-c", "core.filemode=false"])
            .args(["-c", "commit.gpgsign=false"])
            .arg("-c")
            .arg(format!("core.attributesFile={null}"))
            .arg("-c")
            .arg(format!("core.excludesFile={null}"))
            .arg("-c")
            .arg(format!("core.hooksPath={null}"))
            .args(args)
            .stdin(Stdio::null())
            .output()
            .map_err(|error| format!("git {args:?} did not start: {error}"))?;
        if !output.status.success() {
            return Err(format!(
                "git {args:?}: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(output.stdout)
    }

    /// The one-commit repository `upstream` describes, at `dir`.
    fn build(&self, dir: &Path, upstream: &Map<String, Json>) -> Result<(), String> {
        only_keys(upstream, &UPSTREAM_KEYS, "upstream")?;
        let field = |key: &str| text(upstream, key).map_err(|error| format!("upstream: {error}"));
        let (name, email, date) = (field("author")?, field("email")?, field("date")?);
        let author = [
            ("GIT_AUTHOR_NAME", name),
            ("GIT_AUTHOR_EMAIL", email),
            ("GIT_COMMITTER_NAME", name),
            ("GIT_COMMITTER_EMAIL", email),
            ("GIT_AUTHOR_DATE", date),
            ("GIT_COMMITTER_DATE", date),
        ];
        let files = upstream
            .get("files")
            .and_then(Json::as_object)
            .ok_or("upstream has no files object")?;
        std::fs::create_dir(dir).map_err(|error| format!("cannot create a folder: {error}"))?;
        for (path, value) in files {
            let target = dir.join(safe_relative(path)?);
            if let Some(parent) = target.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|error| format!("cannot create a folder: {error}"))?;
            }
            std::fs::write(&target, materialize(value)?)
                .map_err(|error| format!("cannot write a fixture file: {error}"))?;
        }
        self.git(dir, &["init", "-q", "-b", field("branch")?], &author)?;
        self.git(dir, &["add", "-A"], &author)?;
        self.git(dir, &["commit", "-q", "-m", field("message")?], &author)?;
        Ok(())
    }
}

/// Fixture file bytes: text, or `{text, pad, size}`: the text followed by `pad` up to exactly `size` bytes.
fn materialize(value: &Json) -> Result<Vec<u8>, String> {
    if let Some(plain) = value.as_str() {
        return Ok(plain.as_bytes().to_vec());
    }
    let padded = value
        .as_object()
        .ok_or("a fixture file is neither text nor a padded text")?;
    only_keys(padded, &["text", "pad", "size"], "a padded fixture file")?;
    let (head, pad) = (
        text(padded, "text")?.as_bytes(),
        text(padded, "pad")?.as_bytes(),
    );
    let size = padded
        .get("size")
        .and_then(Json::as_u64)
        .filter(|size| *size <= FIXTURE_FILE_LIMIT)
        .ok_or("a padded fixture file has no size up to 4 MiB")?;
    let size = usize::try_from(size).map_err(|error| error.to_string())?;
    if pad.is_empty() {
        return Err("a padded fixture file has an empty pad".into());
    }
    let mut data = head.to_vec();
    while data.len() + pad.len() <= size {
        data.extend_from_slice(pad);
    }
    if data.len() != size {
        return Err(format!("cannot pad a fixture file to exactly {size} bytes"));
    }
    Ok(data)
}

/// Every file below `root` (the `.git` folder included) with its bytes.
fn files_below(root: &Path) -> Result<BTreeMap<PathBuf, Vec<u8>>, String> {
    let mut out = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        let entries = std::fs::read_dir(&folder)
            .map_err(|error| format!("cannot list the fixture repository: {error}"))?;
        for entry in entries {
            let path = entry
                .map_err(|error| format!("cannot list the fixture repository: {error}"))?
                .path();
            if path.is_dir() {
                pending.push(path);
            } else {
                let bytes = std::fs::read(&path)
                    .map_err(|error| format!("cannot read the fixture repository: {error}"))?;
                out.insert(path, bytes);
            }
        }
    }
    Ok(out)
}

/// A `YYYY-MM-DDTHH:MM:SS+00:00` instant.
fn utc(text: &str) -> Result<SystemTime, String> {
    let bad = || format!("{text:?} is not a UTC instant");
    let bytes = text.as_bytes();
    if bytes.len() != 25
        || !text.ends_with("+00:00")
        || [4, 7].iter().any(|at| bytes[*at] != b'-')
        || bytes[10] != b'T'
        || [13, 16].iter().any(|at| bytes[*at] != b':')
    {
        return Err(bad());
    }
    let field = |from: usize, to: usize| -> Result<i64, String> {
        let part = &text[from..to];
        if !part.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(bad());
        }
        part.parse().map_err(|_| bad())
    };
    let (year, month, day) = (field(0, 4)?, field(5, 7)?, field(8, 10)?);
    let (hour, minute, second) = (field(11, 13)?, field(14, 16)?, field(17, 19)?);
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return Err(bad());
    }
    // Howard Hinnant's days-from-civil.
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year - era * 400;
    let day_of_year = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146_097 + day_of_era - 719_468;
    let seconds =
        u64::try_from(days * 86_400 + hour * 3600 + minute * 60 + second).map_err(|_| bad())?;
    Ok(UNIX_EPOCH + Duration::from_secs(seconds))
}
