// 에이전트 DTO: 에이전트 상태 목록, 확인 요청, 바뀐 에이전트 알림의 직렬화 모양과 확인 시각 문자열을 둔다.
//! Agent shapes (CONTRACTS C8, M1-S2): `agents_list` and `start_agents_probe` return [`AgentStatusDto`]s, the
//! probe takes [`AgentsProbeReq`], and the shell emits `agents-changed` with [`AgentsChangedDto`]. No field carries
//! a status command's text: a problem is a coded error whose message names only the command and the reason.

use crate::agents::{
    CapabilityName, CapabilityState, Check, Kind, OpenCodeDetails, Os, SignIn, SignInCheck, Status,
    facts, installed, profile,
};
use crate::config::AgentId;
use crate::error::{Error, ErrorDto};
use crate::paths::Env;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::time::{SystemTime, UNIX_EPOCH};

/// An agent id on the wire: its config id (`"claude"`, `"codex"`, …).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct AgentIdDto(pub AgentId);

impl Serialize for AgentIdDto {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.0.as_str())
    }
}

impl<'de> Deserialize<'de> for AgentIdDto {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<AgentIdDto, D::Error> {
        let text = String::deserialize(deserializer)?;
        AgentId::parse(&text)
            .map(AgentIdDto)
            .ok_or_else(|| serde::de::Error::custom(format!("unknown agent {text}")))
    }
}

/// `start_agents_probe`'s request; every field may be left out.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct AgentsProbeReq {
    /// The agents to probe; absent for all seven.
    pub agents: Option<Vec<AgentIdDto>>,
    /// Also check sign-in; `false` probes detection only.
    pub sign_in: bool,
    /// The window regained focus: sign-in is checked again only for an agent whose last check is 60 seconds old
    /// or older. A rescan leaves it `false`.
    pub focus: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub opencode_details: Option<bool>,
}

/// The sign-in state shown for an agent.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SignInDto {
    SignedIn,
    SignedOut,
    Unknown,
}

/// How to sign in; the UI offers a copy button only when `is_command` (§6.3).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LoginHintDto {
    pub text: String,
    pub is_command: bool,
}

/// Each capability's state on the probed system (CONTRACTS C8, D-07).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct CapabilitiesDto {
    pub detect: CapabilityState,
    pub sign_in: CapabilityState,
    pub skills_link: CapabilityState,
    pub native_switch: CapabilityState,
    pub model_write: CapabilityState,
    pub model_read: CapabilityState,
    pub hook_prompt: CapabilityState,
    pub pack_update: CapabilityState,
}

/// One agent as the roster and `oah doctor --json` show it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AgentStatusDto {
    pub id: AgentIdDto,
    pub name: String,
    pub kind: Kind,
    pub installed: bool,
    pub status: Status,
    /// The version line of the version run; never status-command text.
    pub version: Option<String>,
    /// Where the program or bundle was found, with the home folder as `~`.
    pub source: Option<String>,
    pub sign_in: SignInDto,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub opencode: Option<OpenCodeDetails>,
    pub login_hint: LoginHintDto,
    pub install_url: String,
    pub can_take_section: bool,
    pub capabilities: CapabilitiesDto,
    /// Why the status is `error` or `unsupported-shim`; `null` otherwise.
    pub problem: Option<ErrorDto>,
    /// When this status was found, RFC 3339 in UTC.
    pub checked_at: String,
}

impl AgentStatusDto {
    /// The status `check` shows, probed on `os` and found at `checked_at`.
    pub fn new(env: &Env, check: &Check, os: Os, checked_at: SystemTime) -> AgentStatusDto {
        let id = check.detection.agent;
        let profile = profile(id);
        let version = check.detection.version.as_deref();
        let state = |name| facts().evaluate(id, name, os, version).state();
        AgentStatusDto {
            id: AgentIdDto(id),
            name: profile.name.to_string(),
            kind: profile.kind,
            installed: installed(&check.detection).is_some(),
            status: check.status(),
            version: check.detection.version.clone(),
            source: check.detection.source.clone(),
            sign_in: match check.sign_in {
                SignInCheck::Checked(SignIn::SignedIn) => SignInDto::SignedIn,
                SignInCheck::Checked(SignIn::SignedOut { .. }) => SignInDto::SignedOut,
                _ => SignInDto::Unknown,
            },
            opencode: check.opencode.clone().or_else(|| {
                check
                    .supports_opencode_details()
                    .then(OpenCodeDetails::default)
            }),
            login_hint: LoginHintDto {
                text: profile.login.text.to_string(),
                is_command: profile.login.is_command,
            },
            install_url: profile.install_url.to_string(),
            can_take_section: id.can_own_section(),
            capabilities: CapabilitiesDto {
                detect: state(CapabilityName::Detect),
                sign_in: state(CapabilityName::SignIn),
                skills_link: state(CapabilityName::SkillsLink),
                native_switch: state(CapabilityName::NativeSwitch),
                model_write: state(CapabilityName::ModelWrite),
                model_read: state(CapabilityName::ModelRead),
                hook_prompt: state(CapabilityName::HookPrompt),
                pack_update: state(CapabilityName::PackUpdate),
            },
            problem: check
                .problem()
                .map(|problem| Error::from(problem.clone()).to_dto(env)),
            checked_at: rfc3339_utc(checked_at),
        }
    }
}

/// The `agents-changed` event: the agents whose status a finished probe or refresh may have changed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AgentsChangedDto {
    pub agents: Vec<AgentIdDto>,
}

/// `time` as `YYYY-MM-DDTHH:MM:SSZ`; a time before 1970 reads as the epoch.
pub fn rfc3339_utc(time: SystemTime) -> String {
    let seconds = time
        .duration_since(UNIX_EPOCH)
        .map_or(0, |since| since.as_secs());
    let (hour, minute, second) = (seconds / 3600 % 24, seconds / 60 % 60, seconds % 60);
    // Days to a civil date in the proleptic Gregorian calendar (H. Hinnant, `civil_from_days`).
    let days = i64::try_from(seconds / 86_400).unwrap_or(0) + 719_468;
    let (era, day_of_era) = (days.div_euclid(146_097), days.rem_euclid(146_097));
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}
