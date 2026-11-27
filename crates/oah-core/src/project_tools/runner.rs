// 프로젝트 도구 실행기 어댑터: 앱 실행 파일 옆에 설치된 oah-project-tools 를 정해진 Impact Map 동작으로만, 마감·취소·출력 상한 아래에서 돌리고, 답은 검증한 값으로만 돌려준다.
//! The bounded, cancellable adapter for the packaged project tools (`oah-project-tools`).
//!
//! The tool is a separate program, shipped beside the Desktop executable and pinned to the workspace version. This
//! module is the only place that starts it, and only for an explicit request: making a [`ProjectTools`] reads and
//! starts nothing, and no status card calls [`ProjectTools::impact`].
//!
//! What a call may do is fixed here, not by its caller:
//!
//! - Program. An absolute path to a file whose name starts with `oah-project-tools`: [`SIDECAR_NAME`] beside the
//!   app's own executable, or the copy a development build stages under a platform name. The app builds that
//!   path itself. Never a project path, a configured command or a name looked up on `PATH`.
//! - Version. `--version` must print exactly `oah-project-tools <VERSION>` before every action.
//! - Arguments. `impact status|refresh --root <root> [--git <git>]` and nothing else. Paths go as native strings.
//! - Environment. Cleared, then only the variables of [`PASSED`] from the session's environment, `PATH` reduced
//!   to its absolute entries outside the project (and only when no Git program is given), and `OAH_STATE_DIR`
//!   resolved the way [`crate::paths::locate_project`] resolves it, so the tool writes the map where the Impact
//!   Map card reads it. No key, loader, interpreter or other `GIT_*` variable reaches the tool.
//! - Folder. The tool's own folder, never the project.
//! - Limits. A deadline for each start, a byte cap on each stream and the task's cancellation, all enforced by
//!   [`crate::proc::tree`], which ends the tool's whole process tree before the call returns.
//! - Answer. One JSON object of schema 1 that names this tool, this action, this build and this project folder.
//!   Only checked values leave this module: states, counts, commit ids, a branch name and the tool's refusal code.
//!   Its free text, its absolute paths and its standard error do not.
//!
//! Stopping a refresh does not undo it. The tool replaces the map file in one step, so a stopped refresh has saved
//! either a complete map or none; [`RunnerError::Cancelled`] and [`RunnerError::TimedOut`] say when that is
//! possible. Nothing is remembered here, so the next status asks the tool again and reports what is there.

mod extensions;

use crate::app::Ctx;
use crate::error::Coded;
use crate::paths::Env;
use crate::proc::Output;
use crate::proc::tree::{TreeError, TreeRun, run_tree, run_tree_input};
use crate::tasks::{CancelToken, Progress, ProgressSink};
use serde::Deserialize;
use std::ffi::{OsStr, OsString};
use std::fmt;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// The file name of the tool beside the app's executable.
#[cfg(windows)]
pub const SIDECAR_NAME: &str = "oah-project-tools.exe";
/// The file name of the tool beside the app's executable.
#[cfg(not(windows))]
pub const SIDECAR_NAME: &str = "oah-project-tools";

/// The name the tool gives itself in `--version` and in every answer.
const RUNNER_NAME: &str = "oah-project-tools";
/// The answer schema this build reads.
const SCHEMA: u64 = 1;
/// The tool's exit code for a refusal: `ok` is false and `error.code` says why.
const EXIT_REFUSED: i32 = 69;
/// The most the tool prints for one answer (its own `OUTPUT_LIMIT`), and the newline after it.
const ANSWER_LIMIT: usize = (1 << 18) + 1;
/// The most `--version` may print.
const VERSION_LIMIT: usize = 4096;
/// The most the tool may write to standard error, which is never shown.
const STDERR_LIMIT: usize = 1 << 16;
/// How long a stopped tool may take to end by itself, removing the files it unpacked, before it is killed.
const STOP_GRACE: Duration = Duration::from_secs(2);
/// The steps of one call: the version check, then the action.
const STEPS: u32 = 2;

