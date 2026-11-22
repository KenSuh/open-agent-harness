// review 기록 사례 어댑터: 사례가 설명하는 한 커밋짜리 합성 Git 저장소를 새 임시 폴더에 만들고, 실제 LocalGit 수집기로 검토해 기록을 직렬화한다.
//! The eleven `review` record cases. Each `upstream` becomes a one-commit repository in a new temporary folder,
//! validated upfront as an ordinary portable content inventory (reserving Git's administrative namespace across
//! supported hosts), initialized deterministically with empty templates and SHA-1, and built by git with a minimal
//! allowlisted environment, explicit repository arguments (`--git-dir`/`--work-tree`), and local-only settings.
//! The real bounded read-only collector (`oah_core::registry::local::LocalGit`) then reads it for
//! `oah_core::review::review`, and the run fails if the review changes any directory entry or regular file byte
//! of the repository (without following links). Mapped from `oah-core/tests/review_core/fixtures.rs`.

use super::{ok, only_keys, refused, text, texts};
use crate::runner::safe_relative;
use oah_core::paths::Env;
use oah_core::registry::host_git;
use oah_core::registry::local::LocalGit;
use oah_core::review::{ReviewRequest, review};
use serde_json::{Map, Value as Json};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// The largest fixture file this adapter writes; the review's own file limit is 1 MiB.
const FIXTURE_FILE_LIMIT: u64 = 4 << 20;
/// Maximum byte length of one path segment in a record fixture path.
const FIXTURE_SEGMENT_LIMIT: usize = 255;
const UPSTREAM_KEYS: [&str; 6] = ["author", "branch", "date", "email", "files", "message"];

/// File names Windows reserves for devices, in any case and with any extension.
const WINDOWS_DEVICES: [&str; 24] = [
    "con", "prn", "aux", "nul", "com0", "com1", "com2", "com3", "com4", "com5", "com6", "com7",
    "com8", "com9", "lpt0", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
];

/// Host environment variables required for Git to execute reliably on Windows, macOS, and Linux.
const KEPT_HOST_ENV: [&str; 18] = [
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
    "TMPDIR",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
];

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
    let before = inventory_below(&repository)?;
    let collector = LocalGit::new(&git, &repository, locator, &env);
    let result = review(&collector, &request);
    if inventory_below(&repository)? != before {
        return Err("reviewing changed the fixture repository".into());
    }
    Ok(match result {
        Ok(record) => ok(record.to_json()),
        Err(error) => refused(&error),
    })
}

/// How fixture repositories are written: deterministic isolated git with no host templates,
/// no personal or system configuration, a minimal allowlisted environment, and explicit repo args.
struct Fixture<'a> {
    git: &'a Path,
    env: &'a Env,
    /// An empty folder standing in for the home and configuration folders.
    home: PathBuf,
    /// Scratch root used as `GIT_CEILING_DIRECTORIES`.
    ceiling: PathBuf,
}

