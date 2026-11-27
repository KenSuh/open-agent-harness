// 팀 작업 흐름 DTO: 섹션 카드·배정 선택지·단일 배너·프리셋·규칙 동기 상태·저장 결과의 직렬화 모양을 둔다.
//! Team Workflow shapes (M2-S1, CONTRACTS C12, C15): `sections_show` returns [`WorkflowDto`], `workflow_status`
//! returns [`BannerDto`], `stages_set` takes [`StagesSetReq`] and `preset_apply` takes [`PresetApplyReq`], both
//! returning [`StagesSetDto`]; `rules_check` returns [`RulesStateDto`] and `rules_sync` [`RulesSyncDto`]. Paths
//! here are fixed project-relative names such as `AGENTS.md`, never full paths. `project_tools_status` returns
//! [`ProjectToolsDto`]: the Project Wiki, Impact Map and Jev cards, read without changing or starting anything.

use super::{AgentIdDto, Empty, LoginHintDto, NoticeDto, SignInDto};
use crate::error::ErrorDto;
use crate::plan::{Action, ItemStatus};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowProjectReq<T = Empty> {
    pub project_id: String,
    pub input: T,
}

/// The Team Workflow page: one card per entry of `sections.order`, the agents a section may take, the one banner,
/// the presets, the rules state and what the Advanced disclosure shows.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WorkflowDto {
    pub sections: Vec<SectionDto>,
    /// The agents that can take a section, installed ones first; Cursor is never listed.
    pub agents: Vec<AgentOptionDto>,
    pub banner: BannerDto,
    pub presets: Vec<PresetDto>,
    pub rules: RulesStateDto,
    pub config_format: ConfigFormatDto,
    /// SHA-256 of the config file as read, lowercase hex; `null` when it does not exist. The upgrade approval
    /// of `stages_set` names it, so an approval is valid only for the bytes the warning was shown for.
    pub config_sha256: Option<String>,
    /// The one-time upgrade warning, verbatim (`config::UPGRADE_WARNING`), when the file is in v0.4.0 format.
    pub upgrade_warning: Option<String>,
    pub advanced: AdvancedDto,
    /// Notes that do not change the banner, such as an assignment that may not reach Copilot CLI (U-6).
    pub notes: Vec<String>,
    pub notices: Vec<NoticeDto>,
}

/// One section card.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SectionDto {
    pub id: String,
    pub label: String,
    /// `null` when a custom section has no `[stages]` key.
    pub agent: Option<AgentIdDto>,
    /// `file`, `legacy:<key>` (a v0.4.0 key such as `legacy:initial`), `default` or `unassigned`.
    pub source: String,
    pub independent_of: Vec<String>,
    pub skills: Vec<String>,
}

/// One agent a section may take.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AgentOptionDto {
    pub id: AgentIdDto,
    pub name: String,
    /// `null` until the agent has been checked on this machine.
    pub installed: Option<bool>,
    pub sign_in: SignInDto,
    /// Why the option is shown disabled; a signed-out agent is not disabled.
    pub disabled_reason: Option<String>,
}

/// The config file's generation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ConfigFormatDto {
    Missing,
    Legacy,
    Current,
}

/// A section and the agent the banner names.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BannerSectionDto {
    pub id: String,
    pub label: String,
    pub agent: AgentIdDto,
}

/// An agent the banner names, with how to sign in.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BannerAgentDto {
    pub id: AgentIdDto,
    pub name: String,
    pub login_hint: LoginHintDto,
}

/// A broken `independent_of` rule: `section` and `other` are done by the same `agent`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BannerPairDto {
    pub section: String,
    pub other: String,
    pub agent: AgentIdDto,
}

/// The one banner, first match wins (design §6.3). The UI renders it and never re-derives it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum BannerDto {
    /// An assigned agent is not installed here; `suggestion` is the first installed, signed-in agent that keeps
    /// every `independent_of` rule of those sections.
    NotInstalled {
        message: String,
        sections: Vec<BannerSectionDto>,
        suggestion: Option<AgentIdDto>,
    },
    NotSignedIn {
        message: String,
        agents: Vec<BannerAgentDto>,
    },
    SignInUnknown {
        message: String,
        agents: Vec<BannerAgentDto>,
    },
    NotIndependent {
        message: String,
        pairs: Vec<BannerPairDto>,
    },
    Ready {
        message: String,
    },
}