/// The variables the tool may see besides `PATH` and `OAH_STATE_DIR`, copied from the session's environment when
/// they are set there: where Git finds the user's own configuration, and where the tool unpacks itself and keeps
/// scratch files.
#[cfg(not(windows))]
const PASSED: &[&str] = &[
    "HOME",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "TMPDIR",
];
/// The variables the tool may see besides `PATH` and `OAH_STATE_DIR`, copied from the session's environment when
/// they are set there: where Git finds the user's own configuration, where the tool unpacks itself and keeps
/// scratch files, and the system folder Windows programs need to start.
#[cfg(windows)]
const PASSED: &[&str] = &[
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "ProgramData",
    "SystemRoot",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "TEMP",
    "TMP",
];

/// The deadline of each start. A one-file tool unpacks itself on every start, and a refresh may build the first
/// map of a large project; the tool bounds each Git call itself, and these bound the whole run.
#[derive(Debug, Clone, Copy)]
struct Deadlines {
    version: Duration,
    status: Duration,
    refresh: Duration,
}

const DEADLINES: Deadlines = Deadlines {
    version: Duration::from_secs(60),
    status: Duration::from_secs(5 * 60),
    refresh: Duration::from_secs(30 * 60),
};

/// What the Impact Map tool is asked to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImpactAction {
    /// Read where the project's map stands. Changes nothing.
    Status,
    /// Build the first map, or bring the stored one up to date.
    Refresh,
}

impl ImpactAction {
    /// The word on the tool's command line and in its answer.
    fn word(self) -> &'static str {
        match self {
            ImpactAction::Status => "status",
            ImpactAction::Refresh => "refresh",
        }
    }

    /// The progress phase of a call.
    fn phase(self) -> &'static str {
        match self {
            ImpactAction::Status => "impact-status",
            ImpactAction::Refresh => "impact-refresh",
        }
    }
}

/// One Impact Map request.
///
/// `root` is the absolute project root the app or the CLI located and verified itself (`Project::root`), never
/// text from the renderer. `git` is an explicitly trusted, absolute Git program; without one the tool looks for
/// `git` on the reduced `PATH` and checks what it finds.
#[derive(Debug, Clone, Copy)]
pub struct ImpactRequest<'a> {
    pub action: ImpactAction,
    pub root: &'a Path,
    pub git: Option<&'a Path>,
}

/// Where a project's map stands.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MapState {
    Fresh,
    Stale,
    Missing,
    NoCommit,
    NotARepository,
    Unavailable,
}

/// What a call did to the stored map.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Performed {
    None,
    Built,
    Refreshed,
}

/// The stored map: the commit it was made for, and how much uncommitted or unread work it carries.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct MapSummary {
    /// The full commit id.
    pub commit: String,
    pub dirty: u64,
    pub untracked: u64,
    pub includes_uncommitted: bool,
    pub excluded_total: u64,
    pub sql_unparsed: u64,
}

/// What an existing graph file is worth for the current commit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphState {
    Absent,
    Available,
    Stale,
    Unproven,
}

/// The graph file beside the map, which the tool reads and never builds.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub struct GraphSummary {
    pub state: GraphState,
    pub nodes: u64,
    pub edges: u64,
}

/// The tool's checked answer to one request.
///
/// A refusal is an answer too: `refusal` then holds the tool's `OAH_*` code (not a repository, no commit yet, an
/// unsupported Git, an unsafe repository setting, ...) and `state` what it could still tell.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImpactReply {
    pub action: ImpactAction,
    pub state: MapState,
    pub performed: Performed,
    /// How many files the refresh read again.
    pub changed: u64,
    /// The full id of the commit that is checked out, when there is one.
    pub head: Option<String>,
    pub branch: Option<String>,
    pub map: Option<MapSummary>,
    pub graph: Option<GraphSummary>,
    pub refusal: Option<String>,
}

/// Why the tool's answer cannot be used.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplyProblem {
    /// Not exactly one JSON object with the fields of an answer.
    Malformed,
    /// Another answer schema.
    Schema,
    /// Another tool or another action than the one asked for.
    Request,
    /// Another program or another version than this build's tool.
    Runner,
    /// Another folder than the project that was asked about.
    Root,
    /// The exit code, `ok` and `error` do not agree.
    Outcome,
    /// A value that cannot be right: a status that changed something, or an id or name of the wrong form.
    Value,
}