impl<'a> Fixture<'a> {
    fn new(git: &'a Path, env: &'a Env, scratch: &Path) -> Result<Fixture<'a>, String> {
        let home = scratch.join("home");
        std::fs::create_dir(&home).map_err(|error| format!("cannot create a folder: {error}"))?;
        Ok(Fixture {
            git,
            env,
            home,
            ceiling: scratch.to_path_buf(),
        })
    }

    /// Builds the minimal child environment: allowlisted host variables required for Git on
    /// Windows and macOS, plus isolation overrides and any per-command `extra` variables.
    fn child_env(&self, extra: &[(&str, &str)]) -> Vec<(OsString, OsString)> {
        let null = null_device();
        let mut vars: Vec<(OsString, OsString)> = self
            .env
            .vars()
            .filter(|(name, _)| {
                let upper = name.to_string_lossy().to_ascii_uppercase();
                KEPT_HOST_ENV.contains(&upper.as_str())
            })
            .map(|(name, value)| (name.to_os_string(), value.to_os_string()))
            .collect();
        vars.extend([
            (OsString::from("HOME"), self.home.as_os_str().to_os_string()),
            (
                OsString::from("USERPROFILE"),
                self.home.as_os_str().to_os_string(),
            ),
            (
                OsString::from("XDG_CONFIG_HOME"),
                self.home.as_os_str().to_os_string(),
            ),
            (OsString::from("CURL_HOME"), OsString::from(null)),
            (OsString::from("GIT_CONFIG_NOSYSTEM"), OsString::from("1")),
            (OsString::from("GIT_CONFIG_GLOBAL"), OsString::from(null)),
            (OsString::from("GIT_CONFIG_SYSTEM"), OsString::from(null)),
            (OsString::from("GIT_TEMPLATE_DIR"), OsString::from("")),
            (OsString::from("GIT_TERMINAL_PROMPT"), OsString::from("0")),
            (OsString::from("GIT_ASKPASS"), OsString::from("")),
            (OsString::from("SSH_ASKPASS"), OsString::from("")),
            (OsString::from("GCM_INTERACTIVE"), OsString::from("never")),
            (OsString::from("GIT_ALLOW_PROTOCOL"), OsString::from("")),
            (
                OsString::from("GIT_NO_REPLACE_OBJECTS"),
                OsString::from("1"),
            ),
            (OsString::from("GIT_OPTIONAL_LOCKS"), OsString::from("0")),
        ]);
        let separator = if cfg!(windows) { ';' } else { ':' };
        if !self.ceiling.to_string_lossy().contains(separator) {
            vars.push((
                OsString::from("GIT_CEILING_DIRECTORIES"),
                self.ceiling.as_os_str().to_os_string(),
            ));
        }
        vars.extend(
            extra
                .iter()
                .map(|(k, v)| (OsString::from(*k), OsString::from(*v))),
        );
        vars
    }

    /// Runs git with the minimal isolated environment and local-only configuration flags.
    fn run_git(
        &self,
        cwd: &Path,
        prefix_args: &[OsString],
        args: &[OsString],
        extra: &[(&str, &str)],
    ) -> Result<Vec<u8>, String> {
        let null = null_device();
        let output = Command::new(self.git)
            .env_clear()
            .envs(self.child_env(extra))
            .current_dir(cwd)
            .arg("--literal-pathspecs")
            .args(["-c", "core.autocrlf=false", "-c", "core.filemode=false"])
            .args(["-c", "commit.gpgsign=false"])
            .arg("-c")
            .arg(format!("core.attributesFile={null}"))
            .arg("-c")
            .arg(format!("core.excludesFile={null}"))
            .arg("-c")
            .arg(format!("core.hooksPath={null}"))
            .args(["-c", "core.fsmonitor=false"])
            .args(["-c", "maintenance.auto=false", "-c", "gc.auto=0"])
            .args(["-c", "credential.helper="])
            .args(["-c", "core.askPass="])
            .args(["-c", "init.templateDir="])
            .args(["-c", "http.extraHeader="])
            .args(["-c", "protocol.allow=never"])
            .args(prefix_args)
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

    /// Initializes a fresh SHA-1 repository at `dir` with deterministic empty templates.
    fn git_init(&self, dir: &Path, branch: &str) -> Result<PathBuf, String> {
        let args = [
            OsString::from("init"),
            OsString::from("-q"),
            OsString::from("--template="),
            OsString::from("--object-format=sha1"),
            OsString::from("-b"),
            OsString::from(branch),
            OsString::from("--"),
            dir.as_os_str().to_os_string(),
        ];
        self.run_git(&self.home, &[], &args, &[])?;
        let dot_git = dir.join(".git");
        let meta = std::fs::symlink_metadata(&dot_git)
            .map_err(|error| format!("initialized repository has no .git directory: {error}"))?;
        if meta.file_type().is_symlink() || !meta.file_type().is_dir() {
            return Err("initialized repository .git is not a direct non-symlink directory".into());
        }
        Ok(dot_git)
    }

    /// Runs a git command against the exact scratch repository using explicit `--git-dir` and
    /// `--work-tree` arguments (no upward repository discovery).
    fn git_in_repo(
        &self,
        dir: &Path,
        dot_git: &Path,
        args: &[&str],
        extra: &[(&str, &str)],
    ) -> Result<Vec<u8>, String> {
        let prefix = [
            OsString::from("--git-dir"),
            dot_git.as_os_str().to_os_string(),
            OsString::from("--work-tree"),
            dir.as_os_str().to_os_string(),
        ];
        let cmd_args: Vec<OsString> = args.iter().map(OsString::from).collect();
        self.run_git(dir, &prefix, &cmd_args, extra)
    }

    /// The one-commit repository `upstream` describes, at `dir`.
    fn build(&self, dir: &Path, upstream: &Map<String, Json>) -> Result<(), String> {
        only_keys(upstream, &UPSTREAM_KEYS, "upstream")?;
        let field = |key: &str| text(upstream, key).map_err(|error| format!("upstream: {error}"));
        let (name, email, date, branch, message) = (
            field("author")?,
            field("email")?,
            field("date")?,
            field("branch")?,
            field("message")?,
        );
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

        // Validate the complete file inventory and materialize all payloads before creating any
        // directory, writing any fixture file, or invoking Git.
        let planned = prepare_fixture_files(files)?;

        std::fs::create_dir(dir).map_err(|error| format!("cannot create a folder: {error}"))?;
        for (relative, data) in planned {
            let target = dir.join(&relative);
            if let Some(parent) = target.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|error| format!("cannot create a folder: {error}"))?;
            }
            if std::fs::symlink_metadata(&target).is_ok() {
                return Err(format!(
                    "fixture path {} collided on disk",
                    relative.display()
                ));
            }
            std::fs::write(&target, data)
                .map_err(|error| format!("cannot write a fixture file: {error}"))?;
        }
        let dot_git = self.git_init(dir, branch)?;
        self.git_in_repo(dir, &dot_git, &["add", "-A"], &[])?;
        self.git_in_repo(dir, &dot_git, &["commit", "-q", "-m", message], &author)?;
        Ok(())
    }
}

