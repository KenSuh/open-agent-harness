// 경로 찾기: 명시적 환경으로 사용자 OAH 폴더·프로젝트 루트·기계별 상태 폴더를 정하며 아무 폴더도 만들지 않는다.
//! Where OAH's files live, resolved from an explicit [`Env`] and never by creating anything.
//!
//! * The user folder is `OAH_HOME` when it is set and non-empty, otherwise `<home>/.oah`. The home folder is
//!   `HOME` on Unix and `USERPROFILE` on Windows, as v0.4.0 read it; a missing or relative home is an error,
//!   never a silent fallback to the current folder. A relative `OAH_HOME` is resolved once against the
//!   invocation folder ([`Env::cwd`]).
//! * A project inside a Git work tree keeps per-machine state in `<git-common-dir>/oah/v1/`, which every
//!   worktree of the repository shares and which leaves v0.4.0's `<git-common-dir>/oah/` files alone.
//!   `OAH_STATE_DIR`, when set and non-empty, replaces `<git-common-dir>/oah` (so state is
//!   `$OAH_STATE_DIR/v1/`); a relative value is resolved against the invocation folder.
//! * A folder outside Git keeps state in `OAH_HOME/state/projects/<id>/`, where `<id>` is the first 16 lowercase
//!   hexadecimal digits of the SHA-256 of its canonical path: the native path bytes on Unix, the UTF-16 code
//!   units in little-endian order (no byte-order mark, no terminator) on Windows.
//! * User settings are `OAH_HOME/settings.toml`.

use sha2::{Digest, Sha256};
use std::ffi::{OsStr, OsString};
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// The environment variables and invocation folder one resolution works from.
///
/// Nothing in this module reads the process environment on its own; the CLI builds an `Env` once with
/// [`Env::from_process`], and tests build their own without touching the process.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Env {
    vars: Vec<(OsString, OsString)>,
    cwd: PathBuf,
}

impl Env {
    /// An environment with no variables, invoked in `cwd`, which must be absolute.
    pub fn new(cwd: impl Into<PathBuf>) -> Result<Env, PathsError> {
        let cwd = cwd.into();
        if !cwd.is_absolute() {
            return Err(PathsError::new(
                PathsErrorKind::Invocation,
                format!("the invocation folder {} is not absolute", cwd.display()),
            ));
        }
        Ok(Env {
            vars: Vec::new(),
            cwd,
        })
    }

    /// The current process's variables and folder.
    pub fn from_process() -> Result<Env, PathsError> {
        let cwd = std::env::current_dir().map_err(|error| {
            PathsError::new(
                PathsErrorKind::Invocation,
                format!("the current folder cannot be read: {error}"),
            )
        })?;
        let mut env = Env::new(cwd)?;
        for (name, value) in std::env::vars_os() {
            env.set(name, value);
        }
        Ok(env)
    }

    /// Sets a variable, replacing any earlier value. Names compare case-insensitively on Windows.
    pub fn set(&mut self, name: impl Into<OsString>, value: impl Into<OsString>) {
        let name = name.into();
        let value = value.into();
        match self
            .vars
            .iter_mut()
            .find(|(known, _)| same_name(known, &name))
        {
            Some(entry) => entry.1 = value,
            None => self.vars.push((name, value)),
        }
    }

    /// Removes a variable.
    pub fn remove(&mut self, name: impl AsRef<OsStr>) {
        let name = name.as_ref();
        self.vars.retain(|(known, _)| !same_name(known, name));
    }

    /// Builder form of [`Env::set`].
    pub fn with(mut self, name: impl Into<OsString>, value: impl Into<OsString>) -> Env {
        self.set(name, value);
        self
    }

    /// Builder form of [`Env::remove`].
    pub fn without(mut self, name: impl AsRef<OsStr>) -> Env {
        self.remove(name);
        self
    }