impl fmt::Display for ReplyProblem {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            ReplyProblem::Malformed => "it is not one answer of the expected form",
            ReplyProblem::Schema => "it uses another answer format",
            ReplyProblem::Request => "it answers another request",
            ReplyProblem::Runner => "it comes from another build of the tools",
            ReplyProblem::Root => "it is about another folder",
            ReplyProblem::Outcome => "its exit code and its result disagree",
            ReplyProblem::Value => "it holds a value that cannot be right",
        })
    }
}

/// How a run failed without an answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Failure {
    /// The tool could not be started or watched.
    Io(io::ErrorKind),
    /// The tool ended with an exit code that is neither success nor a refusal, or was ended by a signal (`None`).
    Exit(Option<i32>),
}

/// Why a project-tools call gave no answer. Every message is written here; none repeats the tool's own text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunnerError {
    /// The tool is not installed where the app looked: not an absolute path to a file whose name starts with
    /// `oah-project-tools`. Nothing was started.
    Unavailable,
    /// The installed tool did not report this build's exact version. No action was started.
    Version,
    /// The project root or the Git program is not an absolute path. Nothing was started.
    Request,
    /// The task was cancelled and the tool's process tree was stopped. `may_have_changed` is true for a refresh
    /// that had started: it may already have saved a map.
    Cancelled { may_have_changed: bool },
    /// The deadline passed and the tool's process tree was stopped. `may_have_changed` as for `Cancelled`.
    TimedOut { may_have_changed: bool },
    /// The tool wrote more than a stream's cap and its process tree was stopped.
    Output,
    /// The tool could not be run to an answer.
    Failed(Failure),
    /// The tool answered, and the answer cannot be used.
    Reply(ReplyProblem),
}

impl RunnerError {
    /// Whether the call ended because its task was cancelled.
    pub fn is_cancelled(&self) -> bool {
        matches!(self, RunnerError::Cancelled { .. })
    }

    fn from_run(error: TreeError, may_have_changed: bool) -> RunnerError {
        match error {
            TreeError::Cancelled => RunnerError::Cancelled { may_have_changed },
            TreeError::Timeout => RunnerError::TimedOut { may_have_changed },
            TreeError::TooLarge => RunnerError::Output,
            TreeError::NotAbsolute => RunnerError::Unavailable,
            TreeError::Spawn(error) if error.kind() == io::ErrorKind::NotFound => {
                RunnerError::Unavailable
            }
            TreeError::Spawn(error) | TreeError::Wait(error) => {
                RunnerError::Failed(Failure::Io(error.kind()))
            }
        }
    }
}

impl From<ReplyProblem> for RunnerError {
    fn from(problem: ReplyProblem) -> RunnerError {
        RunnerError::Reply(problem)
    }
}

/// The check that follows a stopped refresh.
const CHECK_AGAIN: &str = "it may already have saved a newer map, so check the status again";

