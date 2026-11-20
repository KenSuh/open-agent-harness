// 확인 결과 타입: 상태·버전·출처와 로그인 상태, 로그인 안내, 그리고 설치된 에이전트만 만들 수 있는 증표를 둔다.
//! What detection and sign-in report (CONTRACTS C8, §4.2), and the [`InstalledAgent`] witness.

use super::problem::AgentError;
use crate::config::AgentId;
use serde::{Deserialize, Serialize};

/// An agent's state: v0.4.0's statuses plus `sign-in-unknown` for an agent with no verified status command
/// (v0.4:src/oah/doctor.py:31, §4.2).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Status {
    Ok,
    Missing,
    TooOld,
    NotSignedIn,
    SignInUnknown,
    UnsupportedShim,
    Error,
}

/// What a detection of one agent found.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Detection {
    pub agent: AgentId,
    pub status: Status,
    /// The version line of the version run (v0.4.0 `first_line`), never status-command text.
    pub version: Option<String>,
    /// Where the program or bundle was found, as found.
    pub source: Option<String>,
    /// Why the status is `error` or `unsupported-shim`; `None` otherwise.
    pub problem: Option<AgentError>,
}

impl Detection {
    pub(crate) fn missing(agent: AgentId) -> Detection {
        Detection {
            agent,
            status: Status::Missing,
            version: None,
            source: None,
            problem: None,
        }
    }

    /// A program or bundle found at `source` (a display string), in `status`.
    pub(crate) fn found(
        agent: AgentId,
        status: Status,
        version: Option<String>,
        source: String,
    ) -> Detection {
        Detection {
            agent,
            status,
            version,
            source: Some(source),
            problem: None,
        }
    }

    /// A detection that failed with `problem`: `unsupported-shim` for a refused batch file, else `error`.
    pub(crate) fn failed(
        agent: AgentId,
        version: Option<String>,
        source: Option<String>,
        problem: AgentError,
    ) -> Detection {
        let status = match problem {
            AgentError::UnsupportedShim { .. } => Status::UnsupportedShim,
            _ => Status::Error,
        };
        Detection {
            agent,
            status,
            version,
            source,
            problem: Some(problem),
        }
    }
}

/// The sign-in state an agent reports.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignIn {
    SignedIn,
    SignedOut {
        login: LoginHint,
    },
    /// No verified status command, or no check was made.
    Unknown,
}

/// How to sign in. `is_command` decides whether the UI offers a copy button (§6.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LoginHint {
    pub text: &'static str,
    pub is_command: bool,
}

/// Whether the agent is a command-line agent or an editor.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Kind {
    Cli,
    Ide,
}

/// Proof that an agent was detected as installed. Every function that writes under an agent's home takes one,
/// so nothing can be written for a missing agent (DECISIONS D-08). Only [`installed`] builds it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstalledAgent {
    id: AgentId,
    source: Option<String>,
}

impl InstalledAgent {
    pub fn id(&self) -> AgentId {
        self.id
    }

    /// Where the agent was found.
    pub fn source(&self) -> Option<&str> {
        self.source.as_deref()
    }
}

/// The witness for the detected agent when `detection` shows it installed: any status but `missing`,
/// `unsupported-shim` and `error`. A folder, a config file or a skills directory never gets here; only a
/// detection does.
pub fn installed(detection: &Detection) -> Option<InstalledAgent> {
    match detection.status {
        Status::Missing | Status::UnsupportedShim | Status::Error => None,
        Status::Ok | Status::TooOld | Status::NotSignedIn | Status::SignInUnknown => {
            Some(InstalledAgent {
                id: detection.agent,
                source: detection.source.clone(),
            })
        }
    }
}
