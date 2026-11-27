// 직렬화 모양: CLI --json 과 데스크톱 IPC 가 함께 쓰는 DTO 를 영역별로 두고, 사람에게 보일 경로는 홈을 ~ 로 쓴다.
//! Serialized shapes shared by CLI `--json` and IPC (§2.4, CONTRACTS C3).
//!
//! JSON field names are `snake_case` and enum values lowercase kebab strings. Every command takes one `req`
//! argument: a request DTO here, or [`Empty`] for `{}`. A path shown to a person is a display string with the
//! home folder written as `~` ([`display_path`]); no DTO carries a secret or an environment value.
//!
//! The samples in `crates/oah-core/tests/dto_samples/<area>/<name>.json` are the contract artefact: a test
//! serializes fixed values and compares them byte for byte, and the UI tests load the same files.

mod agents;
mod app;
mod impact_map;
mod notice;
mod plan;
mod project;
mod project_tools_settings;
mod review;
mod skills;
mod task;
mod wiki;
mod workflow;

pub use agents::{
    AgentIdDto, AgentStatusDto, AgentsChangedDto, AgentsProbeReq, CapabilitiesDto, LoginHintDto,
    SignInDto, rfc3339_utc,
};
pub use app::{
    AppInfoDto, CliDto, CliState, RefreshDto, SettingEdit, SettingValue, SettingsDto,
    SettingsSetReq, UiDto,
};
pub use impact_map::{ImpactGraphState, ImpactMapAction, ImpactMapRunDto, ImpactMapRunReq};
pub use notice::{NoticeDto, NoticeLevel};
pub use plan::{ApplyReportDto, BasisDto, ChangePlanDto, ItemResultDto, PlanItemDto};
pub use project::{ConfigStateDto, ProjectDto, ProjectKindDto};
pub use project_tools_settings::{
    ProjectToolsApplyDto, ProjectToolsApplyReq, ProjectToolsPlanDto, ProjectToolsPlanReq,
    ToolSettingValue, ToolsBackupDto, ToolsBackupStateDto, ToolsKeptDto, ToolsKeyChangeDto,
    ToolsUpgradeDto,
};
pub use review::{
    GateDecisionDto, GateReasonDto, ReviewOutcomeDto, ReviewReq, ReviewSpecDto, kind_name,
    result_name,
};
pub use skills::{
    ApplyProjectSkillDto, ApplyProjectSkillReq, ApplyProjectSkillStatus, InstalledSkillDto,
    InstalledSkillManagementDto, InstalledSkillTarget, LinkedSkillSourceDto, ManagedSkillDto,
    RegisterSkillSourceReq, RemoveInstalledSkillReq, RemovedSkillDto, SkillScopeDto,
};
pub use task::{Progress, TaskCancelReq, TaskEvent, TaskId, TaskSnapshotDto, TaskState};
pub use wiki::{
    ProjectWikiDto, WikiActionKind, WikiActionReq, WikiApplyDto, WikiApplyReq, WikiBackupDto,
    WikiBackupState, WikiConfiguredDto, WikiCountsDto, WikiFileAction, WikiPlanDto,
    WikiPlanFileDto, WikiPlanMode, WikiProblemDto, WikiSearchDto, WikiSearchReq,
    WikiSettingChangeDto, WikiSettingKeptDto, WikiSettingValue, WikiSettingsPlanDto,
    WikiSettingsResultDto, WikiSettingsState, WikiTargetDto, WikiTargetState, WikiUpgradeDto,
};
pub use workflow::{
    AdvancedDto, AgentOptionDto, BannerAgentDto, BannerDto, BannerPairDto, BannerSectionDto,
    ConfigFormatDto, DriftDto, IndependenceReq, PlannedWriteDto, PresetApplyReq, PresetDto,
    PresetStageDto, ProjectToolDto, ProjectToolIdDto, ProjectToolsDto, RulesStateDto, RulesSyncDto,
    RulesWriteDto, SectionDto, StageChangeReq, StagesSetDto, StagesSetReq, ToolPathBaseDto,
    ToolPathDto, WorkflowDto, WorkflowProjectReq,
};

use crate::paths::{Env, canonical_folder, user_home};
use serde::{Deserialize, Serialize};
use std::path::Path;

/// `{}`: the request of a command that takes nothing, and the response of one that returns nothing.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Empty {}