    /// A variable's value, if set.
    pub fn var(&self, name: impl AsRef<OsStr>) -> Option<&OsStr> {
        let name = name.as_ref();
        self.vars
            .iter()
            .find(|(known, _)| same_name(known, name))
            .map(|(_, value)| value.as_os_str())
    }

    /// A variable's value when it is set and not empty.
    fn non_empty(&self, name: &str) -> Option<&OsStr> {
        self.var(name).filter(|value| !value.is_empty())
    }

    /// The absolute folder the command was invoked in.
    pub fn cwd(&self) -> &Path {
        &self.cwd
    }

    /// Every variable, for handing to a child process.
    pub fn vars(&self) -> impl Iterator<Item = (&OsStr, &OsStr)> {
        self.vars
            .iter()
            .map(|(name, value)| (name.as_os_str(), value.as_os_str()))
    }
}

fn same_name(a: &OsStr, b: &OsStr) -> bool {
    if cfg!(windows) {
        a.eq_ignore_ascii_case(b)
    } else {
        a == b
    }
}

/// What kind of path problem occurred.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathsErrorKind {
    /// The invocation folder is unusable.
    Invocation,
    /// Neither `OAH_HOME` nor a usable home folder is set.
    HomeUnavailable,
    /// Git is not installed or not on `PATH`.
    GitMissing,
    /// Git ran but could not describe the folder (for example a bare repository or an unsafe owner).
    GitProbeFailed,
    /// The project folder does not exist or is not a folder.
    ProjectMissing,
}

impl PathsErrorKind {
    pub fn code(self) -> &'static str {
        match self {
            PathsErrorKind::Invocation => "OAH_INVOCATION_INVALID",
            PathsErrorKind::HomeUnavailable => "OAH_HOME_UNAVAILABLE",
            PathsErrorKind::GitMissing => "OAH_GIT_MISSING",
            PathsErrorKind::GitProbeFailed => "OAH_GIT_PROBE_FAILED",
            PathsErrorKind::ProjectMissing => "OAH_PROJECT_MISSING",
        }
    }
}

/// A typed path-resolution failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathsError {
    kind: PathsErrorKind,
    message: String,
}

impl PathsError {
    fn new(kind: PathsErrorKind, message: impl Into<String>) -> Self {
        PathsError {
            kind,
            message: message.into(),
        }
    }

    pub fn kind(&self) -> PathsErrorKind {
        self.kind
    }

    pub fn code(&self) -> &'static str {
        self.kind.code()
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for PathsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.kind.code(), self.message)
    }
}

impl std::error::Error for PathsError {}

const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };

/// The user's home folder: `HOME` on Unix, `USERPROFILE` on Windows. It must be set, non-empty and absolute.
pub fn user_home(env: &Env) -> Result<PathBuf, PathsError> {
    match env.non_empty(HOME_VAR) {
        Some(value) => {
            let home = PathBuf::from(value);
            if home.is_absolute() {
                Ok(home)
            } else {
                Err(PathsError::new(
                    PathsErrorKind::HomeUnavailable,
                    format!(
                        "{HOME_VAR} is not an absolute path; set it to the home folder or set OAH_HOME"
                    ),
                ))
            }
        }
        None => Err(PathsError::new(
            PathsErrorKind::HomeUnavailable,
            format!("{HOME_VAR} is not set; set it to the home folder or set OAH_HOME"),
        )),
    }
}

/// The user's OAH folder: `OAH_HOME` when set and non-empty (a relative value is resolved against the
/// invocation folder), otherwise `<home>/.oah`. The folder is not created.
pub fn oah_home(env: &Env) -> Result<PathBuf, PathsError> {
    match env.non_empty("OAH_HOME") {
        Some(value) => Ok(absolute_in(env, Path::new(value))),
        None => Ok(user_home(env)?.join(".oah")),
    }
}

/// `OAH_HOME/settings.toml`.
pub fn settings_file(env: &Env) -> Result<PathBuf, PathsError> {
    Ok(oah_home(env)?.join("settings.toml"))
}