/// Validates every fixture file path and materializes all file payloads before any write or Git call.
fn prepare_fixture_files(files: &Map<String, Json>) -> Result<Vec<(PathBuf, Vec<u8>)>, String> {
    let validated = validate_fixture_paths(files.keys().map(String::as_str))?;
    let mut planned = Vec::with_capacity(validated.len());
    for (raw, relative) in validated {
        let value = &files[raw];
        planned.push((relative, materialize(value)?));
    }
    Ok(planned)
}

/// Validates the complete record fixture file inventory before any filesystem write or Git call.
///
/// Single path authority — accepted record fixture filename grammar:
/// - Passes the runner's plain-relative path check (`safe_relative`: non-empty, `/`-separated,
///   no `.` or `..`, no `\\` or `:`, every segment is `Component::Normal`).
/// - Each segment is 1..=255 bytes and matches ordinary portable ASCII content names
///   `[A-Za-z0-9_][A-Za-z0-9._-]*` without a trailing `.`, excluding dot-prefixed administrative
///   namespaces (including `.git`), spaces, tildes, control characters, and non-ASCII characters.
/// - Rejects Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM0`-`COM9`,
///   `LPT0`-`LPT9`, with or without extension).
/// - Preflights collisions across the full inventory: rejects duplicate paths, case-insensitive
///   collisions, and file-vs-directory prefix collisions.
fn validate_fixture_paths<'a>(
    paths: impl IntoIterator<Item = &'a str>,
) -> Result<Vec<(&'a str, PathBuf)>, String> {
    let mut seen: BTreeMap<String, (String, bool)> = BTreeMap::new();
    let mut out = Vec::new();
    for raw in paths {
        let relative = validate_fixture_path(raw)?;
        let parts: Vec<&str> = raw.split('/').collect();
        let mut folded_prefix = String::new();
        for (index, part) in parts.iter().enumerate() {
            if index > 0 {
                folded_prefix.push('/');
            }
            folded_prefix.push_str(&part.to_ascii_lowercase());
            let spelling = parts[..=index].join("/");
            let is_file = index + 1 == parts.len();
            if let Some((previous, previous_is_file)) = seen.get(&folded_prefix)
                && (*previous != spelling || *previous_is_file || is_file)
            {
                return Err(format!(
                    "fixture path collision between {previous:?} and {spelling:?}"
                ));
            }
            seen.insert(folded_prefix.clone(), (spelling, is_file));
        }
        out.push((raw, relative));
    }
    Ok(out)
}