/// One section a preset sets.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PresetStageDto {
    pub section: String,
    pub agent: AgentIdDto,
}

/// A preset; disabled with the reason when it names an agent that is not installed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PresetDto {
    pub id: String,
    pub label: String,
    pub stages: Vec<PresetStageDto>,
    pub disabled_reason: Option<String>,
}

/// A drift finding of `rules_check`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DriftDto {
    pub code: String,
    pub path: String,
    pub message: String,
}

/// A file a sync would write.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PlannedWriteDto {
    pub path: String,
    pub action: Action,
}

/// The rules state: in sync, drift with the files a sync would write, or the refusal a sync would hit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RulesStateDto {
    pub synced: bool,
    pub drift: Vec<DriftDto>,
    pub writes: Vec<PlannedWriteDto>,
    pub error: Option<ErrorDto>,
}

/// The real config tables and the real block text, as they are on disk.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AdvancedDto {
    /// The `[sections]` and `[stages]` tables of `.oah/config.toml` as TOML; empty when the file has neither.
    pub config_tables: String,
    /// The managed block of `AGENTS.md`, markers included; `null` when there is none.
    pub block: Option<String>,
}

/// One assignment change.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StageChangeReq {
    pub section: String,
    pub agent: AgentIdDto,
}

/// One `independent_of` list to write; an empty list switches the rule off.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndependenceReq {
    pub section: String,
    pub of: Vec<String>,
}

/// `stages_set`: the changes, written in one config save, then the rules re-sync.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct StagesSetReq {
    pub stages: Vec<StageChangeReq>,
    pub independent_of: Vec<IndependenceReq>,
    /// The `config_sha256` of the v0.4.0 file whose upgrade warning the user accepted; absent otherwise.
    pub approve_upgrade: Option<String>,
}

/// `preset_apply`: a preset id and, for a v0.4.0 file, the accepted upgrade.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct PresetApplyReq {
    pub preset: String,
    pub approve_upgrade: Option<String>,
}

/// What an assignment did: the config save, the backup of an upgraded v0.4.0 file, and the rules re-sync. When the
/// config was saved and the rules refused, `synced` is `false`, `sync_error` holds the reason and `actions`
/// offers `rules_sync` (*Sync now*); the saved assignment stays.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StagesSetDto {
    pub saved: bool,
    pub upgraded: bool,
    /// The backup's project-relative path, when this save upgraded a v0.4.0 file.
    pub backup: Option<String>,
    pub synced: bool,
    pub sync_error: Option<ErrorDto>,
    pub actions: Vec<String>,
    pub workflow: WorkflowDto,
}

/// A project tool the Team Workflow page shows a card for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ProjectToolIdDto {
    Wiki,
    ImpactMap,
    Jev,
}

/// What a [`ToolPathDto`] path is relative to. No card carries a full path.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ToolPathBaseDto {
    /// The project root.
    Project,
    /// The repository's common Git folder, shared by its worktrees.
    GitCommonDir,
    /// The state folder `OAH_STATE_DIR` names.
    StateDir,
}

/// One path a card names, written with `/` and relative to `base`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ToolPathDto {
    pub label: String,
    pub path: String,
    pub base: ToolPathBaseDto,
}

/// One project tool card: its name, what was found as text, and the one next step. `state` is a stable kebab
/// word for tests and scripts (`workflow::tools` lists each tool's words); a person reads `status`, `reason`
/// and `next_step`, which the UI and the CLI show as they are.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProjectToolDto {
    pub id: ProjectToolIdDto,
    pub name: String,
    pub state: String,
    pub status: String,
    /// Why, when the status is a problem; `null` otherwise.
    pub reason: Option<String>,
    pub next_step: String,
    pub paths: Vec<ToolPathDto>,
    pub notes: Vec<String>,
}

/// `project_tools_status`: the three cards of the project `project_id` names. A caller that has moved to another
/// project meanwhile compares `project_id` and drops the answer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProjectToolsDto {
    pub project_id: String,
    pub tools: Vec<ProjectToolDto>,
}

/// One file a `rules_sync` wrote or tried.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RulesWriteDto {
    pub path: String,
    pub action: Action,
    pub status: ItemStatus,
}

/// `rules_sync`: every file written, and why it stopped, if it did.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RulesSyncDto {
    pub synced: bool,
    pub written: Vec<RulesWriteDto>,
    pub error: Option<ErrorDto>,
}
