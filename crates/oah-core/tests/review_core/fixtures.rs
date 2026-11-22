// 하네스 공용 도구: 고정 v0.4.0 사례 파일을 읽고, 기록 사례가 설명하는 한 커밋짜리 Git 저장소를 임시 폴더에 만들어 로컬 수집기로 검토한다.
//! Shared helpers: frozen v0.4.0 case files, one-commit fixture repositories and reviews of them through the
//! local collector.

use crate::error::Coded;
use crate::paths::Env;
use crate::proc::resolve::{Resolution, Rule, resolve};
use crate::registry::local::LocalGit;
use crate::review::{ReviewError, ReviewRequest, ReviewedSkillV1, review};
use serde_json::{Map, Value, json};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// The clock and commit fields every fixture review uses (v0.4.0 `area_review.py`).
pub const NOW: &str = "2026-01-02T00:00:00+00:00";

/// The frozen v0.4.0 conformance folder.
pub fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../conformance/v0.4.0")
}

pub fn read_json(path: &Path) -> Value {
    let text =
        std::fs::read_to_string(path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|error| panic!("{}: {error}", path.display()))
}

/// One frozen case: its input and the result v0.4.0 gave.
pub struct Case {
    pub input: Value,
    pub expect: Value,
}

pub fn case(area: &str, name: &str) -> Case {
    let folder = root().join(area).join(name);
    Case {
        input: read_json(&folder.join("input.json")),
        expect: read_json(&folder.join("expect.json")),
    }
}

/// The case names an area's MANIFEST lists.
pub fn listed(area: &str) -> Vec<String> {
    let manifest = read_json(&root().join(area).join("MANIFEST.json"));
    let cases = manifest["cases"].as_array().expect("MANIFEST cases");
    cases
        .iter()
        .map(|case| case["name"].as_str().expect("case name").to_string())
        .collect()
}

/// The rows of a table case with their arguments.
pub fn rows(case: &Case) -> &Map<String, Value> {
    case.input["calls"]
        .as_object()
        .expect("a table case has calls")
}

pub fn ok(value: Value) -> Value {
    json!({ "ok": value })
}

/// A refusal as the fixtures record it: the code and the whole exception text.
pub fn refused(error: &ReviewError) -> Value {
    json!({ "error": { "code": error.code(), "message": error.to_string() } })
}

pub fn string_at<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key]
        .as_str()
        .unwrap_or_else(|| panic!("{key} is not a string in {value}"))
}

pub fn strings_at(value: &Value) -> Vec<String> {
    let items = value
        .as_array()
        .unwrap_or_else(|| panic!("{value} is not a list"));
    items
        .iter()
        .map(|item| item.as_str().expect("a string").to_string())
        .collect()
}

/// The first place two JSON values differ, for a readable failure.
pub fn difference(got: &Value, want: &Value, at: &str) -> Option<String> {
    match (got, want) {
        (Value::Object(left), Value::Object(right)) => {
            let mut keys: Vec<&String> = left.keys().chain(right.keys()).collect();
            keys.sort();
            keys.dedup();
            keys.into_iter()
                .find_map(|key| match (left.get(key), right.get(key)) {
                    (Some(left), Some(right)) => difference(left, right, &format!("{at}.{key}")),
                    (left, right) => Some(format!("{at}.{key}: got {left:?}, want {right:?}")),
                })
        }
        (Value::Array(left), Value::Array(right)) if left.len() == right.len() => left
            .iter()
            .zip(right)
            .enumerate()
            .find_map(|(index, (left, right))| difference(left, right, &format!("{at}[{index}]"))),
        _ if got == want => None,
        _ => Some(format!("{at}: got {got}, want {want}")),
    }
}

/// The process environment the local collector starts from, and the absolute git program on its PATH.
pub fn git() -> &'static (Env, PathBuf) {
    static FOUND: OnceLock<(Env, PathBuf)> = OnceLock::new();
    FOUND.get_or_init(|| {
        let env = Env::from_process().expect("the process environment");
        let program = match resolve(&env, "git", Rule::host()) {
            Resolution::Found(found) => PathBuf::from(&found.argv[0]),
            other => panic!("the record cases need git on PATH: {other:?}"),
        };
        (env, program)
    })
}

