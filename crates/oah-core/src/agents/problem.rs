// 에이전트 확인 오류: 확인·로그인 명령 실패, 실행하지 않는 배치 파일, 다른 제품인 프로그램을 코드와 함께 알린다.
//! Why an agent's status is `error` or `unsupported-shim` (CONTRACTS C8: an `error` status carries a code).

use crate::error::{Coded, codes};
use std::path::PathBuf;
use std::time::Duration;

/// How a probe command failed. The command's output is never part of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Failure {
    /// It could not be started; the reason as the system gave it.
    Launch(String),
    /// It was stopped at the deadline.
    TimedOut(Duration),
    /// The version run ended with a code other than 0 (`None`: without a code).
    Exit(Option<i32>),
}

/// A failure of one agent's detection or sign-in check. It fails only that agent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentError {
    /// `command` (the agent's command name and arguments, as a person would type them) failed.
    ProbeFailed { command: String, failure: Failure },
    /// A Windows batch file OAH never launches (v0.4:src/oah/engines/proc.py:1-29).
    UnsupportedShim { path: PathBuf, reason: String },
    /// The program found under the agent's command name does not report itself as the agent (N-7).
    Unidentified { name: &'static str, source: PathBuf },
}

impl Coded for AgentError {
    fn code(&self) -> &'static str {
        match self {
            AgentError::ProbeFailed { .. } => codes::AGENT_PROBE_FAILED,
            AgentError::UnsupportedShim { .. } => codes::UNSUPPORTED_BATCH_SHIM,
            AgentError::Unidentified { .. } => codes::AGENT_UNIDENTIFIED,
        }
    }

    fn message(&self) -> String {
        match self {
            AgentError::ProbeFailed { command, failure } => match failure {
                Failure::Launch(reason) => format!("'{command}' could not launch: {reason}"),
                Failure::TimedOut(after) => {
                    format!("'{command}' timed out after {}s", seconds(*after))
                }
                Failure::Exit(code) => format!("'{command}' exited with code {}", exit_text(*code)),
            },
            AgentError::UnsupportedShim { path, reason } => {
                format!("{}: {reason}", path.display())
            }
            AgentError::Unidentified { name, source } => {
                format!(
                    "the program at {} does not report itself as {name}",
                    source.display()
                )
            }
        }
    }
}

/// A deadline in seconds as v0.4.0 printed it with `{:g}` (`10.0` → `10`).
pub(crate) fn seconds(duration: Duration) -> String {
    duration.as_secs_f64().to_string()
}

/// An exit code as v0.4.0 printed it; Python wrote a missing code as `None`.
pub(crate) fn exit_text(code: Option<i32>) -> String {
    code.map_or_else(|| String::from("None"), |code| code.to_string())
}