impl Coded for RunnerError {
    fn code(&self) -> &'static str {
        match self {
            RunnerError::Unavailable => "OAH_PROJECT_TOOLS_UNAVAILABLE",
            RunnerError::Version => "OAH_PROJECT_TOOLS_VERSION",
            RunnerError::Request => "OAH_PROJECT_TOOLS_REQUEST",
            RunnerError::Cancelled { .. } => "OAH_PROJECT_TOOLS_CANCELLED",
            RunnerError::TimedOut { .. } => "OAH_PROJECT_TOOLS_TIMEOUT",
            RunnerError::Output => "OAH_PROJECT_TOOLS_OUTPUT",
            RunnerError::Failed(_) => "OAH_PROJECT_TOOLS_FAILED",
            RunnerError::Reply(_) => "OAH_PROJECT_TOOLS_REPLY",
        }
    }

    fn message(&self) -> String {
        match self {
            RunnerError::Unavailable => {
                String::from("the project tools are not installed with this app")
            }
            RunnerError::Version => format!(
                "the installed project tools are not the ones of OAH {}",
                crate::VERSION
            ),
            RunnerError::Request => {
                String::from("the project folder or the Git program is not an absolute path")
            }
            RunnerError::Cancelled {
                may_have_changed: false,
            } => String::from("the project tools were stopped before they changed anything"),
            RunnerError::Cancelled {
                may_have_changed: true,
            } => format!("the Impact Map refresh was stopped; {CHECK_AGAIN}"),
            RunnerError::TimedOut {
                may_have_changed: false,
            } => String::from("the project tools did not finish in time and were stopped"),
            RunnerError::TimedOut {
                may_have_changed: true,
            } => format!(
                "the Impact Map refresh did not finish in time and was stopped; {CHECK_AGAIN}"
            ),
            RunnerError::Output => {
                String::from("the project tools printed more than expected and were stopped")
            }
            RunnerError::Failed(Failure::Io(kind)) => {
                format!("the project tools could not be run ({kind})")
            }
            RunnerError::Failed(Failure::Exit(Some(code))) => {
                format!("the project tools ended with exit code {code}")
            }
            RunnerError::Failed(Failure::Exit(None)) => {
                String::from("the project tools were ended by a signal")
            }
            RunnerError::Reply(problem) => {
                format!("the project tools gave an answer OAH cannot use: {problem}")
            }
        }
    }
}

impl fmt::Display for RunnerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message())
    }
}

impl std::error::Error for RunnerError {}

/// The installed project tools: the one program this module may start.
#[derive(Debug, Clone)]
pub struct ProjectTools {
    program: PathBuf,
    deadlines: Deadlines,
}

impl ProjectTools {
    /// The tools at `program`, the path the app built from the folder of its own executable. Reads and starts
    /// nothing: a missing tool is reported by the first call.
    pub fn new(program: impl Into<PathBuf>) -> ProjectTools {
        ProjectTools {
            program: program.into(),
            deadlines: DEADLINES,
        }
    }

    /// Whether a tool is installed at the path this was made with. Starts nothing, so a status card may ask.
    pub fn installed(&self) -> bool {
        self.folder().is_ok()
    }

    /// Runs one Impact Map action for `request.root` and returns the tool's checked answer.
    ///
    /// Progress goes to `ctx.progress` (phase `impact-status` or `impact-refresh`, steps 0 to 2 of 2). The call
    /// ends early when `ctx.cancel` is cancelled; the tool's process tree is stopped before it returns.
    pub fn impact(
        &self,
        ctx: &Ctx<'_>,
        request: &ImpactRequest<'_>,
    ) -> Result<ImpactReply, RunnerError> {
        self.impact_in(ctx.env, ctx.cancel, ctx.progress, request)
    }

    fn impact_in(
        &self,
        env: &Env,
        cancel: &CancelToken,
        progress: &dyn ProgressSink,
        request: &ImpactRequest<'_>,
    ) -> Result<ImpactReply, RunnerError> {
        let ImpactRequest { action, root, git } = *request;
        if !root.is_absolute() || git.is_some_and(|git| !git.is_absolute()) {
            return Err(RunnerError::Request);
        }
        let folder = self.folder()?;
        let vars = tool_env(env, root, git.is_none());
        let step = |done| {
            progress.progress(Progress {
                phase: action.phase(),
                done,
                total: Some(STEPS),
            });
        };
        let before_start = |cancel: &CancelToken| match cancel.is_cancelled() {
            true => Err(RunnerError::Cancelled {
                may_have_changed: false,
            }),
            false => Ok(()),
        };

        step(0);
        self.check_version(folder, &vars, cancel)?;

        step(1);
        before_start(cancel)?;
        let mut args = vec![
            OsStr::new("impact"),
            OsStr::new(action.word()),
            OsStr::new("--root"),
            root.as_os_str(),
        ];
        if let Some(git) = git {
            args.extend([OsStr::new("--git"), git.as_os_str()]);
        }
        let (deadline, changes) = match action {
            ImpactAction::Status => (self.deadlines.status, false),
            ImpactAction::Refresh => (self.deadlines.refresh, true),
        };
        let output = self
            .start(folder, &vars, &args, deadline, ANSWER_LIMIT, cancel)
            .map_err(|error| RunnerError::from_run(error, changes))?;
        let reply = read_answer(action, root, &output)?;
        step(STEPS);
        Ok(reply)
    }

