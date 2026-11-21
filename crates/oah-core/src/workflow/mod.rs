// 팀 작업 흐름: 섹션 배정을 설정에 저장한 뒤 규칙 블록을 다시 맞추고, 단일 배너·프리셋·규칙 점검을 앱과 CLI 에 준다.
//! Team Workflow (design §6.1-§6.3, CONTRACTS C12, M2-S1).
//!
//! * [`set_stages`] is the one assignment operation: `config::save_project` with `Edit::stage` (a v0.4.0 file
//!   needs the approval of its exact bytes, and its backup is kept), then the rules plan and the rules write
//!   (`rules::sync`). A rules refusal after a saved config leaves the config written and returns `synced: false`
//!   with the reason and the *Sync now* action.
//! * [`status`] is the single banner, first match wins: an assigned agent not installed, not signed in, sign-in
//!   unknown, an `independent_of` rule broken, ready.
//! * [`presets`] set several sections at once; a preset naming an agent that is not installed is disabled.
//! * [`view`] is what the page and `oah sections show` show: cards from `sections.order`, the agent options
//!   (Cursor never offered), the banner, the presets, the rules state and the real config tables and block.
//!
//! Every renderer iterates `Effective.sections`; the section ids a preset names are design data (§6.1) and apply
//! only to sections that exist. Agent detection is a read of the caller's statuses (`app::agents_list`); an agent
//! not checked yet is never reported as missing.

mod ops;
pub mod presets;
pub mod status;

pub use ops::{
    preset_apply, rules_check, rules_check_at, rules_sync, rules_sync_at, rules_sync_for_project,
    sections_show, sections_show_for_project, set_stages, stages_set, view, workflow_status,
};
pub use presets::{PRESETS, Preset};
pub use status::{independence_breaks, status};

use crate::error::{Coded, codes};

/// A Team Workflow refusal that is not another module's error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkflowError {
    /// No project is open in the session.
    NoProject,
    ProjectChanged,
    /// The section is not in `sections.order`.
    UnknownSection {
        section: String,
    },
    /// The preset does not exist, or names an agent that is not installed here.
    PresetUnavailable {
        preset: String,
        reason: String,
    },
}

impl Coded for WorkflowError {
    fn code(&self) -> &'static str {
        match self {
            WorkflowError::NoProject => codes::PROJECT_NOT_OPEN,
            WorkflowError::ProjectChanged => codes::PROJECT_CHANGED,
            WorkflowError::UnknownSection { .. } => codes::SECTION_UNKNOWN,
            WorkflowError::PresetUnavailable { .. } => codes::PRESET_UNAVAILABLE,
        }
    }

    fn message(&self) -> String {
        match self {
            WorkflowError::NoProject => String::from("no project is open"),
            WorkflowError::ProjectChanged => String::from("the open project changed"),
            WorkflowError::UnknownSection { section } => {
                format!("{section:?} is not a section in sections.order; nothing was written")
            }
            WorkflowError::PresetUnavailable { preset, reason } => {
                format!("the preset {preset:?} cannot be applied: {reason}; nothing was written")
            }
        }
    }
}
