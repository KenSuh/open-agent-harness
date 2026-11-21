// 로컬 Git 수집기: 명시된 픽스처 저장소 하나만 격리된 읽기 전용 plumbing 으로 읽어, 검토 스냅숏의 커밋·트리·파일을 한도 안에서 가져온다.
//! A bounded local Git fixture collector (M1-S3): reads one explicit local fixture repository using bounded
//! read-only plumbing, disconnected from production network and external-repository fallback paths.
//!
//! Security and confinement boundaries:
//! * Explicit repository identity: [`LocalGit`] binds one expected canonical GitHub locator and fails closed if a
//!   request names any other `spec.locator`.
//! * No implicit repository discovery: the configured folder is canonicalized and its direct `.git` directory must
//!   be a real non-symlink directory containing `HEAD` and `objects`, with no `commondir`, no object `alternates`,
//!   and only safe local fixture settings in `.git/config`. Every git call passes `--git-dir` explicitly rather than
//!   relying on `-C` discovery or `GIT_CEILING_DIRECTORIES` alone.
//! * Bounded read-only plumbing: git runs through the crate's one process runner (`proc::run_args`) with an absolute
//!   program path, no shell, per-call and cumulative observation deadlines, producer-side output limits sized to the
//!   command (`4 KiB` metadata, `16 MiB` tree listing, `1 MiB` blob read), an allowlisted environment with
//!   `GIT_ALLOW_PROTOCOL=""`, and `GIT_SAFE_CONFIG` flags.
//! * Disconnected from production: any network or external repository production fallback belongs to a later
//!   reviewed scope using isolated scratch fetch rather than reading a repository in place.

use crate::paths::Env;
use crate::proc::{ProcError, run_args};
use crate::review::identity::SourceSpec;
use crate::review::snapshot::{
    CollectError, Collector, Entry, FILE_LIMIT, Observation, Tree, snapshot,
};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// The longest one git call may take.
const CALL_DEADLINE: Duration = Duration::from_secs(60);
/// The longest an entire observation may take across all git calls.
const OBSERVE_DEADLINE: Duration = Duration::from_secs(120);
/// Output limit for single-line metadata queries (`rev-parse`, `cat-file -s`).
const META_OUTPUT_LIMIT: usize = 4096;
/// Output limit for `ls-tree` listings.
const TREE_OUTPUT_LIMIT: usize = 16 << 20;
/// Producer-side output limit for `cat-file blob`: matches the per-file snapshot limit (1 MiB).
const BLOB_OUTPUT_LIMIT: usize = FILE_LIMIT as usize;
/// Maximum `.git/config` file size inspected for fixture safety.
const CONFIG_LIMIT: u64 = 64 << 10;
/// Sentinel message used internally when a call or observation reaches its deadline.
const TIMEOUT_MARKER: &str = "timeout";

/// The caller's variables a git call keeps (v0.4.0 `git_env`, without its proxies).
const KEPT: [&str; 17] = [
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
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
];

/// Safe `[core]` keys accepted in a local fixture repository's `.git/config`.
const ALLOWED_CORE_KEYS: [&str; 10] = [
    "repositoryformatversion",
    "filemode",
    "bare",
    "logallrefupdates",
    "ignorecase",
    "precomposeunicode",
    "autocrlf",
    "symlinks",
    "hidedotfiles",
    "quotepath",
];

/// One finished git call.
struct Ran {
    code: Option<i32>,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

/// Reads one explicit local fixture repository as a review collector.
#[derive(Debug, Clone)]
pub struct LocalGit {
    program: PathBuf,
    repository: PathBuf,
    expected_locator: String,
    env: Env,
}

impl LocalGit {
    /// A fixture-only collector bound to `repository` (resolved relative to `env`'s cwd) and `expected_locator`
    /// (canonical `https://github.com/<owner>/<repo>`).
    pub fn new(
        program: impl Into<PathBuf>,
        repository: impl Into<PathBuf>,
        expected_locator: impl Into<String>,
        env: &Env,
    ) -> LocalGit {
        let repository = env.cwd().join(repository.into());
        LocalGit {
            program: program.into(),
            repository,
            expected_locator: expected_locator.into(),
            env: git_env(env),
        }
    }