    /// The folder of the installed tool, or `Unavailable` for any other path. Starts nothing.
    fn folder(&self) -> Result<&Path, RunnerError> {
        let named = self
            .program
            .file_name()
            .and_then(OsStr::to_str)
            .is_some_and(|name| name.starts_with(RUNNER_NAME));
        match self.program.parent() {
            Some(folder) if named && self.program.is_absolute() && self.program.is_file() => {
                Ok(folder)
            }
            _ => Err(RunnerError::Unavailable),
        }
    }

    fn start(
        &self,
        folder: &Path,
        vars: &[(OsString, OsString)],
        args: &[&OsStr],
        deadline: Duration,
        stdout_limit: usize,
        cancel: &CancelToken,
    ) -> Result<Output, TreeError> {
        run_bundled(
            &TreeRun {
                program: &self.program,
                args,
                env: vars,
                cwd: folder,
                deadline,
                stdout_limit,
                stderr_limit: STDERR_LIMIT,
                grace: STOP_GRACE,
            },
            cancel,
            &[],
        )
    }
}

fn run_bundled(run: &TreeRun<'_>, cancel: &CancelToken, input: &[u8]) -> Result<Output, TreeError> {
    let execute = |spec: &TreeRun<'_>| {
        if input.is_empty() {
            run_tree(spec, cancel)
        } else {
            run_tree_input(spec, cancel, input)
        }
    };
    #[cfg(windows)]
    {
        let parent = run
            .env
            .iter()
            .find(|(name, value)| name == "TEMP" && Path::new(value).is_absolute())
            .map_or_else(std::env::temp_dir, |(_, value)| PathBuf::from(value));
        let scratch = tempfile::Builder::new()
            .prefix("oah-project-tools-")
            .tempdir_in(parent)
            .map_err(TreeError::Spawn)?;
        let mut env = run.env.to_vec();
        env.retain(|(name, _)| name != "TEMP" && name != "TMP");
        for name in ["TEMP", "TMP"] {
            env.push((OsString::from(name), scratch.path().as_os_str().to_owned()));
        }
        execute(&TreeRun { env: &env, ..*run })
    }
    #[cfg(not(windows))]
    execute(run)
}

/// The tool's whole environment for a request about `root`.
fn tool_env(env: &Env, root: &Path, with_path: bool) -> Vec<(OsString, OsString)> {
    let set = |name: &str| env.var(name).filter(|value| !value.is_empty());
    let mut vars: Vec<(OsString, OsString)> = PASSED
        .iter()
        .filter_map(|name| Some((OsString::from(name), set(name)?.to_os_string())))
        .collect();
    if with_path && let Some(path) = set("PATH").and_then(|path| path_outside(path, root)) {
        vars.push((OsString::from("PATH"), path));
    }
    if let Some(state) = set("OAH_STATE_DIR").map(Path::new) {
        let state = match state.is_absolute() {
            true => state.to_path_buf(),
            false => env.cwd().join(state),
        };
        vars.push((OsString::from("OAH_STATE_DIR"), state.into_os_string()));
    }
    vars
}

/// `path` with only its absolute entries outside the project, in their order; `None` when none is left. The tool
/// looks for `git` there and checks again what it finds.
fn path_outside(path: &OsStr, root: &Path) -> Option<OsString> {
    let real_root = fs::canonicalize(root).ok();
    let inside = |entry: &Path| {
        entry.starts_with(root)
            || match (fs::canonicalize(entry), &real_root) {
                (Ok(real), Some(real_root)) => real.starts_with(real_root),
                // An entry that cannot be resolved holds no program; it is judged by its text alone.
                _ => false,
            }
    };
    let kept: Vec<PathBuf> = std::env::split_paths(path)
        .filter(|entry| entry.is_absolute() && !inside(entry))
        .collect();
    match kept.is_empty() {
        true => None,
        false => std::env::join_paths(kept).ok(),
    }
}

