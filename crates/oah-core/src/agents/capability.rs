// 능력과 근거: 능력마다 확인됨·모름·없음 가운데 하나와 근거(이식·문서·측정), 출처, 확인한 버전·시스템을 둔다.
//! Capabilities and their evidence (CONTRACTS C8). The value types of the write-bearing capabilities (`SwitchCaps`,
//! `ModelCaps`, `HookCaps`, `PackCaps`) arrive with the slices that use them; until then a capability carries `()`
//! and only its evidence.

use serde::{Deserialize, Serialize};

/// What is known about one capability. `Unknown` and `None` are never used by auto-apply and show the neutral
/// state (§4.1).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Capability<T> {
    Verified { value: T, evidence: Evidence },
    Unknown { why: &'static str },
    None,
}

impl<T> Capability<T> {
    /// The state as the wire shows it.
    pub fn state(&self) -> CapabilityState {
        match self {
            Capability::Verified { .. } => CapabilityState::Verified,
            Capability::Unknown { .. } => CapabilityState::Unknown,
            Capability::None => CapabilityState::None,
        }
    }
}

/// Why a capability is believed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Evidence {
    pub basis: Basis,
    /// Where the fact comes from: a v0.4.0 file and fixture, a design source, or a measuring command.
    pub source: &'static str,
    /// The agent's version a measurement saw; `None` for carried and documented facts.
    pub verified_on: Option<&'static str>,
    /// The systems the basis covers; on any other system the capability reads as unknown.
    pub os: &'static [Os],
}

/// What a fact rests on.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Basis {
    /// Pinned by a frozen v0.4.0 fixture.
    Carried,
    /// A cited source says so; not measured on any machine.
    Docs,
    /// Observed on a machine, with the agent's version.
    Measured,
}

/// An operating system a fact can cover.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Os {
    Macos,
    Windows,
    Linux,
}

impl Os {
    /// The system this build runs on.
    pub fn host() -> Os {
        if cfg!(windows) {
            Os::Windows
        } else if cfg!(target_os = "macos") {
            Os::Macos
        } else {
            Os::Linux
        }
    }

    pub fn parse(text: &str) -> Option<Os> {
        [Os::Macos, Os::Windows, Os::Linux]
            .into_iter()
            .find(|os| os.as_str() == text)
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Os::Macos => "macos",
            Os::Windows => "windows",
            Os::Linux => "linux",
        }
    }
}

/// A capability's state on the wire: `verified`, `unknown` or `none`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum CapabilityState {
    Verified,
    Unknown,
    None,
}

/// The capabilities the facts file records for every agent, in wire order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum CapabilityName {
    Detect,
    SignIn,
    SkillsLink,
    NativeSwitch,
    ModelWrite,
    ModelRead,
    HookPrompt,
    PackUpdate,
}

impl CapabilityName {
    pub const ALL: [CapabilityName; 8] = [
        CapabilityName::Detect,
        CapabilityName::SignIn,
        CapabilityName::SkillsLink,
        CapabilityName::NativeSwitch,
        CapabilityName::ModelWrite,
        CapabilityName::ModelRead,
        CapabilityName::HookPrompt,
        CapabilityName::PackUpdate,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            CapabilityName::Detect => "detect",
            CapabilityName::SignIn => "sign_in",
            CapabilityName::SkillsLink => "skills_link",
            CapabilityName::NativeSwitch => "native_switch",
            CapabilityName::ModelWrite => "model_write",
            CapabilityName::ModelRead => "model_read",
            CapabilityName::HookPrompt => "hook_prompt",
            CapabilityName::PackUpdate => "pack_update",
        }
    }

    pub fn parse(text: &str) -> Option<CapabilityName> {
        CapabilityName::ALL
            .into_iter()
            .find(|name| name.as_str() == text)
    }

    /// Whether using the capability makes OAH write under the agent's home (link visibility, native switch, model
    /// key and `model_read`, hook entry, pack hold). Such a capability is used only with carried or measured
    /// evidence for the system (DECISIONS D-07).
    pub fn writes_agent_home(self) -> bool {
        !matches!(self, CapabilityName::Detect | CapabilityName::SignIn)
    }
}