/// `path` as a person sees it: the home folder written as `~`. Without a usable home it is the full path.
pub fn display_path(env: &Env, path: &Path) -> String {
    display_text(env, &path.display().to_string())
}

/// `text` with every whole occurrence of the home folder written as `~`.
pub(crate) fn display_text(env: &Env, text: &str) -> String {
    let Ok(home) = user_home(env) else {
        return text.to_string();
    };
    let configured = home.to_string_lossy();
    match canonical_folder(&home) {
        Ok(resolved) if resolved != home => {
            let resolved = resolved.to_string_lossy();
            let (first, second) = if configured.len() > resolved.len() {
                (configured.as_ref(), resolved.as_ref())
            } else {
                (resolved.as_ref(), configured.as_ref())
            };
            replace_home(&replace_home(text, first), second)
        }
        _ => replace_home(text, &configured),
    }
}

fn replace_home(text: &str, home: &str) -> String {
    let home = home.trim_end_matches(['/', '\\']);
    if home.is_empty() {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut copied = 0;
    let mut search = 0;
    while let Some(found) = text[search..].find(home) {
        let at = search + found;
        let end = at + home.len();
        let before = text[..at].chars().next_back();
        let after = text[end..].chars().next();
        if !before.is_some_and(continues_path) && !after.is_some_and(continues_name) {
            out.push_str(&text[copied..at]);
            out.push('~');
            copied = end;
        }
        search = end;
    }
    out.push_str(&text[copied..]);
    out
}

/// A character that can sit right before a path inside a longer path.
fn continues_path(c: char) -> bool {
    c.is_alphanumeric() || matches!(c, '/' | '\\' | '.' | '_' | '-' | '~')
}

/// A character that would make the match only the start of a longer file name.
fn continues_name(c: char) -> bool {
    c.is_alphanumeric() || matches!(c, '.' | '_' | '-' | '~')
}

#[cfg(test)]
mod tests {
    use super::replace_home;

    #[cfg(any(unix, windows))]
    #[test]
    fn configured_and_resolved_home_paths_have_the_same_display() {
        use super::{Env, canonical_folder, display_path, display_text};

        let temp = tempfile::tempdir().unwrap();
        let root = canonical_folder(temp.path()).unwrap();
        let home = root.join("home");
        let alias = root.join("home-alias");
        std::fs::create_dir(&home).unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&home, &alias).unwrap();
        #[cfg(windows)]
        junction::create(&home, &alias).unwrap();
        let home_var = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
        let env = Env::new(&home).unwrap().with(home_var, &alias);
        let configured_file = alias.join("settings.toml");
        let resolved_file = home.join("settings.toml");
        let expected = std::path::Path::new("~").join("settings.toml");
        let message = format!(
            "{} and {}",
            configured_file.display(),
            resolved_file.display()
        );

        assert_eq!(
            display_path(&env, &configured_file),
            expected.to_string_lossy()
        );
        assert_eq!(
            display_path(&env, &resolved_file),
            expected.to_string_lossy()
        );
        assert_eq!(
            display_text(&env, &message),
            format!("{0} and {0}", expected.display())
        );
        #[cfg(unix)]
        std::fs::remove_file(&alias).unwrap();
        #[cfg(windows)]
        junction::delete(&alias).unwrap();
    }

    #[test]
    fn only_whole_home_paths_become_a_tilde() {
        let home = "/opt/sample";
        assert_eq!(replace_home("/opt/sample", home), "~");
        assert_eq!(replace_home("/opt/sample/", home), "~/");
        assert_eq!(
            replace_home("/opt/sample/.oah/bin/oah", home),
            "~/.oah/bin/oah"
        );
        assert_eq!(
            replace_home("cannot read /opt/sample/a: denied", home),
            "cannot read ~/a: denied"
        );
        assert_eq!(replace_home("/opt/samples/x", home), "/opt/samples/x");
        assert_eq!(replace_home("/srv/opt/sample/x", home), "/srv/opt/sample/x");
        assert_eq!(
            replace_home("/opt/sample/a and /opt/sample/b", home),
            "~/a and ~/b"
        );
        assert_eq!(replace_home(r"C:\Sample\x", r"C:\Sample\"), r"~\x");
        assert_eq!(replace_home("x", "/"), "x");
    }
}
