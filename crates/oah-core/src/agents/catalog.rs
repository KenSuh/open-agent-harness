// v0.4.0 도구 목록: doctor 의 결정표와 JSON 행이 읽는 14개 도구의 목적·로그인 명령·설치 안내를 그대로 옮긴다.
//! The v0.4.0 tool catalog as `doctor` reads it (v0.4:src/oah/setup/tools.py:117-174,
//! v0.4:src/oah/setup/companions.py:55-80). Install texts are stored as v0.4.0's `display` printed them, and a
//! note is stored only where it stands in for an empty install list, which is all `render_json` shows. Each
//! tool is looked up by its name with `--version`, as every v0.4.0 path tool was.

mod rows;

pub use rows::catalog;

use serde::{Deserialize, Serialize};

/// How much a tool matters (v0.4:src/oah/setup/tools.py:101-104).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Requirement {
    Required,
    Recommended,
    Optional,
}

/// A sign-in status command and the command a person runs to sign in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StatusCommand {
    pub args: &'static [&'static str],
    pub login: &'static str,
}

/// The install texts and the page shown for one system.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Guide {
    pub install: &'static [&'static str],
    pub url: &'static str,
}

/// One catalog row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Tool {
    pub name: &'static str,
    pub requirement: Requirement,
    pub purpose: &'static str,
    pub min_version: Option<[u64; 3]>,
    /// Counted as required in a project that uses gates.
    pub required_for_gates: bool,
    pub sign_in: Option<StatusCommand>,
    pub macos: Guide,
    pub windows: Guide,
}

impl Tool {
    /// The guide for v0.4.0's `platform` string: `win32` reads Windows, anything else macOS.
    pub fn guide(&self, platform: &str) -> Guide {
        if platform == "win32" {
            self.windows
        } else {
            self.macos
        }
    }
}
