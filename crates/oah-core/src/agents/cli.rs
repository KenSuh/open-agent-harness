// 명령줄 에이전트 어댑터: PATH 의 프로그램을 버전 인자로 실행해 설치와 정체를 보고, 확인된 상태 명령이 있을 때만 로그인을 본다.
//! The five command-line adapters (§4.2): Claude Code and Codex from the carried v0.4.0 probes
//! (v0.4:src/oah/setup/tools.py:141-149), Gemini CLI, Copilot CLI and OpenCode from their documented version
//! commands with sign-in left unknown (U-1, U-4, U-7a).

use super::adapters::{AgentAdapter, profile};
use super::detection::{Detection, SignIn, Status};
use super::probe::{Probe, Resolution, Resolved};
use super::problem::{AgentError, Failure};
use super::steps::{SignInRun, VersionRun, run_sign_in, run_version};
use crate::config::AgentId;
use crate::dto::display_path;
use crate::paths::Env;
use std::time::Duration;

/// How long one version or status command may run (v0.4.0's doctor timeout).
pub(crate) const DEADLINE: Duration = Duration::from_secs(10);

/// An agent found as a program on `PATH`.
pub(crate) struct CliAdapter {
    id: AgentId,
    command: &'static str,
    version_args: &'static [&'static str],
    /// The verified sign-in status command; `None` leaves sign-in unknown.
    status_args: Option<&'static [&'static str]>,
    /// The version-line shape that confirms the program is this agent (N-7); `None` when no shape is evidenced.
    identity: Option<fn(&str) -> bool>,
}

fn claude_code(line: &str) -> bool {
    line.ends_with("(Claude Code)")
}

fn codex_cli(line: &str) -> bool {
    line.starts_with("codex-cli ")
}

pub(crate) static CLAUDE: CliAdapter = CliAdapter {
    id: AgentId::Claude,
    command: "claude",
    version_args: &["--version"],
    status_args: Some(&["auth", "status"]),
    identity: Some(claude_code),
};

pub(crate) static CODEX: CliAdapter = CliAdapter {
    id: AgentId::Codex,
    command: "codex",
    version_args: &["--version"],
    status_args: Some(&["login", "status"]),
    identity: Some(codex_cli),
};

pub(crate) static GEMINI: CliAdapter = CliAdapter {
    id: AgentId::Gemini,
    command: "gemini",
    version_args: &["--version"],
    status_args: None,
    identity: None,
};

pub(crate) static COPILOT: CliAdapter = CliAdapter {
    id: AgentId::Copilot,
    command: "copilot",
    version_args: &["version"],
    status_args: None,
    identity: None,
};

pub(crate) static OPENCODE: CliAdapter = CliAdapter {
    id: AgentId::Opencode,
    command: "opencode",
    version_args: &["--version"],
    status_args: None,
    identity: None,
};

impl CliAdapter {
    /// The command as a person would type it, for messages.
    fn typed(&self, args: &[&str]) -> String {
        format!("{} {}", self.command, args.join(" "))
    }

    fn failed(&self, args: &[&str], failure: Failure) -> AgentError {
        AgentError::ProbeFailed {
            command: self.typed(args),
            failure,
        }
    }
}

fn shown(env: &Env, program: &Resolved) -> Option<String> {
    Some(display_path(env, &program.source))
}

impl AgentAdapter for CliAdapter {
    fn id(&self) -> AgentId {
        self.id
    }

    fn detect(&self, probe: &dyn Probe) -> Detection {
        let env = probe.env();
        let args = self.version_args;
        match run_version(probe, self.command, args, DEADLINE) {
            VersionRun::Missing => Detection::missing(self.id),
            VersionRun::UnsupportedShim { path, reason } => {
                let source = Some(display_path(env, &path));
                Detection::failed(
                    self.id,
                    None,
                    source,
                    AgentError::UnsupportedShim { path, reason },
                )
            }
            VersionRun::Launch { program, reason } => Detection::failed(
                self.id,
                None,
                shown(env, &program),
                self.failed(args, Failure::Launch(reason)),
            ),
            VersionRun::TimedOut { program } => Detection::failed(
                self.id,
                None,
                shown(env, &program),
                self.failed(args, Failure::TimedOut(DEADLINE)),
            ),
            VersionRun::Exited {
                program,
                exit,
                version,
            } if exit != Some(0) => Detection::failed(
                self.id,
                version,
                shown(env, &program),
                self.failed(args, Failure::Exit(exit)),
            ),
            VersionRun::Exited {
                program, version, ..
            } => {
                let source = shown(env, &program);
                if let Some(identity) = self.identity
                    && !version.as_deref().is_some_and(identity)
                {
                    let name = profile(self.id).name;
                    let problem = AgentError::Unidentified {
                        name,
                        source: program.source,
                    };
                    return Detection::failed(self.id, None, source, problem);
                }
                let status = if self.status_args.is_some() {
                    Status::Ok
                } else {
                    Status::SignInUnknown
                };
                Detection::found(self.id, status, version, source.unwrap_or_default())
            }
        }
    }

    fn sign_in(&self, probe: &dyn Probe, detection: &Detection) -> Result<SignIn, AgentError> {
        let Some(args) = self.status_args.filter(|_| detection.status == Status::Ok) else {
            return Ok(SignIn::Unknown);
        };
        let Resolution::Found(program) = probe.resolve(self.command) else {
            return Ok(SignIn::Unknown);
        };
        match run_sign_in(probe, &program, args, DEADLINE) {
            SignInRun::SignedIn => Ok(SignIn::SignedIn),
            SignInRun::SignedOut => Ok(SignIn::SignedOut {
                login: profile(self.id).login,
            }),
            SignInRun::Launch(reason) => Err(self.failed(args, Failure::Launch(reason))),
            SignInRun::TimedOut => Err(self.failed(args, Failure::TimedOut(DEADLINE))),
        }
    }
}