    /// Validates all local fixture repository prerequisites and returns the explicit `.git` dir and call environment.
    fn establish(&self, spec: &SourceSpec) -> Result<(PathBuf, Env), CollectError> {
        if !is_canonical_locator(&self.expected_locator) || spec.locator != self.expected_locator {
            return Err(CollectError {
                message: format!(
                    "local fixture collector bound to {:?} refused spec locator {:?}",
                    self.expected_locator, spec.locator
                ),
            });
        }
        if !self.program.is_absolute() {
            return Err(CollectError {
                message: "git program path is not absolute".to_string(),
            });
        }
        let canonical = std::fs::canonicalize(&self.repository).map_err(|error| CollectError {
            message: format!("cannot resolve local fixture repository: {error}"),
        })?;
        if !canonical.is_dir() {
            return Err(CollectError {
                message: "local fixture repository path is not a directory".to_string(),
            });
        }
        let dot_git = canonical.join(".git");
        let dot_meta = std::fs::symlink_metadata(&dot_git).map_err(|error| CollectError {
            message: format!("local fixture repository has no .git directory: {error}"),
        })?;
        if dot_meta.file_type().is_symlink() || !dot_meta.is_dir() {
            return Err(CollectError {
                message: "local fixture .git must be a direct non-symlink directory".to_string(),
            });
        }
        require_non_symlink_file(&dot_git.join("HEAD"), "HEAD")?;
        require_non_symlink_dir(&dot_git.join("objects"), "objects")?;
        refuse_present(&dot_git.join("commondir"), "commondir")?;
        let objects_info = dot_git.join("objects").join("info");
        refuse_present(&objects_info.join("alternates"), "objects/info/alternates")?;
        refuse_present(
            &objects_info.join("http-alternates"),
            "objects/info/http-alternates",
        )?;
        validate_local_config(&dot_git.join("config"))?;

        let mut env = self.env.clone();
        if let Some(parent) = canonical.parent() {
            let separator = if cfg!(windows) { ';' } else { ':' };
            let text = parent.to_string_lossy();
            if !text.contains(separator) {
                env.set("GIT_CEILING_DIRECTORIES", parent.as_os_str());
            }
        }
        Ok((dot_git, env))
    }

    /// Runs git with `--git-dir` (no upward discovery), safe config flags, bounded deadline and output limit.
    fn run(
        &self,
        dot_git: &Path,
        env: &Env,
        args: &[&str],
        deadline: Instant,
        output_limit: usize,
    ) -> Result<Ran, CollectError> {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|left| !left.is_zero())
            .ok_or_else(|| CollectError {
                message: TIMEOUT_MARKER.to_string(),
            })?;
        let null = null_device();
        let config = [
            "credential.helper=".to_string(),
            "core.askPass=".to_string(),
            "core.autocrlf=false".to_string(),
            format!("core.hooksPath={null}"),
            format!("core.attributesFile={null}"),
            "init.templateDir=".to_string(),
            "http.extraHeader=".to_string(),
            "protocol.allow=never".to_string(),
            "core.fsmonitor=false".to_string(),
        ];
        let mut argv: Vec<OsString> = vec!["--literal-pathspecs".into()];
        for setting in config {
            argv.push("-c".into());
            argv.push(setting.into());
        }
        argv.push("--git-dir".into());
        argv.push(dot_git.as_os_str().to_os_string());
        argv.extend(args.iter().map(OsString::from));
        let what = args.first().copied().unwrap_or("git");
        let output = run_args(
            env,
            &self.program,
            &argv,
            CALL_DEADLINE.min(remaining),
            output_limit,
        )
        .map_err(|error| match error {
            ProcError::Timeout => CollectError {
                message: TIMEOUT_MARKER.to_string(),
            },
            other => CollectError {
                message: format!("git {what}: {other}"),
            },
        })?;
        Ok(Ran {
            code: output.status.code(),
            stdout: output.stdout,
            stderr: output.stderr,
        })
    }

    /// Runs git and returns its output when it succeeded.
    fn git(
        &self,
        dot_git: &Path,
        env: &Env,
        args: &[&str],
        deadline: Instant,
        output_limit: usize,
    ) -> Result<Vec<u8>, CollectError> {
        let Ran {
            code,
            stdout,
            stderr,
        } = self.run(dot_git, env, args, deadline, output_limit)?;
        if code == Some(0) {
            return Ok(stdout);
        }
        let said = String::from_utf8_lossy(&stderr);
        let said = said.lines().next().unwrap_or("").trim();
        let what = args.first().copied().unwrap_or("git");
        Err(CollectError {
            message: format!(
                "git {what} failed ({}): {said}",
                code.map_or("signal".to_string(), |code| code.to_string())
            ),
        })
    }

