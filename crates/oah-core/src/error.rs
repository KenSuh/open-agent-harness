// 공통 오류: 모듈별 오류를 하나의 Error 로 묶고, 화면과 CLI 가 함께 쓰는 {code, message, hint} 로 바꾼다.
//! The one error type core operations return, and its serialized form `{code, message, hint}` (§2.4, C3).
//!
//! Module errors keep their own types; [`Error`] wraps them, one variant per module. Every variant has a stable
//! `OAH_*` code through [`Coded`]. [`Error::to_dto`] writes the home folder as `~` in the message, so no full user
//! path crosses IPC; [`hint_for`] is the one table of hints.

use crate::agents::AgentError;
use crate::config::ConfigError;
use crate::fsx::FsxError;
use crate::paths::{Env, PathsError};
use crate::plan::PlanError;
use crate::project::ProjectError;
use crate::tasks::{Cancelled, TaskError};
use serde::Serialize;
use std::fmt;

/// The codes M1-S1 and M1-S2 add (CONTRACTS C16).
pub mod codes {
    /// A task of the same kind and subject is already running.
    pub const TASK_BUSY: &str = "OAH_TASK_BUSY";
    /// The task stopped at a cancellation check before it published anything. Shown as the `cancelled` state.
    pub const TASK_CANCELLED: &str = "OAH_TASK_CANCELLED";
    /// A basis of a change plan moved, at the initial preflight or later.
    pub const PLAN_STALE: &str = "OAH_PLAN_STALE";
    /// The CLI copy failed before publishing; the previous copy, or none, is still in place.
    pub const CLI_COPY_FAILED: &str = "OAH_CLI_COPY_FAILED";
    /// The chosen path is not a folder.
    pub const PROJECT_NOT_FOLDER: &str = "OAH_PROJECT_NOT_FOLDER";
    /// An agent's detection or sign-in command could not launch, timed out or exited with an error (M1-S2).
    pub const AGENT_PROBE_FAILED: &str = "OAH_AGENT_PROBE_FAILED";
    /// A Windows batch file OAH does not launch: not an npm shim it can map to `node` (v0.4.0 code, M1-S2).
    pub const UNSUPPORTED_BATCH_SHIM: &str = "OAH_UNSUPPORTED_BATCH_SHIM";
    /// The program found under an agent's command name does not report itself as that agent (M1-S2).
    pub const AGENT_UNIDENTIFIED: &str = "OAH_AGENT_UNIDENTIFIED";
}

/// A failure with a stable code and a message for a person.
pub trait Coded {
    fn code(&self) -> &'static str;
    fn message(&self) -> String;
}

/// Every failure a core operation can return.
#[derive(Debug)]
pub enum Error {
    Paths(PathsError),
    Fsx(FsxError),
    Config(ConfigError),
    Task(TaskError),
    Plan(PlanError),
    Project(ProjectError),
    Agents(AgentError),
}

/// The serialized error: `code` is stable, `message` and `hint` are English text for a person.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ErrorDto {
    pub code: String,
    pub message: String,
    pub hint: Option<String>,
}

impl Error {
    /// The serialized form, with the home folder written as `~` in the message.
    pub fn to_dto(&self, env: &Env) -> ErrorDto {
        let code = self.code();
        ErrorDto {
            code: code.to_string(),
            message: crate::dto::display_text(env, &self.message()),
            hint: hint_for(code).map(str::to_string),
        }
    }

    /// Whether this is the stop of a cancelled task.
    pub fn is_cancelled(&self) -> bool {
        matches!(self, Error::Task(TaskError::Cancelled))
    }
}

/// The hint shown under a message, for the codes that have one.
pub fn hint_for(code: &str) -> Option<&'static str> {
    Some(match code {
        codes::TASK_BUSY => "Wait for the running task to finish, or cancel it, then try again.",
        codes::PLAN_STALE => {
            "Files changed after the plan was made. Review the new plan, then apply it again."
        }
        codes::CLI_COPY_FAILED => {
            "OAH keeps working. Agent hooks use the previous copy until a later start copies it again."
        }
        codes::PROJECT_NOT_FOLDER => "Choose a folder, not a file.",
        codes::AGENT_PROBE_FAILED => {
            "Run the agent's command in a terminal to see what is wrong, then refresh."
        }
        codes::UNSUPPORTED_BATCH_SHIM => {
            "Install the tool's native executable, or configure an explicit command such as \
             [\"node.exe\", \"<path to the script>\"] in .oah/config.toml."
        }
        codes::AGENT_UNIDENTIFIED => {
            "Another program uses this name. Fix PATH, or set the agent's command in .oah/config.toml."
        }
        "OAH_PROJECT_MISSING" => "Check that the folder still exists, then open it again.",
        "OAH_HOME_UNAVAILABLE" => "Set OAH_HOME, or make sure the home folder is set.",
        "OAH_GIT_MISSING" => "Install Git or add it to PATH, then open the folder again.",
        "OAH_CONFIG_INVALID" => "Fix the file, or remove it to use the defaults.",
        "OAH_CONFIG_CHANGED" => "The file changed while OAH was saving it. Try again.",
        "OAH_CONFIG_KEY_NOT_OWNED" => "OAH changes only its own settings.",
        "OAH_FILE_BUSY" => {
            "Another OAH window or command is writing this file. Try again in a moment."
        }
        "OAH_FILE_IO" => "Check that the folder exists and can be written.",
        "OAH_FILE_UNVERIFIED" => "Refresh to read the file again.",
        _ => return None,
    })
}

impl Coded for Error {
    fn code(&self) -> &'static str {
        match self {
            Error::Paths(error) => error.code(),
            Error::Fsx(error) => error.code(),
            Error::Config(error) => error.code(),
            Error::Task(error) => error.code(),
            Error::Plan(error) => error.code(),
            Error::Project(error) => error.code(),
            Error::Agents(error) => error.code(),
        }
    }

    fn message(&self) -> String {
        match self {
            Error::Paths(error) => error.message().to_string(),
            Error::Fsx(error) => fsx_message(error),
            Error::Config(error) => error.message().to_string(),
            Error::Task(error) => Coded::message(error),
            Error::Plan(error) => Coded::message(error),
            Error::Project(error) => Coded::message(error),
            Error::Agents(error) => Coded::message(error),
        }
    }
}

/// An fsx error's text without its leading code.
fn fsx_message(error: &FsxError) -> String {
    let text = error.to_string();
    text.strip_prefix(error.code())
        .and_then(|rest| rest.strip_prefix(": "))
        .unwrap_or(&text)
        .to_string()
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code(), self.message())
    }
}

impl std::error::Error for Error {}

impl From<PathsError> for Error {
    fn from(error: PathsError) -> Self {
        Error::Paths(error)
    }
}

impl From<FsxError> for Error {
    fn from(error: FsxError) -> Self {
        Error::Fsx(error)
    }
}

impl From<ConfigError> for Error {
    fn from(error: ConfigError) -> Self {
        Error::Config(error)
    }
}

impl From<TaskError> for Error {
    fn from(error: TaskError) -> Self {
        Error::Task(error)
    }
}

impl From<Cancelled> for Error {
    fn from(_: Cancelled) -> Self {
        Error::Task(TaskError::Cancelled)
    }
}

impl From<PlanError> for Error {
    fn from(error: PlanError) -> Self {
        Error::Plan(error)
    }
}

impl From<ProjectError> for Error {
    fn from(error: ProjectError) -> Self {
        Error::Project(error)
    }
}

impl From<AgentError> for Error {
    fn from(error: AgentError) -> Self {
        Error::Agents(error)
    }
}
