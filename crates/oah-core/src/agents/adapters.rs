// 어댑터 계약과 목록: 일곱 에이전트마다 설치·로그인 확인 방법을 한 트레이트로 두고, 이름·종류·로그인 안내·설치 주소를 함께 둔다.
//! The adapter contract (CONTRACTS C8: the methods M1-S2 uses; `skill_dirs`, `native_switch`, `model`, `hooks` and
//! `pack_update` arrive with the slices that consume them) and the seven adapters with their display facts
//! (§4.2, §6.3).

use super::antigravity::Antigravity;
use super::cli::{CLAUDE, CODEX, COPILOT, GEMINI, OPENCODE};
use super::cursor::Cursor;
use super::detection::{Detection, Kind, LoginHint, SignIn};
use super::probe::Probe;
use super::problem::AgentError;
use crate::config::AgentId;

/// How OAH checks one agent.
pub trait AgentAdapter: Sync {
    fn id(&self) -> AgentId;
    /// Whether the agent is installed, from a program or an application bundle only, never from a folder or a
    /// settings file (§4.2). It touches nothing but `probe`.
    fn detect(&self, probe: &dyn Probe) -> Detection;
    /// The sign-in state of a detected agent: `Unknown` without a verified status command. A status command that
    /// cannot run or finish is that agent's error, never another's. Only its exit code and `loggedIn` are read.
    fn sign_in(&self, probe: &dyn Probe, detection: &Detection) -> Result<SignIn, AgentError>;
    fn can_take_section(&self) -> bool {
        self.id().can_own_section()
    }
}

/// What a person sees about an agent before anything is probed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Profile {
    pub name: &'static str,
    pub kind: Kind,
    pub login: LoginHint,
    pub install_url: &'static str,
    /// The command name a project's `[agents.<id>] command` replaces; `None` for an editor found by its bundle.
    pub command: Option<&'static str>,
}

/// The adapter of `id`.
pub fn adapter(id: AgentId) -> &'static dyn AgentAdapter {
    match id {
        AgentId::Claude => &CLAUDE,
        AgentId::Codex => &CODEX,
        AgentId::Gemini => &GEMINI,
        AgentId::Copilot => &COPILOT,
        AgentId::Opencode => &OPENCODE,
        AgentId::Antigravity => &Antigravity,
        AgentId::Cursor => &Cursor,
    }
}

const fn hint(text: &'static str, is_command: bool) -> LoginHint {
    LoginHint { text, is_command }
}

/// The display facts of `id`. Install pages: v0.4.0's for Claude Code and Codex (v0.4:src/oah/setup/tools.py),
/// DESIGN-C2 §13 sources for the others, except Cursor's download page, which §13 does not list.
pub fn profile(id: AgentId) -> Profile {
    let (name, kind, login, install_url, command) = match id {
        AgentId::Claude => (
            "Claude Code",
            Kind::Cli,
            hint("claude auth login", true),
            "https://code.claude.com/docs/en/setup",
            Some("claude"),
        ),
        AgentId::Codex => (
            "Codex",
            Kind::Cli,
            hint("codex login", true),
            "https://github.com/openai/codex",
            Some("codex"),
        ),
        AgentId::Gemini => (
            "Gemini CLI",
            Kind::Cli,
            hint("Run gemini; it asks you to sign in on first run", false),
            "https://github.com/google-gemini/gemini-cli",
            Some("gemini"),
        ),
        AgentId::Copilot => (
            "Copilot CLI",
            Kind::Cli,
            hint("copilot login", true),
            "https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli",
            Some("copilot"),
        ),
        AgentId::Opencode => (
            "OpenCode",
            Kind::Cli,
            hint("Open opencode and run /connect", false),
            "https://opencode.ai/docs/",
            Some("opencode"),
        ),
        AgentId::Antigravity => (
            "Antigravity",
            Kind::Cli,
            hint("Open agy and sign in in the browser", false),
            "https://antigravity.google/docs/install",
            Some("agy"),
        ),
        AgentId::Cursor => (
            "Cursor",
            Kind::Ide,
            hint("Cursor Settings → Sign In", false),
            "https://cursor.com/download",
            None,
        ),
    };
    Profile {
        name,
        kind,
        login,
        install_url,
        command,
    }
}
