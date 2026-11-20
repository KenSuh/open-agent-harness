// Cursor 어댑터: 문서에 적힌 응용 프로그램 묶음이 있을 때만 설치로 보고, 같은 이름의 agent 명령은 정체를 확인하기 전까지 보지 않는다.
//! Cursor (§4.2): installed only when its application bundle exists at the path DESIGN-C2 names for macOS, which
//! is not yet verified (U-9); no Windows path is documented, so Cursor reads as missing there with detection
//! `unknown` in its capabilities. The `agent` command is not consulted: a bare name is not identity (N-7).

use super::adapters::AgentAdapter;
use super::capability::Os;
use super::detection::{Detection, SignIn, Status};
use super::probe::Probe;
use super::problem::AgentError;
use crate::config::AgentId;
use std::path::Path;

/// The macOS application bundle of DESIGN-C2 §4.2.
const MACOS_BUNDLE: &str = "/Applications/Cursor.app";

pub(crate) struct Cursor;

/// The documented bundle path on `os`, if any.
fn bundle(os: Os) -> Option<&'static str> {
    (os == Os::Macos).then_some(MACOS_BUNDLE)
}

impl AgentAdapter for Cursor {
    fn id(&self) -> AgentId {
        AgentId::Cursor
    }

    fn detect(&self, probe: &dyn Probe) -> Detection {
        match bundle(probe.os()) {
            Some(path) if probe.exists(Path::new(path)) => {
                Detection::found(self.id(), Status::SignInUnknown, None, path.to_string())
            }
            _ => Detection::missing(self.id()),
        }
    }

    fn sign_in(&self, _probe: &dyn Probe, _detection: &Detection) -> Result<SignIn, AgentError> {
        Ok(SignIn::Unknown)
    }
}