    /// The object id a `rev-parse` name resolves to, or `None` when it does not exist.
    fn resolve(
        &self,
        dot_git: &Path,
        env: &Env,
        name: &str,
        deadline: Instant,
    ) -> Result<Option<String>, CollectError> {
        let Ran {
            code,
            stdout,
            stderr,
        } = self.run(
            dot_git,
            env,
            &["rev-parse", "--verify", "--quiet", name],
            deadline,
            META_OUTPUT_LIMIT,
        )?;
        match code {
            Some(0) => object_id(String::from_utf8_lossy(&stdout).trim()).map(Some),
            Some(1) if stderr.is_empty() => Ok(None),
            _ => Err(CollectError {
                message: format!(
                    "git rev-parse failed: {}",
                    String::from_utf8_lossy(&stderr)
                        .lines()
                        .next()
                        .unwrap_or("")
                        .trim()
                ),
            }),
        }
    }

    fn attempt(&self, spec: &SourceSpec) -> Result<Observation, CollectError> {
        let deadline = Instant::now() + OBSERVE_DEADLINE;
        let (dot_git, env) = self.establish(spec)?;
        let Some(commit) = self.resolve(&dot_git, &env, "HEAD^{commit}", deadline)? else {
            return Ok(Observation::Blocked {
                problems: vec!["ref-missing".to_string()],
            });
        };
        let tree_id = self
            .resolve(&dot_git, &env, &format!("{commit}^{{tree}}"), deadline)?
            .ok_or_else(|| CollectError {
                message: format!("commit {commit} has no tree"),
            })?;
        let tree = Commit {
            git: self,
            dot_git: &dot_git,
            env: &env,
            commit: &commit,
            deadline,
        };
        Ok(Observation::Snapshot(snapshot(
            &tree, &commit, &tree_id, spec,
        )?))
    }
}

impl Collector for LocalGit {
    fn observe(&self, spec: &SourceSpec) -> Observation {
        self.attempt(spec).unwrap_or_else(|error| {
            let code = if error.message == TIMEOUT_MARKER {
                "timeout"
            } else {
                "git-failed"
            };
            Observation::Failed {
                code: code.to_string(),
            }
        })
    }
}

/// One commit of an established local fixture repository.
struct Commit<'a> {
    git: &'a LocalGit,
    dot_git: &'a Path,
    env: &'a Env,
    commit: &'a str,
    deadline: Instant,
}

impl Tree for Commit<'_> {
    fn entries(&self, path: &str) -> Result<Vec<Entry>, CollectError> {
        let mut args = vec!["ls-tree", "-r", "-z", "--full-tree", self.commit];
        if !path.is_empty() {
            args.extend(["--", path]);
        }
        let prefix = format!("{}/", path.trim_end_matches('/'));
        let out = self.git.git(
            self.dot_git,
            self.env,
            &args,
            self.deadline,
            TREE_OUTPUT_LIMIT,
        )?;
        let listed = listing(&out, true)?;
        Ok(listed
            .into_iter()
            .filter(|entry| {
                path.is_empty() || entry.path == path || entry.path.starts_with(&prefix)
            })
            .collect())
    }

    fn children(&self, folder: &str) -> Result<Vec<Entry>, CollectError> {
        let target = format!("{}/", folder.trim_end_matches('/'));
        let mut args = vec!["ls-tree", "-z", "--full-tree", self.commit];
        if !folder.is_empty() {
            args.extend(["--", target.as_str()]);
        }
        let out = self.git.git(
            self.dot_git,
            self.env,
            &args,
            self.deadline,
            TREE_OUTPUT_LIMIT,
        )?;
        listing(&out, false)
    }

    fn size(&self, blob: &str) -> Result<u64, CollectError> {
        let oid = object_id(blob)?;
        let out = self.git.git(
            self.dot_git,
            self.env,
            &["cat-file", "-s", &oid],
            self.deadline,
            META_OUTPUT_LIMIT,
        )?;
        let text = String::from_utf8_lossy(&out);
        let text = text.trim();
        if text.is_empty() || !text.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(CollectError {
                message: format!("git printed no size for blob {blob}"),
            });
        }
        text.parse().map_err(|_| CollectError {
            message: format!("git printed no size for blob {blob}"),
        })
    }

    fn read(&self, blob: &str) -> Result<Vec<u8>, CollectError> {
        let oid = object_id(blob)?;
        self.git.git(
            self.dot_git,
            self.env,
            &["cat-file", "blob", &oid],
            self.deadline,
            BLOB_OUTPUT_LIMIT,
        )
    }
}

