// 프로젝트 위키 작업 DTO: 위키 카드의 개요·미리보기·적용·검색 요청과 응답 모양을 둔다. 모든 경로는 프로젝트 기준 상대 경로다.
//! The Project Wiki actions of the Team Workflow page (core `project_tools::wiki_actions`).
//!
//! Every request travels inside [`WorkflowProjectReq`](super::WorkflowProjectReq) and every answer names the
//! project it is about. A path is relative to the project and written with `/`. No DTO carries a root path, the
//! bytes of a file or of the settings, a key or an environment value.

use crate::error::ErrorDto;
use crate::project_tools::wiki::{
    ApplyItem, CopyBlocked, LinkNote, WikiCandidate, WikiSearchHit, WikiSkip,
};
use serde::{Deserialize, Serialize};

/// What the user asks for. The same value is sent for the preview and, unchanged, for the apply.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "kebab-case", deny_unknown_fields)]
pub enum WikiActionReq {
    /// Use a wiki folder where it is and add the standard files it does not have. Without `selected` the folder
    /// is the one the settings name, else the one folder found, else the default folder, which is created.
    Setup {
        /// One of the folders the overview lists.
        #[serde(default)]
        selected: Option<String>,
        /// The date (`YYYY-MM-DD`) the preview wrote into the new log; an apply sends the preview's date back so
        /// that the same files are made after midnight. Without it core uses today's date (UTC).
        #[serde(default)]
        today: Option<String>,
    },
    /// Copy one of the folders the overview lists to the standard folder. The source stays as it is.
    Copy { source: String },
}

/// `start_project_wiki_apply`'s input: the request, the plan it was shown as and nothing else.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WikiApplyReq {
    pub request: WikiActionReq,
    pub plan_id: String,
    /// The plan's `settings.upgrade.fingerprint`, sent only after the user acknowledged that plan's upgrade.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub approve_upgrade: Option<String>,
}

/// `project_wiki_search`'s input. Without `dir` the wiki folder in use is searched.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WikiSearchReq {
    /// The folder the settings name or one of the folders the overview lists.
    #[serde(default)]
    pub dir: Option<String>,
    pub query: String,
    #[serde(default)]
    pub max_hits: Option<usize>,
}

/// `[wiki] dir` as the settings file has it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiConfiguredDto {
    pub dir: String,
    /// `false` when the file does not set it and `dir` is the default.
    pub explicit: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiProblemDto {
    pub dir: String,
    pub error: ErrorDto,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum WikiTargetState {
    /// The folder is there with every standard file and the settings lead to it.
    Ready,
    /// The folder is there; standard files are missing or the settings do not name it yet.
    Incomplete,
    /// No wiki folder: setting up creates the default one.
    None,
    /// More than one folder looks like the wiki; one must be chosen.
    Choose,
    /// The settings name a folder that does not exist. Nothing is created in its place.
    ConfiguredMissing,
    /// The folder or the settings cannot be used; `error` says why.
    Unavailable,
}

/// The wiki folder a setup without a choice would use, and what it would do.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiTargetDto {
    pub state: WikiTargetState,
    pub dir: Option<String>,
    /// How many standard files a setup would add.
    pub creates: usize,
    /// Whether a setup would also set `[wiki] dir`.
    pub sets_dir: bool,
    pub error: Option<ErrorDto>,
}

/// `project_wiki_overview`: the folders that look like a wiki and the one in use. Reads only.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProjectWikiDto {
    pub project_id: String,
    /// The standard folder, where a copy goes: `docs/wiki`.
    pub default_dir: String,
    /// `None` when the settings file cannot be read; `settings_error` says why.
    pub configured: Option<WikiConfiguredDto>,
    pub settings_error: Option<ErrorDto>,
    pub candidates: Vec<WikiCandidate>,
    pub problems: Vec<WikiProblemDto>,
    /// The search for folders stopped at a bound, so a folder may be missing from `candidates`.
    pub truncated: bool,
    pub target: WikiTargetDto,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum WikiActionKind {
    Setup,
    Copy,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum WikiPlanMode {
    /// The folder does not exist and is created with the standard files.
    Create,
    /// The folder exists; only missing standard files are added.
    Adopt,
    /// Files are copied to the standard folder; the source stays.
    Copy,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum WikiFileAction {
    Create,
    /// The file exists and is not touched.
    Keep,
    /// The name is taken by something that is not a regular file.
    Blocked,
    Copy,
    /// The destination already has this file with the same bytes.
    Same,
}

/// One file of a plan, relative to the wiki folder (`dir`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiPlanFileDto {
    pub path: String,
    pub action: WikiFileAction,
    /// The size of a file to copy; `None` for a setup.
    pub bytes: Option<u64>,
}

/// A value of one settings key as the file writes it: text, a switch or a list of text.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum WikiSettingValue {
    Text(String),
    Switch(bool),
    List(Vec<String>),
}

