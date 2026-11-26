// 한 에이전트 확인: 설정한 명령을 끼운 확인 통로로 설치를 보고, 쓸 수 있을 때만 로그인을 보며, 둘을 화면에 보일 한 상태로 합친다.
//! One agent's probe: detection, then sign-in when the detection is usable, both through the project's configured
//! command when it gives one, and the status they make together (§4.2).

use super::adapters::{adapter, profile};
use super::detection::{Detection, SignIn, Status};
use super::opencode::{self, OpenCodeDetails};
use super::probe::Probe;
use super::problem::AgentError;
use super::system::Configured;
use crate::config::AgentId;

/// The outcome of a sign-in check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SignInCheck {
    /// Not run: sign-in was not asked for, or the detection was not usable.
    NotChecked,
    Checked(SignIn),
    /// The status command could not run or finish; the agent's status is `error`, and it stays installed.
    Failed(AgentError),
}

/// One agent's detection and sign-in check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Check {
    pub detection: Detection,
    pub sign_in: SignInCheck,
    pub opencode: Option<OpenCodeDetails>,
}

impl Check {
    pub(crate) fn can_read_details(&self) -> bool {
        self.detection.status == Status::Ok || opencode::supports(&self.detection)
    }

    pub(crate) fn read_details(&mut self, probe: &dyn Probe, command: Option<&[String]>) {
        let id = self.detection.agent;
        if opencode::supports(&self.detection) {
            self.opencode = Some(with_command(id, probe, command, opencode::read));
        } else {
            self.sign_in = check_sign_in(id, probe, command, &self.detection);
        }
    }

    /// The status shown for the agent: the detection's, refined by the sign-in check of a usable detection.
    pub fn status(&self) -> Status {
        match (self.detection.status, &self.sign_in) {
            (Status::Ok, SignInCheck::Checked(SignIn::SignedIn)) => Status::Ok,
            (Status::Ok, SignInCheck::Checked(SignIn::SignedOut { .. })) => Status::NotSignedIn,
            (Status::Ok, SignInCheck::Failed(_)) => Status::Error,
            (Status::Ok, _) => Status::SignInUnknown,
            (status, _) => status,
        }
    }

    /// Why the status is `error` or `unsupported-shim`.
    pub fn problem(&self) -> Option<&AgentError> {
        match &self.sign_in {
            SignInCheck::Failed(problem) => Some(problem),
            _ => self.detection.problem.as_ref(),
        }
    }
}

/// Detects `id`. `command` is the project's `[agents.<id>] command` when the file gives one: it replaces the
/// `PATH` lookup of the agent's command name (v0.4:src/oah/models.py:203-218).
pub fn detect(id: AgentId, probe: &dyn Probe, command: Option<&[String]>) -> Detection {
    with_command(id, probe, command, |probe| adapter(id).detect(probe))
}

/// Checks the sign-in of a usable `detection` of `id`; anything else is not checked.
pub fn check_sign_in(
    id: AgentId,
    probe: &dyn Probe,
    command: Option<&[String]>,
    detection: &Detection,
) -> SignInCheck {
    if detection.status != Status::Ok {
        return SignInCheck::NotChecked;
    }
    with_command(id, probe, command, |probe| {
        match adapter(id).sign_in(probe, detection) {
            Ok(sign_in) => SignInCheck::Checked(sign_in),
            Err(problem) => SignInCheck::Failed(problem),
        }
    })
}

fn with_command<R>(
    id: AgentId,
    probe: &dyn Probe,
    command: Option<&[String]>,
    run: impl FnOnce(&dyn Probe) -> R,
) -> R {
    match (command, profile(id).command) {
        (Some(command), Some(name)) => run(&Configured {
            inner: probe,
            name,
            command,
        }),
        _ => run(probe),
    }
}