/// The fields this module reads from the tool's answer. Everything else in it is ignored and never passed on.
#[derive(Deserialize)]
struct Answer {
    tool: Option<String>,
    action: Option<String>,
    ok: bool,
    state: MapState,
    performed: Performed,
    changed: u64,
    root: String,
    branch: Option<String>,
    head: Option<String>,
    map: Option<MapSummary>,
    graph: Option<GraphSummary>,
    runner: AnswerRunner,
    error: Option<AnswerError>,
}

#[derive(Deserialize)]
struct AnswerRunner {
    name: String,
    version: String,
}

#[derive(Deserialize)]
struct AnswerError {
    code: String,
}

/// Checks the finished run of `action` for `root` and turns its answer into a reply.
fn read_answer(
    action: ImpactAction,
    root: &Path,
    output: &Output,
) -> Result<ImpactReply, RunnerError> {
    let refused = match output.status.code() {
        Some(0) => false,
        Some(EXIT_REFUSED) => true,
        code => return Err(RunnerError::Failed(Failure::Exit(code))),
    };
    let document: serde_json::Value =
        serde_json::from_slice(&output.stdout).map_err(|_| ReplyProblem::Malformed)?;
    if !document.is_object() {
        return Err(ReplyProblem::Malformed.into());
    }
    if document.get("schema").and_then(serde_json::Value::as_u64) != Some(SCHEMA) {
        return Err(ReplyProblem::Schema.into());
    }
    let answer = Answer::deserialize(document).map_err(|_| ReplyProblem::Malformed)?;
    if answer.tool.as_deref() != Some("impact") || answer.action.as_deref() != Some(action.word()) {
        return Err(ReplyProblem::Request.into());
    }
    if answer.runner.name != RUNNER_NAME || answer.runner.version != crate::VERSION {
        return Err(ReplyProblem::Runner.into());
    }
    if !same_folder(Path::new(&answer.root), root) {
        return Err(ReplyProblem::Root.into());
    }
    let refusal = match (refused, answer.ok, answer.error) {
        (false, true, None) => None,
        (true, false, Some(error)) if is_code(&error.code) => Some(error.code),
        _ => return Err(ReplyProblem::Outcome.into()),
    };
    let changed_by_status = action == ImpactAction::Status
        && (answer.performed != Performed::None || answer.changed != 0);
    let commits = answer
        .head
        .iter()
        .chain(answer.map.iter().map(|map| &map.commit));
    if changed_by_status
        || !commits.into_iter().all(|id| is_commit_id(id))
        || !answer.branch.as_deref().is_none_or(is_branch)
    {
        return Err(ReplyProblem::Value.into());
    }
    Ok(ImpactReply {
        action,
        state: answer.state,
        performed: answer.performed,
        changed: answer.changed,
        head: answer.head,
        branch: answer.branch,
        map: answer.map,
        graph: answer.graph,
        refusal,
    })
}

/// Whether the answer's root is the folder that was asked about: the same text (a refusal repeats the argument),
/// or the same folder once links and letter case are resolved (an answer names the folder it found).
fn same_folder(answered: &Path, asked: &Path) -> bool {
    answered == asked
        || matches!(
            (fs::canonicalize(answered), fs::canonicalize(asked)),
            (Ok(answered), Ok(asked)) if answered == asked
        )
}

/// An `OAH_*` code as the tool writes them.
fn is_code(code: &str) -> bool {
    code.len() <= 64
        && code.strip_prefix("OAH_").is_some_and(|rest| {
            !rest.is_empty()
                && rest
                    .bytes()
                    .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
        })
}

/// A full commit id: 40 (SHA-1) or 64 (SHA-256) lowercase hexadecimal digits.
fn is_commit_id(id: &str) -> bool {
    matches!(id.len(), 40 | 64)
        && id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// A branch name that can be shown: not empty, not overlong, and without control characters.
fn is_branch(name: &str) -> bool {
    !name.is_empty() && name.len() <= 1024 && !name.chars().any(char::is_control)
}

#[cfg(test)]
#[path = "runner_tests.rs"]
mod tests;