/// Runs git on a fixture repository with the user's git configuration and inherited git variables kept out;
/// returns its standard output.
pub fn fixture_git(dir: &Path, args: &[&str], extra: &[(&str, &str)]) -> Vec<u8> {
    let (env, program) = git();
    let kept = env.vars().filter(|(name, _)| {
        !name
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
    });
    let null = if cfg!(windows) { "nul" } else { "/dev/null" };
    let output = Command::new(program)
        .env_clear()
        .envs(kept)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", null)
        .envs(extra.iter().copied())
        .arg("-C")
        .arg(dir)
        .args([
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.filemode=false",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .expect("git runs");
    assert!(
        output.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

/// The commit fields of a fixture repository (v0.4.0 `area_review.py` `COMMIT`).
pub fn commit_fields() -> Value {
    json!({"branch": "main", "author": "Fixture", "email": "fixture", "date": "2026-01-01T00:00:00+00:00",
           "message": "skills"})
}

fn author(upstream: &Value) -> [(&str, &str); 6] {
    let (name, email, date) = (
        string_at(upstream, "author"),
        string_at(upstream, "email"),
        string_at(upstream, "date"),
    );
    [
        ("GIT_AUTHOR_NAME", name),
        ("GIT_AUTHOR_EMAIL", email),
        ("GIT_COMMITTER_NAME", name),
        ("GIT_COMMITTER_EMAIL", email),
        ("GIT_AUTHOR_DATE", date),
        ("GIT_COMMITTER_DATE", date),
    ]
}

/// Fixture file bytes: text, or `{text, pad, size}`: the text followed by `pad` up to exactly `size` bytes.
pub fn materialize(value: &Value) -> Vec<u8> {
    if let Some(plain) = value.as_str() {
        return plain.as_bytes().to_vec();
    }
    let (head, pad) = (
        string_at(value, "text").as_bytes(),
        string_at(value, "pad").as_bytes(),
    );
    let size = usize::try_from(value["size"].as_u64().expect("size")).expect("a small size");
    let mut data = head.to_vec();
    while data.len() + pad.len() <= size {
        data.extend_from_slice(pad);
    }
    assert_eq!(data.len(), size, "cannot pad to {size} bytes");
    data
}

/// Writes `files` (path to fixture value) into `dir` and stages them in a new repository on the upstream's branch.
pub fn stage(dir: &Path, upstream: &Value, files: &Map<String, Value>) {
    std::fs::create_dir_all(dir).expect("the repository folder");
    for (path, value) in files {
        let target = dir.join(path);
        std::fs::create_dir_all(target.parent().expect("a parent folder")).expect("a folder");
        std::fs::write(&target, materialize(value)).expect("a fixture file");
    }
    let author = author(upstream);
    fixture_git(
        dir,
        &["init", "-q", "-b", string_at(upstream, "branch")],
        &author,
    );
    fixture_git(dir, &["add", "-A"], &author);
}

/// Commits what is staged with the upstream's author, date and message.
pub fn commit(dir: &Path, upstream: &Value) {
    fixture_git(
        dir,
        &["commit", "-q", "-m", string_at(upstream, "message")],
        &author(upstream),
    );
}

/// Stores `data` as a blob and adds it to the index at `path` with `mode`, without touching the work tree.
pub fn add_entry(repository: &Path, scratch: &Path, mode: &str, path: &str, data: &[u8]) {
    let source = scratch.join(format!("blob-{}", path.replace('/', "-")));
    std::fs::write(&source, data).expect("a blob source");
    let source = source.to_str().expect("a UTF-8 scratch path").to_string();
    let oid = String::from_utf8(fixture_git(
        repository,
        &["hash-object", "-w", &source],
        &[],
    ))
    .expect("an id");
    let entry = format!("{mode},{},{path}", oid.trim());
    fixture_git(
        repository,
        &[
            "-c",
            "core.ignorecase=false",
            "update-index",
            "--add",
            "--cacheinfo",
            &entry,
        ],
        &[],
    );
}

/// The one-commit repository a record case's `upstream` describes.
pub fn upstream(dir: &Path, upstream: &Value) {
    stage(dir, upstream, upstream["files"].as_object().expect("files"));
    commit(dir, upstream);
}

/// A review of `repository` by the local collector with a record case's request fields.
pub fn review_at(repository: &Path, input: &Value) -> Result<ReviewedSkillV1, ReviewError> {
    review_with(repository, input, &git().0)
}

/// [`review_at`] with the collector built from `env`.
pub fn review_with(
    repository: &Path,
    input: &Value,
    env: &Env,
) -> Result<ReviewedSkillV1, ReviewError> {
    let repo = string_at(input, "repo");
    let locator = format!(
        "https://github.com/{}",
        repo.trim_end_matches(".git").to_ascii_lowercase()
    );
    let collector = LocalGit::new(&git().1, repository, locator, env);
    let allow = strings_at(&input["allow"]);
    let request = ReviewRequest {
        repo,
        path: string_at(input, "path"),
        candidate: input["candidate"].as_object().expect("candidate"),
        allow: &allow,
        now: utc(string_at(input, "now")),
    };
    review(&collector, &request)
}

/// A record case: its upstream built in a scratch folder, then reviewed.
pub fn review_case(input: &Value) -> Result<ReviewedSkillV1, ReviewError> {
    let scratch = tempfile::tempdir().expect("a scratch folder");
    let repository = scratch.path().join("upstream");
    upstream(&repository, &input["upstream"]);
    review_at(&repository, input)
}

/// A `YYYY-MM-DDTHH:MM:SS+00:00` instant.
pub fn utc(text: &str) -> SystemTime {
    assert!(
        text.len() == 25 && text.ends_with("+00:00"),
        "{text} is not a UTC instant"
    );
    let field = |from: usize, to: usize| -> i64 { text[from..to].parse().expect("a date field") };
    let (year, month, day) = (field(0, 4), field(5, 7), field(8, 10));
    let seconds = field(11, 13) * 3600 + field(14, 16) * 60 + field(17, 19);
    // Howard Hinnant's days-from-civil.
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year - era * 400;
    let day_of_year = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146_097 + day_of_era - 719_468;
    UNIX_EPOCH
        + Duration::from_secs(
            u64::try_from(days * 86_400 + seconds).expect("an instant after 1970"),
        )
}

/// A record case's request fields for another repository: the fixture clock, MIT allowed, no search hit.
pub fn request(repo: &str, path: &str) -> Value {
    json!({"repo": repo, "path": path, "allow": ["MIT"], "now": NOW,
           "candidate": {"provider": "github", "query_id": null, "hit": null, "seed": null}})
}