fn is_canonical_locator(locator: &str) -> bool {
    let Some(rest) = locator.strip_prefix("https://github.com/") else {
        return false;
    };
    let Some((owner, repo)) = rest.split_once('/') else {
        return false;
    };
    !owner.is_empty()
        && !repo.is_empty()
        && !repo.contains('/')
        && owner
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        && repo.bytes().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'_' | b'-')
        })
}

fn require_non_symlink_file(path: &Path, label: &str) -> Result<(), CollectError> {
    let meta = std::fs::symlink_metadata(path).map_err(|error| CollectError {
        message: format!("local fixture .git/{label} is missing: {error}"),
    })?;
    if meta.file_type().is_symlink() || !meta.is_file() {
        return Err(CollectError {
            message: format!("local fixture .git/{label} must be a regular non-symlink file"),
        });
    }
    Ok(())
}

fn require_non_symlink_dir(path: &Path, label: &str) -> Result<(), CollectError> {
    let meta = std::fs::symlink_metadata(path).map_err(|error| CollectError {
        message: format!("local fixture .git/{label} is missing: {error}"),
    })?;
    if meta.file_type().is_symlink() || !meta.is_dir() {
        return Err(CollectError {
            message: format!("local fixture .git/{label} must be a real non-symlink directory"),
        });
    }
    Ok(())
}

fn refuse_present(path: &Path, label: &str) -> Result<(), CollectError> {
    match std::fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        _ => Err(CollectError {
            message: format!("local fixture .git/{label} is not permitted"),
        }),
    }
}

/// Validates `.git/config` against a strict allowlist of benign local fixture settings.
fn validate_local_config(path: &Path) -> Result<(), CollectError> {
    let meta = match std::fs::symlink_metadata(path) {
        Ok(meta) => meta,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => {
            return Err(CollectError {
                message: format!("cannot stat local fixture .git/config: {error}"),
            });
        }
    };
    if meta.file_type().is_symlink() || !meta.is_file() || meta.len() > CONFIG_LIMIT {
        return Err(CollectError {
            message: "local fixture .git/config is not a bounded regular file".to_string(),
        });
    }
    let bytes = std::fs::read(path).map_err(|error| CollectError {
        message: format!("cannot read local fixture .git/config: {error}"),
    })?;
    let text = std::str::from_utf8(&bytes).map_err(|_| CollectError {
        message: "local fixture .git/config is not valid UTF-8".to_string(),
    })?;
    let mut section: Option<String> = None;
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if line.ends_with('\\') {
            return Err(CollectError {
                message: "line continuations in local fixture .git/config are not allowed"
                    .to_string(),
            });
        }
        if line.starts_with('[') {
            let Some(inside) = line
                .strip_prefix('[')
                .and_then(|rest| rest.strip_suffix(']'))
            else {
                return Err(CollectError {
                    message: "malformed section header in local fixture .git/config".to_string(),
                });
            };
            let name = inside
                .split(|c: char| c.is_ascii_whitespace() || c == '"')
                .next()
                .unwrap_or("")
                .to_ascii_lowercase();
            if !matches!(name.as_str(), "core" | "user" | "init") {
                return Err(CollectError {
                    message: format!(
                        "section [{name}] is not permitted in local fixture .git/config"
                    ),
                });
            }
            section = Some(name);
            continue;
        }
        let Some(current) = section.as_deref() else {
            return Err(CollectError {
                message: "entry outside section in local fixture .git/config".to_string(),
            });
        };
        let (key, value) = match line.split_once('=') {
            Some((key, value)) => (key.trim().to_ascii_lowercase(), Some(strip_comment(value))),
            None => (line.trim().to_ascii_lowercase(), None),
        };
        match current {
            "core" => {
                if !ALLOWED_CORE_KEYS.contains(&key.as_str()) {
                    return Err(CollectError {
                        message: format!(
                            "core.{key} is not permitted in local fixture .git/config"
                        ),
                    });
                }
                if key == "repositoryformatversion" && value.as_deref() != Some("0") {
                    return Err(CollectError {
                        message: "unsupported repositoryformatversion in local fixture .git/config"
                            .to_string(),
                    });
                }
                if key == "bare" && value.as_deref() != Some("false") {
                    return Err(CollectError {
                        message: "bare repositories are not permitted in local fixture collector"
                            .to_string(),
                    });
                }
            }
            "user" => {
                if !matches!(key.as_str(), "name" | "email") {
                    return Err(CollectError {
                        message: format!(
                            "user.{key} is not permitted in local fixture .git/config"
                        ),
                    });
                }
            }
            "init" => {
                if key != "defaultbranch" {
                    return Err(CollectError {
                        message: format!(
                            "init.{key} is not permitted in local fixture .git/config"
                        ),
                    });
                }
            }
            _ => unreachable!(),
        }
    }
    Ok(())
}