/// Validates one record fixture path against `safe_relative` and the ordinary portable ASCII
/// content segment grammar.
fn validate_fixture_path(raw: &str) -> Result<PathBuf, String> {
    let relative = safe_relative(raw)?;
    for part in raw.split('/') {
        if !is_ordinary_portable_segment(part) {
            return Err(format!(
                "fixture path {raw:?} is not an ordinary portable content path"
            ));
        }
    }
    Ok(relative)
}

/// Ordinary portable ASCII content segment grammar: `[A-Za-z0-9_][A-Za-z0-9._-]*`, no trailing `.`,
/// length 1..=255 bytes, and not a Windows reserved device name.
fn is_ordinary_portable_segment(part: &str) -> bool {
    let bytes = part.as_bytes();
    if bytes.is_empty() || bytes.len() > FIXTURE_SEGMENT_LIMIT {
        return false;
    }
    if !(bytes[0].is_ascii_alphanumeric() || bytes[0] == b'_') {
        return false;
    }
    if bytes[bytes.len() - 1] == b'.' {
        return false;
    }
    if !bytes
        .iter()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    {
        return false;
    }
    let device = part.split('.').next().unwrap_or("").to_ascii_lowercase();
    !WINDOWS_DEVICES.contains(&device.as_str())
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum EntryKind {
    Dir,
    File,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum InventoryEntry {
    Dir,
    File(Vec<u8>),
}

/// Classifies a filesystem entry from `symlink_metadata`, refusing links and unsupported types.
fn classify_entry_type(
    is_symlink: bool,
    is_dir: bool,
    is_file: bool,
    label: &Path,
) -> Result<EntryKind, String> {
    if is_symlink {
        return Err(format!(
            "unsupported symlink entry in fixture repository: {}",
            label.display()
        ));
    }
    if is_dir {
        return Ok(EntryKind::Dir);
    }
    if is_file {
        return Ok(EntryKind::File);
    }
    Err(format!(
        "unsupported entry type in fixture repository: {}",
        label.display()
    ))
}

/// Every directory entry and regular file below `root` (the `.git` folder included) keyed by its
/// path relative to `root`, inspected without following links and rejecting unsupported entry types.
fn inventory_below(root: &Path) -> Result<BTreeMap<PathBuf, InventoryEntry>, String> {
    let root_meta = std::fs::symlink_metadata(root)
        .map_err(|error| format!("cannot inspect the fixture repository: {error}"))?;
    let root_kind = root_meta.file_type();
    if root_kind.is_symlink() || !root_kind.is_dir() {
        return Err("fixture repository root is not a direct non-symlink directory".into());
    }
    let mut out = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        let entries = std::fs::read_dir(&folder)
            .map_err(|error| format!("cannot list the fixture repository: {error}"))?;
        for entry in entries {
            let path = entry
                .map_err(|error| format!("cannot list the fixture repository: {error}"))?
                .path();
            let relative = path
                .strip_prefix(root)
                .map_err(|error| format!("cannot relativize fixture path: {error}"))?
                .to_path_buf();
            let meta = std::fs::symlink_metadata(&path)
                .map_err(|error| format!("cannot inspect the fixture repository: {error}"))?;
            let kind = meta.file_type();
            match classify_entry_type(kind.is_symlink(), kind.is_dir(), kind.is_file(), &relative)?
            {
                EntryKind::Dir => {
                    out.insert(relative, InventoryEntry::Dir);
                    pending.push(path);
                }
                EntryKind::File => {
                    let mut file = std::fs::File::open(&path)
                        .map_err(|error| format!("cannot read the fixture repository: {error}"))?;
                    let opened = file.metadata().map_err(|error| {
                        format!("cannot inspect the fixture repository: {error}")
                    })?;
                    if !opened.file_type().is_file() {
                        return Err(format!(
                            "unsupported entry type in fixture repository: {}",
                            relative.display()
                        ));
                    }
                    let mut bytes = Vec::new();
                    file.read_to_end(&mut bytes)
                        .map_err(|error| format!("cannot read the fixture repository: {error}"))?;
                    out.insert(relative, InventoryEntry::File(bytes));
                }
            }
        }
    }
    Ok(out)
}

fn null_device() -> &'static str {
    if cfg!(windows) { "nul" } else { "/dev/null" }
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn g18_m1s3_c6_fixture_inventory_validator_guard() {
        // One ordinary valid path and a multi-file ordinary inventory pass.
        let valid = validate_fixture_paths(["skills/grill/SKILL.md"]).expect("ordinary valid path");
        assert_eq!(valid.len(), 1);
        assert_eq!(valid[0].1, PathBuf::from("skills/grill/SKILL.md"));

        let multi = validate_fixture_paths([
            "LICENSE",
            "skills/grill/SKILL.md",
            "skills/grill/references/more.md",
        ])
        .expect("ordinary inventory");
        assert_eq!(multi.len(), 3);

        // One reserved administrative name is exercised strictly as inert validator input only.
        let err =
            validate_fixture_paths([".git/config"]).expect_err("reserved administrative name");
        assert!(
            err.contains("ordinary portable content path"),
            "unexpected error for reserved name: {err:?}"
        );

        // Plain-relative, portable segment limit, trailing-dot, and Windows device rules apply.
        let over_limit = "a".repeat(FIXTURE_SEGMENT_LIMIT + 1);
        for invalid in [
            "../outside.md",
            "skills/CON.md",
            "skills/trailing.",
            over_limit.as_str(),
        ] {
            assert!(
                validate_fixture_paths([invalid]).is_err(),
                "expected rejection for {invalid:?}"
            );
        }

        // Preflight collisions (case-folding and file-vs-directory) fail before any writes.
        for colliding in [
            ["skills/tool/SKILL.md", "skills/tool/skill.md"],
            ["Skills/tool/a.md", "skills/tool/b.md"],
            ["skills/tool", "skills/tool/SKILL.md"],
            ["skills/tool/SKILL.md", "skills/tool"],
        ] {
            let err = validate_fixture_paths(colliding).expect_err("collision must fail");
            assert!(err.contains("collision"), "unexpected error: {err:?}");
        }

        // Preflight aborts Fixture::build before creating the repository directory on a harmless
        // non-reserved collision (no reserved name ever reaches Fixture::build).
        let env = Env::from_process().expect("process env");
        let git = host_git(&env).expect("git on PATH");
        let scratch = tempfile::tempdir().expect("scratch");
        let repo = scratch.path().join("upstream");
        let fixture = Fixture::new(&git, &env, scratch.path()).expect("fixture");
        let upstream = json!({
            "branch": "main",
            "author": "Fixture",
            "email": "fixture",
            "date": "2026-01-01T00:00:00+00:00",
            "message": "skills",
            "files": {
                "skills/demo/note.md": "one\n",
                "skills/demo/NOTE.md": "two\n"
            }
        });
        let err = fixture
            .build(&repo, upstream.as_object().expect("upstream object"))
            .expect_err("preflight collision must fail");
        assert!(err.contains("collision"), "unexpected error: {err:?}");
        assert!(
            !repo.exists(),
            "repository folder must not be created when preflight validation fails"
        );
    }

    #[test]
    fn g19_m1s3_c7_isolated_git_init_and_explicit_repo_guard() {
        let host_env = Env::from_process().expect("process env");
        let git = host_git(&host_env).expect("git on PATH");
        let scratch = tempfile::tempdir().expect("scratch");

        // Create a harmless template directory containing only a benign plain-text marker file.
        let fake_template = scratch.path().join("host-template");
        std::fs::create_dir(&fake_template).expect("template dir");
        std::fs::write(fake_template.join("benign-template-marker.txt"), "marker\n")
            .expect("marker file");

        let mut env = host_env.clone();
        env.set("GIT_TEMPLATE_DIR", fake_template.as_os_str());
        env.set("GIT_DIR", scratch.path().join("wrong-git").as_os_str());
        env.set("GIT_WORK_TREE", scratch.path().join("wrong-wt").as_os_str());
        env.set("GIT_CONFIG_PARAMETERS", "'init.defaultBranch=wrong'");
        env.set("HTTP_PROXY", "http://127.0.0.1:9");
        env.set("CUSTOM_UNRELATED_VAR", "must-not-leak");
        env.set("SYSTEMROOT", "C:\\Windows");
        env.set("WINDIR", "C:\\Windows");
        env.set("SYSTEMDRIVE", "C:");
        env.set("COMSPEC", "C:\\Windows\\System32\\cmd.exe");
        env.set("PATHEXT", ".COM;.EXE");
        env.set("TEMP", scratch.path().as_os_str());
        env.set("TMP", scratch.path().as_os_str());
        env.set("TMPDIR", scratch.path().as_os_str());

        let fixture = Fixture::new(&git, &env, scratch.path()).expect("fixture");
        let child_vars: BTreeMap<String, String> = fixture
            .child_env(&[])
            .into_iter()
            .map(|(k, v)| {
                (
                    k.to_string_lossy().to_ascii_uppercase(),
                    v.to_string_lossy().into_owned(),
                )
            })
            .collect();

        // Required Windows & macOS host variables survive in the minimal child environment.
        for required in [
            "PATH",
            "TMPDIR",
            "SYSTEMROOT",
            "WINDIR",
            "SYSTEMDRIVE",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
        ] {
            assert!(
                child_vars.contains_key(required),
                "missing required host var {required}"
            );
        }
        // Unrelated and dangerous inherited variables are stripped.
        for stripped in [
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_CONFIG_PARAMETERS",
            "HTTP_PROXY",
            "CUSTOM_UNRELATED_VAR",
        ] {
            assert!(
                !child_vars.contains_key(stripped),
                "inherited var {stripped} leaked into child env"
            );
        }
        assert_eq!(
            child_vars.get("GIT_TEMPLATE_DIR").map(String::as_str),
            Some("")
        );
        assert_eq!(
            child_vars.get("GIT_CONFIG_NOSYSTEM").map(String::as_str),
            Some("1")
        );
        assert_eq!(
            child_vars.get("GIT_ALLOW_PROTOCOL").map(String::as_str),
            Some("")
        );
        assert_eq!(
            child_vars.get("GIT_TERMINAL_PROMPT").map(String::as_str),
            Some("0")
        );

        // Build a fresh ordinary synthetic repository using the polluted Env and verify isolation,
        // SHA-1 object format, and benign fixture review through LocalGit.
        let repo = scratch.path().join("upstream");
        let upstream = json!({
            "branch": "main",
            "author": "Fixture",
            "email": "fixture",
            "date": "2026-01-01T00:00:00+00:00",
            "message": "skills",
            "files": {
                "LICENSE": "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy of this software.\n",
                "skills/demo/SKILL.md": "---\nname: demo\ndescription: Ordinary synthetic skill fixture\n---\n\n# Demo\n"
            }
        });
        fixture
            .build(&repo, upstream.as_object().expect("upstream object"))
            .expect("isolated build");

        let dot_git = repo.join(".git");
        for (key, expected) in [("maintenance.auto", "false"), ("gc.auto", "0")] {
            let value = fixture
                .git_in_repo(&repo, &dot_git, &["config", "--get", key], &[])
                .unwrap_or_else(|error| panic!("read effective {key}: {error}"));
            assert_eq!(String::from_utf8_lossy(&value).trim(), expected, "{key}");
        }
        assert!(
            !dot_git.join("benign-template-marker.txt").exists(),
            "host template marker must not be copied into .git"
        );
        let hooks = dot_git.join("hooks");
        if hooks.exists() {
            assert_eq!(
                std::fs::read_dir(&hooks).expect("read hooks").count(),
                0,
                "hooks directory must be empty when initialized with empty templates"
            );
        }
        let config_text =
            std::fs::read_to_string(dot_git.join("config")).expect("read .git/config");
        assert!(
            config_text.contains("repositoryformatversion = 0")
                && !config_text.contains("[extensions]"),
            "expected SHA-1 format version 0 without extensions, got:\n{config_text}"
        );

        let before = inventory_below(&repo).expect("inventory before review");
        let collector = LocalGit::new(&git, &repo, "https://github.com/example/demo", &host_env);
        let allow = vec!["MIT".to_string()];
        let candidate = json!({
            "provider": "github",
            "query_id": null,
            "hit": null,
            "seed": null
        });
        let request = ReviewRequest {
            repo: "example/demo",
            path: "skills/demo",
            candidate: candidate.as_object().expect("candidate"),
            allow: &allow,
            now: utc("2026-01-02T00:00:00+00:00").expect("utc"),
        };
        let reviewed = review(&collector, &request).expect("benign review succeeds");
        let record = reviewed.to_json();
        let commit = record["source"]["commit"]
            .as_str()
            .expect("commit string in record");
        assert_eq!(
            commit.len(),
            40,
            "expected 40-hex SHA-1 commit id: {commit}"
        );
        assert!(
            commit
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
            "expected lowercase hex commit id: {commit}"
        );
        assert_eq!(
            inventory_below(&repo).expect("inventory after review"),
            before
        );

        // Explicit --git-dir rejects a nested directory that has no direct .git rather than
        // discovering the enclosing repository.
        let nested = repo.join("skills");
        assert!(
            fixture
                .git_in_repo(
                    &nested,
                    &nested.join(".git"),
                    &["rev-parse", "--git-dir"],
                    &[]
                )
                .is_err(),
            "explicit --git-dir must not fall back to upward repository discovery"
        );
    }

    #[test]
    fn g20_m1s3_c8_inventory_entries_and_no_link_following_guard() {
        let scratch = tempfile::tempdir().expect("scratch");
        let repo = scratch.path().join("repo");
        std::fs::create_dir(&repo).expect("repo dir");
        std::fs::write(repo.join("notes.txt"), "hello\n").expect("notes file");

        // Regular files and empty directory changes are both observed.
        let before = inventory_below(&repo).expect("before");
        assert_eq!(
            before.get(Path::new("notes.txt")),
            Some(&InventoryEntry::File(b"hello\n".to_vec()))
        );

        let empty_dir = repo.join("empty-dir");
        std::fs::create_dir(&empty_dir).expect("empty dir");
        let with_empty_dir = inventory_below(&repo).expect("with empty dir");
        assert_ne!(
            with_empty_dir, before,
            "inventory must distinguish an empty directory addition"
        );
        assert_eq!(
            with_empty_dir.get(Path::new("empty-dir")),
            Some(&InventoryEntry::Dir)
        );

        std::fs::remove_dir(&empty_dir).expect("remove empty dir");
        assert_eq!(inventory_below(&repo).expect("restored"), before);

        // Unsupported entry types (symlink and non-regular/non-dir) are refused.
        assert_eq!(
            classify_entry_type(false, true, false, Path::new("d")).expect("dir"),
            EntryKind::Dir
        );
        assert_eq!(
            classify_entry_type(false, false, true, Path::new("f")).expect("file"),
            EntryKind::File
        );
        assert!(
            classify_entry_type(true, false, false, Path::new("l"))
                .expect_err("symlink")
                .contains("symlink")
        );
        assert!(
            classify_entry_type(false, false, false, Path::new("fifo"))
                .expect_err("unsupported")
                .contains("unsupported entry type")
        );

        #[cfg(unix)]
        {
            // Local scratch symlink to a sibling directory inside the same tempdir must be
            // rejected without following it.
            let sibling_dir = scratch.path().join("sibling");
            std::fs::create_dir(&sibling_dir).expect("sibling dir");
            std::fs::write(sibling_dir.join("secret.txt"), "local-only\n").expect("sibling file");
            let dir_link = repo.join("linked-dir");
            std::os::unix::fs::symlink(&sibling_dir, &dir_link).expect("create local dir symlink");
            let err = inventory_below(&repo).expect_err("symlink dir must be refused");
            assert!(err.contains("symlink"), "unexpected error: {err:?}");
            std::fs::remove_file(&dir_link).expect("remove dir symlink");

            // Local scratch symlink to a regular file inside the same tempdir must also be refused.
            let file_link = repo.join("linked-file.txt");
            std::os::unix::fs::symlink(repo.join("notes.txt"), &file_link)
                .expect("create local file symlink");
            let err = inventory_below(&repo).expect_err("symlink file must be refused");
            assert!(err.contains("symlink"), "unexpected error: {err:?}");
            std::fs::remove_file(&file_link).expect("remove file symlink");

            // A symlinked repository root itself is refused.
            let root_link = scratch.path().join("repo-link");
            std::os::unix::fs::symlink(&repo, &root_link).expect("create root symlink");
            assert!(inventory_below(&root_link).is_err());
        }
    }
}
