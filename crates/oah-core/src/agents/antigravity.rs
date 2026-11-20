// Antigravity 어댑터: PATH 의 agy 나 문서에 적힌 기본 폴더의 agy 로만 설치를 보고, 명령은 실행하지 않으며 로그인은 알 수 없다고 둔다.
//! Antigravity (§4.2, [S11]): installed when `agy` resolves on `PATH` or in its documented default folder, with
//! the same lookup rules. Nothing is run, so there is no version line; the app bundle and a sign-in status command
//! are open (U-8a).

use super::adapters::AgentAdapter;
use super::capability::Os;
use super::detection::{Detection, SignIn, Status};
use super::probe::{Probe, Resolution};
use super::problem::AgentError;
use crate::config::AgentId;
use crate::dto::display_path;
use crate::paths::{Env, user_home};
use std::path::PathBuf;

const COMMAND: &str = "agy";

pub(crate) struct Antigravity;

/// The documented folder of `agy` outside `PATH` [S11]: `<home>/.local/bin`; on Windows `%LOCALAPPDATA%\agy\bin`.
pub(crate) fn default_folder(env: &Env, os: Os) -> Option<PathBuf> {
    match os {
        Os::Windows => env
            .var("LOCALAPPDATA")
            .map(PathBuf::from)
            .filter(|folder| folder.is_absolute())
            .map(|folder| folder.join("agy").join("bin")),
        Os::Macos | Os::Linux => user_home(env)
            .ok()
            .map(|home| home.join(".local").join("bin")),
    }
}

impl AgentAdapter for Antigravity {
    fn id(&self) -> AgentId {
        AgentId::Antigravity
    }

    fn detect(&self, probe: &dyn Probe) -> Detection {
        let resolution = match probe.resolve(COMMAND) {
            Resolution::Missing => match default_folder(probe.env(), probe.os()) {
                Some(folder) => probe.resolve_in(COMMAND, &folder),
                None => Resolution::Missing,
            },
            other => other,
        };
        let env = probe.env();
        match resolution {
            Resolution::Missing => Detection::missing(self.id()),
            Resolution::UnsupportedShim { path, reason } => {
                let source = Some(display_path(env, &path));
                Detection::failed(
                    self.id(),
                    None,
                    source,
                    AgentError::UnsupportedShim { path, reason },
                )
            }
            Resolution::Found(program) => Detection::found(
                self.id(),
                Status::SignInUnknown,
                None,
                display_path(env, &program.source),
            ),
        }
    }

    fn sign_in(&self, _probe: &dyn Probe, _detection: &Detection) -> Result<SignIn, AgentError> {
        Ok(SignIn::Unknown)
    }
}
