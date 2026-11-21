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
use crate::rules::RulesError;
use crate::tasks::{Cancelled, TaskError};
use crate::workflow::WorkflowError;
use serde::Serialize;
use std::fmt;

/// The codes M1-S1, M1-S2 and M2-S1 add (CONTRACTS C16).
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
    /// Warning: a section and a section it lists in `independent_of` are done by the same agent (M2-S1, v0.4.0's
    /// code). It never blocks.
    pub const REVIEW_NOT_INDEPENDENT: &str = "OAH_REVIEW_NOT_INDEPENDENT";
    /// The config was saved and the rules re-sync was refused; the reason follows and *Sync now* is offered (M2-S1).
    pub const RULES_NOT_SYNCED: &str = "OAH_RULES_NOT_SYNCED";
    /// A Team Workflow or rules operation needs an open project (M2-S1).
    pub const PROJECT_NOT_OPEN: &str = "OAH_PROJECT_NOT_OPEN";
    pub const PROJECT_CHANGED: &str = "OAH_PROJECT_CHANGED";
    /// An assignment names a section that is not in `sections.order` (M2-S1).
    pub const SECTION_UNKNOWN: &str = "OAH_SECTION_UNKNOWN";
    /// A preset that does not exist or names an agent that is not installed here (M2-S1).
    pub const PRESET_UNAVAILABLE: &str = "OAH_PRESET_UNAVAILABLE";
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
    Rules(RulesError),
    Workflow(WorkflowError),
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
        codes::REVIEW_NOT_INDEPENDENT => {
            "Assign a different agent to one of the two sections, or switch the rule off. This only warns."
        }
        codes::RULES_NOT_SYNCED => {
            "The assignment is saved. Fix the reason shown, then use Sync now or run `oah rules sync`."
        }
        codes::PROJECT_NOT_OPEN => "Open a project folder first.",
        codes::PROJECT_CHANGED => "Open the intended project and repeat the action.",
        codes::SECTION_UNKNOWN => "Add the section id to sections.order in .oah/config.toml first.",
        codes::PRESET_UNAVAILABLE => {
            "Install the agents the preset names, or assign each section by hand."
        }
        "OAH_CONFIG_UPGRADE_REQUIRED" => {
            "Read the upgrade warning, then save again with its approval. Nothing was written."
        }
        "OAH_AGENT_DEPLOY_ONLY" => {
            "Cursor receives the rules but cannot take a section; choose another agent."
        }
        "OAH_RULES_BLOCK_TOO_LARGE" | "OAH_RULES_BUDGET" => {
            "Shorten the text named in the message, then sync again. AGENTS.md was not changed."
        }
        "OAH_RULES_LINK" => "Replace the link with a regular file, then sync again.",
        "OAH_RULES_MARKER_BROKEN" | "OAH_RULES_MARKER_DUPLICATE" => {
            "Fix the marker lines by hand, then sync again. Nothing was written."
        }
        "OAH_RULES_NEWER" => "Upgrade OAH; this block was written by a newer version.",
        "OAH_RULES_ENCODING" => "Save the file as UTF-8, then sync again.",
        "OAH_RULES_VERIFY" => "Refresh to read the files again, then sync again.",
        "OAH_RULES_DRIFT" => "Run Sync now or `oah rules sync` to restore the managed text.",
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
            Error::Rules(error) => Coded::code(error),
            Error::Workflow(error) => error.code(),
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
            Error::Rules(error) => Coded::message(error),
            Error::Workflow(error) => Coded::message(error),
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

impl From<RulesError> for Error {
    fn from(error: RulesError) -> Self {
        Error::Rules(error)
    }
}

impl From<WorkflowError> for Error {
    fn from(error: WorkflowError) -> Self {
        Error::Workflow(error)
    }
}