/// `OAH_HOME/state`, the user-wide state folder.
pub fn user_state_dir(env: &Env) -> Result<PathBuf, PathsError> {
    Ok(oah_home(env)?.join("state"))
}

fn absolute_in(env: &Env, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        env.cwd().join(path)
    }
}

/// How a project was identified.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProjectKind {
    /// A Git work tree; `common_dir` is the repository's common Git folder, shared by its worktrees.
    Git { common_dir: PathBuf },
    /// A folder outside Git, identified by the hash of its canonical path.
    Folder { id: String },
}

/// A located project: its root folder and where its per-machine state lives.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Project {
    pub root: PathBuf,
    pub kind: ProjectKind,
    /// The per-machine state folder; not created by locating.
    pub state_dir: PathBuf,
}

impl Project {
    /// `<root>/.oah/config.toml`.
    pub fn config_file(&self) -> PathBuf {
        self.root.join(".oah").join("config.toml")
    }
}

/// Finds the project that `folder` (resolved against the invocation folder) belongs to.
///
/// Git is asked first, through its argument vector and never a shell. Inside a work tree the root is Git's top
/// level; outside one the folder itself, canonicalized, is the project. A missing Git executable and a Git
/// failure other than "not a git repository" are errors rather than a silent fallback.
pub fn locate_project(env: &Env, folder: &Path) -> Result<Project, PathsError> {
    let folder = absolute_in(env, folder);
    match fs::metadata(&folder) {
        Ok(metadata) if metadata.is_dir() => {}
        Ok(_) => {
            return Err(PathsError::new(
                PathsErrorKind::ProjectMissing,
                format!("{} is not a folder", folder.display()),
            ));
        }
        Err(error) => {
            return Err(PathsError::new(
                PathsErrorKind::ProjectMissing,
                format!("{} cannot be opened: {error}", folder.display()),
            ));
        }
    }
    match probe_git(env, &folder)? {
        GitProbe::WorkTree { root, common_dir } => {
            let base = match env.non_empty("OAH_STATE_DIR") {
                Some(value) => absolute_in(env, Path::new(value)),
                None => common_dir.join("oah"),
            };
            Ok(Project {
                root,
                kind: ProjectKind::Git { common_dir },
                state_dir: base.join("v1"),
            })
        }
        GitProbe::NotARepository => {
            let root = canonical_folder(&folder)?;
            let id = folder_id(&root);
            let state_dir = user_state_dir(env)?.join("projects").join(&id);
            Ok(Project {
                root,
                kind: ProjectKind::Folder { id },
                state_dir,
            })
        }
    }
}

/// The canonical form of an existing folder: every link resolved and, on Windows, written without the `\\?\`
/// prefix when the path is an ordinary drive or network path shorter than 260 characters.
pub fn canonical_folder(folder: &Path) -> Result<PathBuf, PathsError> {
    let canonical = fs::canonicalize(folder).map_err(|error| {
        PathsError::new(
            PathsErrorKind::ProjectMissing,
            format!("{} cannot be resolved: {error}", folder.display()),
        )
    })?;
    Ok(simplify_verbatim(canonical))
}

#[cfg(windows)]
fn simplify_verbatim(path: PathBuf) -> PathBuf {
    let Some(text) = path.to_str() else {
        return path;
    };
    let simple = if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        format!(r"\\{rest}")
    } else if let Some(rest) = text.strip_prefix(r"\\?\") {
        let bytes = rest.as_bytes();
        if bytes.len() >= 3
            && bytes[0].is_ascii_alphabetic()
            && bytes[1] == b':'
            && bytes[2] == b'\\'
        {
            rest.to_string()
        } else {
            return path;
        }
    } else {
        return path;
    };
    if simple.encode_utf16().count() < 260 {
        PathBuf::from(simple)
    } else {
        path
    }
}

#[cfg(not(windows))]
fn simplify_verbatim(path: PathBuf) -> PathBuf {
    path
}