fn strip_comment(value: &str) -> String {
    let trimmed = value
        .split(['#', ';'])
        .next()
        .unwrap_or("")
        .trim()
        .trim_matches('"');
    trimmed.to_ascii_lowercase()
}

/// A full hexadecimal object id, so nothing git reads as an option or a revision expression reaches it.
fn object_id(text: &str) -> Result<String, CollectError> {
    let hex = text
        .bytes()
        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    if hex && matches!(text.len(), 40 | 64) {
        Ok(text.to_string())
    } else {
        Err(CollectError {
            message: format!("{text:?} is not a git object id"),
        })
    }
}

/// `ls-tree -z` records: `<mode> <kind> <id>\t<path>`; with `blobs_only`, the id of anything but a blob is empty.
fn listing(out: &[u8], blobs_only: bool) -> Result<Vec<Entry>, CollectError> {
    let mut entries = Vec::new();
    for record in out
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
    {
        let text = std::str::from_utf8(record).map_err(|_| CollectError {
            message: "git listed a path that is not UTF-8".to_string(),
        })?;
        let malformed = || CollectError {
            message: format!("git listed an unreadable entry {text:?}"),
        };
        let (meta, path) = text.split_once('\t').ok_or_else(malformed)?;
        let mut fields = meta.split_whitespace();
        let (Some(mode), Some(kind), Some(oid), None) =
            (fields.next(), fields.next(), fields.next(), fields.next())
        else {
            return Err(malformed());
        };
        let oid = if blobs_only && kind != "blob" {
            String::new()
        } else {
            object_id(oid)?
        };
        entries.push(Entry {
            mode: mode.to_string(),
            oid,
            path: path.to_string(),
        });
    }
    Ok(entries)
}

/// The caller's allowlisted variables, plus v0.4.0's settings that keep git's system and global configuration,
/// prompts, protocols and credential helpers out, and no replacement objects or optional locks.
fn git_env(env: &Env) -> Env {
    let mut clean = env.clone();
    let dropped: Vec<OsString> = env
        .vars()
        .filter(|(name, _)| !KEPT.contains(&name.to_string_lossy().to_ascii_uppercase().as_str()))
        .map(|(name, _)| name.to_os_string())
        .collect();
    for name in dropped {
        clean.remove(name);
    }
    let null = null_device();
    let settings = [
        ("GIT_TERMINAL_PROMPT", "0"),
        ("GIT_ASKPASS", ""),
        ("SSH_ASKPASS", ""),
        ("GCM_INTERACTIVE", "never"),
        ("GIT_ALLOW_PROTOCOL", ""),
        ("GIT_CONFIG_NOSYSTEM", "1"),
        ("GIT_CONFIG_GLOBAL", null),
        ("GIT_CONFIG_SYSTEM", null),
        ("HOME", null),
        ("USERPROFILE", null),
        ("XDG_CONFIG_HOME", null),
        ("CURL_HOME", null),
        ("GIT_NO_REPLACE_OBJECTS", "1"),
        ("GIT_OPTIONAL_LOCKS", "0"),
    ];
    for (name, value) in settings {
        clean.set(name, value);
    }
    clean
}

/// Python's `os.devnull`.
fn null_device() -> &'static str {
    if cfg!(windows) { "nul" } else { "/dev/null" }
}