/// One key the save changes; `None` is a key the file does not have. `key` is dotted, such as `wiki.dir`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiSettingChangeDto {
    pub key: String,
    pub before: Option<WikiSettingValue>,
    pub after: Option<WikiSettingValue>,
}

/// A value the plan leaves as the file has it, with the reason (`explicit-wiki-dirs`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiSettingKeptDto {
    pub key: String,
    pub value: WikiSettingValue,
    pub reason: String,
}

/// For a v0.4.0 settings file: the one-way upgrade to acknowledge, for exactly the bytes `fingerprint` names.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiUpgradeDto {
    pub message: String,
    pub fingerprint: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum WikiBackupState {
    Write,
    Reuse,
    Conflict,
}

/// For a v0.4.0 settings file: what the save finds at the backup path.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiBackupDto {
    pub state: WikiBackupState,
    pub path: String,
}

/// The settings change that follows the files, saved only after every file is in place and verified.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiSettingsPlanDto {
    /// The target file: `.oah/config.toml`.
    pub config_path: String,
    pub changes: Vec<WikiSettingChangeDto>,
    pub kept: Vec<WikiSettingKeptDto>,
    pub upgrade: Option<WikiUpgradeDto>,
    pub backup: Option<WikiBackupDto>,
}

/// `project_wiki_plan`: what the request would do, computed by reading only.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiPlanDto {
    pub project_id: String,
    /// Names the project, the request, the folders and files as they are now, the settings file's bytes now and
    /// after, and its upgrade. An apply is refused unless the disk still gives this id.
    pub plan_id: String,
    pub action: WikiActionKind,
    /// The wiki folder after the apply: the folder set up, or the destination of a copy.
    pub dir: String,
    /// The folder copied from; `None` for a setup.
    pub source: Option<String>,
    pub mode: WikiPlanMode,
    /// The date written into a new log; `None` for a copy. An apply sends it back in `request.today`.
    pub today: Option<String>,
    /// Folders that would be created, relative to the project.
    pub folders: Vec<String>,
    pub files: Vec<WikiPlanFileDto>,
    /// How many files the apply would write.
    pub writes: usize,
    /// What stops a copy until it is resolved; `blocked_total` counts all of it.
    pub blocked: Vec<CopyBlocked>,
    pub blocked_total: usize,
    /// Entries of the source that are deliberately not copied.
    pub left: Vec<String>,
    /// Links that already lead nowhere in the source.
    pub unresolved_links: Vec<LinkNote>,
    pub unresolved_total: usize,
    /// `None` when the settings already lead to `dir`, or a copy has no file to switch to.
    pub settings: Option<WikiSettingsPlanDto>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize)]
pub struct WikiCountsDto {
    pub created: usize,
    pub kept: usize,
    pub failed: usize,
    pub pending: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum WikiSettingsState {
    /// The plan had no settings change.
    Unchanged,
    Saved,
    /// Not attempted, because not every file is in place.
    Skipped,
    /// The settings file changed after the preview. The files stay; preview again to save the setting.
    Stale,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiSettingsResultDto {
    pub state: WikiSettingsState,
    pub config_path: String,
    pub upgraded: bool,
    /// Where the original v0.4.0 file is kept, relative to the project; `None` without an upgrade.
    pub backup: Option<String>,
    pub error: Option<ErrorDto>,
}

/// The `done` payload of a wiki apply: what happened to each file, then to the settings.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiApplyDto {
    pub project_id: String,
    pub plan_id: String,
    pub action: WikiActionKind,
    pub dir: String,
    pub source: Option<String>,
    pub folders_created: Vec<String>,
    pub items: Vec<ApplyItem>,
    pub counts: WikiCountsDto,
    /// Why the files stopped; `OAH_WIKI_STOPPED` when the user stopped the apply. `None` when all are in place.
    pub stop: Option<ErrorDto>,
    pub settings: WikiSettingsResultDto,
    /// Every file is in place and the settings lead to `dir`.
    pub complete: bool,
}

/// `project_wiki_search`: bounded hits, each with the file relative to `dir`, the line and the heading above it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiSearchDto {
    pub project_id: String,
    pub dir: String,
    pub hits: Vec<WikiSearchHit>,
    /// More hits exist than were returned, or a bound stopped the search.
    pub truncated: bool,
    pub files_searched: usize,
    pub skipped: Vec<WikiSkip>,
    pub skipped_total: usize,
}