/// The 16-digit project id of a canonical folder path outside Git.
pub fn folder_id(canonical: &Path) -> String {
    let digest = Sha256::digest(path_bytes(canonical));
    let mut id = String::with_capacity(16);
    for byte in digest.iter().take(8) {
        id.push_str(&format!("{byte:02x}"));
    }
    id
}

#[cfg(unix)]
fn path_bytes(path: &Path) -> Vec<u8> {
    use std::os::unix::ffi::OsStrExt;
    path.as_os_str().as_bytes().to_vec()
}

#[cfg(windows)]
fn path_bytes(path: &Path) -> Vec<u8> {
    use std::os::windows::ffi::OsStrExt;
    path.as_os_str()
        .encode_wide()
        .flat_map(|unit| unit.to_le_bytes())
        .collect()
}

#[cfg(not(any(unix, windows)))]
fn path_bytes(path: &Path) -> Vec<u8> {
    path.to_string_lossy().into_owned().into_bytes()
}

enum GitProbe {
    WorkTree { root: PathBuf, common_dir: PathBuf },
    NotARepository,
}

/// Asks Git for the work-tree root and the absolute common directory of `folder`.
fn probe_git(env: &Env, folder: &Path) -> Result<GitProbe, PathsError> {
    let git = find_git(env)?;
    let mut command = Command::new(&git);
    command
        .arg("-C")
        .arg(folder)
        .args([
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
        ])
        .env_clear()
        .envs(env.vars())
        // Git's own messages are matched below, so they must not be translated.
        .env("LC_ALL", "C")
        .env("LANGUAGE", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = command.output().map_err(|error| {
        PathsError::new(
            PathsErrorKind::GitMissing,
            format!("git at {} cannot be started: {error}", git.display()),
        )
    })?;
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        if stderr.contains("not a git repository") {
            return Ok(GitProbe::NotARepository);
        }
        return Err(PathsError::new(
            PathsErrorKind::GitProbeFailed,
            format!(
                "git cannot describe {} ({}): {}",
                folder.display(),
                output.status,
                stderr.trim()
            ),
        ));
    }
    let lines = split_lines(&output.stdout);
    match lines.as_slice() {
        [root, common] if !root.is_empty() && !common.is_empty() => {
            let root = native_git_path(root)?;
            let common_dir = native_git_path(common)?;
            if !root.is_absolute() || !common_dir.is_absolute() {
                return Err(PathsError::new(
                    PathsErrorKind::GitProbeFailed,
                    "git returned a relative path; Git 2.31 or newer is required",
                ));
            }
            Ok(GitProbe::WorkTree { root, common_dir })
        }
        _ => Err(PathsError::new(
            PathsErrorKind::GitProbeFailed,
            format!(
                "git returned an unexpected answer for {}; Git 2.31 or newer is required",
                folder.display()
            ),
        )),
    }
}

fn split_lines(stdout: &[u8]) -> Vec<&[u8]> {
    let mut text = stdout;
    if let Some(stripped) = text.strip_suffix(b"\n") {
        text = stripped;
    }
    if text.is_empty() {
        return Vec::new();
    }
    text.split(|byte| *byte == b'\n')
        .map(|line| line.strip_suffix(b"\r").unwrap_or(line))
        .collect()
}

#[cfg(unix)]
fn native_git_path(bytes: &[u8]) -> Result<PathBuf, PathsError> {
    use std::os::unix::ffi::OsStrExt;
    Ok(PathBuf::from(OsStr::from_bytes(bytes)))
}

#[cfg(not(unix))]
fn native_git_path(bytes: &[u8]) -> Result<PathBuf, PathsError> {
    let text = std::str::from_utf8(bytes).map_err(|_| {
        PathsError::new(
            PathsErrorKind::GitProbeFailed,
            "git returned a path that is not UTF-8",
        )
    })?;
    // Git for Windows prints forward slashes; keep native separators inside OAH.
    Ok(PathBuf::from(text.replace('/', "\\")))
}

/// Resolves `git` on the environment's `PATH`. On Windows only `.exe` and `.com` files qualify; batch shims are
/// never launched.
fn find_git(env: &Env) -> Result<PathBuf, PathsError> {
    let missing = || {
        PathsError::new(
            PathsErrorKind::GitMissing,
            "git was not found on PATH; install Git or add it to PATH",
        )
    };
    let path = env.var("PATH").ok_or_else(missing)?;
    for folder in std::env::split_paths(path) {
        if folder.as_os_str().is_empty() || !folder.is_absolute() {
            continue;
        }
        for name in git_names() {
            let candidate = folder.join(name);
            if is_runnable(&candidate) {
                return Ok(candidate);
            }
        }
    }
    Err(missing())
}

fn git_names() -> &'static [&'static str] {
    if cfg!(windows) {
        &["git.exe", "git.com"]
    } else {
        &["git"]
    }
}

#[cfg(unix)]
fn is_runnable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    match fs::metadata(path) {
        Ok(metadata) => metadata.is_file() && metadata.permissions().mode() & 0o111 != 0,
        Err(_) => false,
    }
}

#[cfg(not(unix))]
fn is_runnable(path: &Path) -> bool {
    fs::metadata(path).is_ok_and(|metadata| metadata.is_file())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root() -> PathBuf {
        PathBuf::from(if cfg!(windows) { r"C:\work" } else { "/work" })
    }

    #[test]
    fn a_relative_invocation_folder_is_refused() {
        let error = Env::new("relative").unwrap_err();
        assert_eq!(error.kind(), PathsErrorKind::Invocation);
    }

    #[test]
    fn setting_a_variable_twice_keeps_the_last_value() {
        let env = Env::new(root()).unwrap().with("A", "1").with("A", "2");
        assert_eq!(env.var("A"), Some(OsStr::new("2")));
        assert_eq!(env.vars().count(), 1);
    }

    #[test]
    fn git_output_lines_drop_the_final_newline_and_carriage_returns() {
        assert_eq!(
            split_lines(b"/a\r\n/b\n"),
            vec![b"/a".as_slice(), b"/b".as_slice()]
        );
        assert!(split_lines(b"").is_empty());
    }

    #[test]
    fn the_folder_id_is_sixteen_lowercase_hex_digits() {
        let id = folder_id(&root().join("project"));
        assert_eq!(id.len(), 16);
        assert!(
            id.chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        );
    }

    #[cfg(unix)]
    #[test]
    fn the_folder_id_hashes_the_native_path_bytes() {
        // The first 16 hex digits of SHA-256 over the UTF-8 bytes of each path.
        assert_eq!(folder_id(Path::new("/work/project")), "65d80d2c48b3d23b");
        assert_eq!(folder_id(Path::new("/work/프로젝트")), "088855a534bc32c4");
    }

    #[cfg(windows)]
    #[test]
    fn the_folder_id_hashes_utf16_little_endian_code_units() {
        // The first 16 hex digits of SHA-256 over the UTF-16LE code units, no BOM, no terminator.
        assert_eq!(folder_id(Path::new(r"C:\work\project")), "d1d5f089c483621a");
    }

    #[cfg(windows)]
    #[test]
    fn verbatim_drive_and_network_paths_are_simplified() {
        assert_eq!(
            simplify_verbatim(PathBuf::from(r"\\?\C:\work\project")),
            PathBuf::from(r"C:\work\project")
        );
        assert_eq!(
            simplify_verbatim(PathBuf::from(r"\\?\UNC\server\share\x")),
            PathBuf::from(r"\\server\share\x")
        );
        assert_eq!(
            simplify_verbatim(PathBuf::from(r"\\?\Volume{1}\x")),
            PathBuf::from(r"\\?\Volume{1}\x")
        );
    }
}
