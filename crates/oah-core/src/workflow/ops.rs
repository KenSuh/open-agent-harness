// 팀 작업 흐름 작업: 연 프로젝트의 설정·규칙 파일을 읽어 화면 모양을 만들고, 배정을 저장한 뒤 규칙을 다시 맞춘다.
//! The M2-S1 operations of CONTRACTS C15: `sections_show` (read), `workflow_status` (read), `stages_set` and
//! `preset_apply` (write-own), `rules_check` (read) and `rules_sync` (write-own). Each `*_at` / [`view`] /
//! [`set_stages`] form takes the project root and the agent statuses explicitly; the session forms take the open
//! project and the session's agent cache (`app::agents_list`), so the app and the CLI share one implementation.

use super::presets::PRESETS;
use super::status::{independence_breaks, status};
use super::{ConfigPathRefusal, WorkflowError};
use crate::app::{Ctx, agents_list};
use crate::config::{
    AgentId, AssignmentSource, ConfigFile, Edit, Effective, Format, ProjectConfig, Table,
    UPGRADE_WARNING, Value, backup_path, plan_project_save, save_project,
};
use crate::dto::{
    AdvancedDto, AgentIdDto, AgentOptionDto, AgentStatusDto, BannerDto, ConfigFormatDto, DriftDto,
    Empty, NoticeDto, NoticeLevel, PlannedWriteDto, PresetApplyReq, RulesStateDto, RulesSyncDto,
    RulesWriteDto, SectionDto, SignInDto, StageChangeReq, StagesSetDto, StagesSetReq, WorkflowDto,
    WorkflowProjectReq,
};
use crate::error::{Error, codes, hint_for};
use crate::fsx::{self, Fingerprint, FsxError, LockOptions};
use crate::paths::Env;
use crate::plan::ItemStatus;
use crate::rules::body::{AlwaysSkill, agent_name};
use crate::rules::input::{ExpectedEntry, InspectedEntry, inspect_project_entry};
use crate::rules::sync::read_block;
use crate::rules::{RulesSnapshot, SyncInput, SyncResult, apply_sync, plan_sync};
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

/// The action id of *Sync now*: the `rules_sync` command.
const SYNC_NOW: &str = "rules_sync";
const CONFIG_DIR_LABEL: &str = ".oah";
const CONFIG_FILE_LABEL: &str = ".oah/config.toml";
const CONFIG_LOCK_LABEL: &str = ".oah/config.toml.lock";
const CONFIG_BACKUP_LABEL: &str = ".oah/config.toml.v0.4.bak";

fn config_path(root: &Path) -> PathBuf {
    root.join(".oah").join("config.toml")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum ConfigPathPhase {
    BeforeWrite,
    AfterWrite,
}

fn refuse_config_path(
    entry: &'static str,
    reason: ConfigPathRefusal,
    phase: ConfigPathPhase,
) -> Error {
    match phase {
        ConfigPathPhase::BeforeWrite => WorkflowError::UnsafeConfigPath { entry, reason }.into(),
        ConfigPathPhase::AfterWrite => {
            WorkflowError::UnsafeConfigPathAfterSave { entry, reason }.into()
        }
    }
}

/// Checks `.oah` without following links or Windows reparse points. Returns `Ok(true)` when `.oah` exists as a
/// regular folder and `Ok(false)` when `.oah` is absent.
fn check_config_dir(root: &Path, phase: ConfigPathPhase) -> Result<bool, Error> {
    let dir = root.join(".oah");
    match inspect_project_entry(&dir, ExpectedEntry::Directory)? {
        InspectedEntry::Missing => Ok(false),
        InspectedEntry::Allowed => Ok(true),
        InspectedEntry::LinkOrReparse => Err(refuse_config_path(
            CONFIG_DIR_LABEL,
            ConfigPathRefusal::LinkOrReparse,
            phase,
        )),
        InspectedEntry::Unsupported => Err(refuse_config_path(
            CONFIG_DIR_LABEL,
            ConfigPathRefusal::NotDirectory,
            phase,
        )),
    }
}

/// Checks one project-local file entry under `.oah` without following links or Windows reparse points.
fn check_regular_config_entry(
    path: &Path,
    entry: &'static str,
    phase: ConfigPathPhase,
) -> Result<(), Error> {
    match inspect_project_entry(path, ExpectedEntry::RegularFile)? {
        InspectedEntry::Missing | InspectedEntry::Allowed => Ok(()),
        InspectedEntry::LinkOrReparse => Err(refuse_config_path(
            entry,
            ConfigPathRefusal::LinkOrReparse,
            phase,
        )),
        InspectedEntry::Unsupported => Err(refuse_config_path(
            entry,
            ConfigPathRefusal::NotRegularFile,
            phase,
        )),
    }
}

/// Checks `.oah` and only the project-local paths that a real `config::save_project` publication touches, without
/// following links or Windows reparse points.
///
/// Always checks `.oah`, `.oah/config.toml`, and `.oah/config.toml.lock` when `.oah` exists; checks
/// `.oah/config.toml.v0.4.bak` only when `check_backup` is true (an approved v0.4.0 upgrade).
///
/// Residual limit: this is a name-based metadata check before calling `config::save_project`; it refuses
/// pre-existing links, reparse entries, and unsupported file types, but cannot by itself prevent a
/// concurrent local writer from replacing `.oah` between inspection and `save_project`'s filesystem calls.
fn check_config_publication_paths(root: &Path, check_backup: bool) -> Result<(), Error> {
    if !check_config_dir(root, ConfigPathPhase::BeforeWrite)? {
        return Ok(());
    }
    let dir = root.join(".oah");
    let config = config_path(root);
    let lock = dir.join("config.toml.lock");
    check_regular_config_entry(&config, CONFIG_FILE_LABEL, ConfigPathPhase::BeforeWrite)?;
    check_regular_config_entry(&lock, CONFIG_LOCK_LABEL, ConfigPathPhase::BeforeWrite)?;
    if check_backup {
        let backup = backup_path(&config);
        check_regular_config_entry(&backup, CONFIG_BACKUP_LABEL, ConfigPathPhase::BeforeWrite)?;
    }
    Ok(())
}

fn map_config_read_error(error: FsxError, phase: ConfigPathPhase) -> Error {
    if let Some(io) = error.io_error() {
        #[cfg(unix)]
        if rustix::io::Errno::from_io_error(io) == Some(rustix::io::Errno::LOOP) {
            return refuse_config_path(CONFIG_FILE_LABEL, ConfigPathRefusal::LinkOrReparse, phase);
        }
        if io.kind() == ErrorKind::InvalidData {
            return refuse_config_path(CONFIG_FILE_LABEL, ConfigPathRefusal::NotRegularFile, phase);
        }
    }
    error.into()
}

/// Validates `.oah` and `.oah/config.toml` only (leaving unused lock/backup sidecars untouched) and reads
/// `.oah/config.toml` without following leaf links.
pub(super) fn read_config_source(
    root: &Path,
    phase: ConfigPathPhase,
) -> Result<ProjectConfig, Error> {
    let path = config_path(root);
    let dir_exists = check_config_dir(root, phase)?;
    let bytes = if dir_exists {
        check_regular_config_entry(&path, CONFIG_FILE_LABEL, phase)?;
        match fsx::read_regular_optional(&path) {
            Ok(Some((bytes, _))) => Some(bytes),
            Ok(None) => None,
            Err(error) => return Err(map_config_read_error(error, phase)),
        }
    } else {
        None
    };
    let file = ConfigFile::from_bytes(&path, bytes)?;
    Ok(ProjectConfig::from_file(file)?)
}

fn open_root(ctx: &Ctx<'_>, project_id: Option<&str>) -> Result<PathBuf, Error> {
    let open = ctx.session.project().ok_or(WorkflowError::NoProject)?;
    if project_id.is_some_and(|id| id != open.id) {
        return Err(WorkflowError::ProjectChanged.into());
    }
    Ok(open.project.root)
}

/// The agents the caller detected as installed (a read; thin files take no `InstalledAgent`, D-33).
fn installed(agents: &[AgentStatusDto]) -> Vec<AgentId> {
    agents
        .iter()
        .filter(|agent| agent.installed)
        .map(|agent| agent.id.0)
        .collect()
}

/// The always-on project skills the block lists. `skills::lock` does not exist at this base, so the list is
/// empty; the slice that lands the lock wires it here (D-14, CONTRACTS C18).
fn always_skills() -> Vec<AlwaysSkill> {
    Vec::new()
}

fn sync_input<'a>(config: &'a ProjectConfig, always: &'a [AlwaysSkill]) -> SyncInput<'a> {
    SyncInput {
        config,
        always,
        lang: None,
    }
}

fn rules_state(
    env: &Env,
    root: &Path,
    config: &ProjectConfig,
    agents: &[AgentStatusDto],
) -> RulesStateDto {
    let refused = |error: Error| RulesStateDto {
        synced: false,
        drift: Vec::new(),
        writes: Vec::new(),
        error: Some(error.to_dto(env)),
    };
    let (always, detected) = (always_skills(), installed(agents));
    let snapshot = match RulesSnapshot::read(root, &detected) {
        Ok(snapshot) => snapshot,
        Err(error) => return refused(error.into()),
    };
    match plan_sync(&snapshot, &sync_input(config, &always)) {
        Ok(plan) => {
            let drift: Vec<DriftDto> = plan
                .drift()
                .into_iter()
                .map(|line| DriftDto {
                    code: line.code.to_string(),
                    path: line.path.to_string(),
                    message: line.message,
                })
                .collect();
            RulesStateDto {
                synced: drift.is_empty(),
                drift,
                writes: plan
                    .writes()
                    .map(|file| PlannedWriteDto {
                        path: file.path.to_string(),
                        action: file.action,
                    })
                    .collect(),
                error: None,
            }
        }
        Err(error) => refused(error.into()),
    }
}

fn source(effective: &Effective, section: &str) -> String {
    match effective
        .stages
        .iter()
        .find(|assignment| assignment.section == section)
        .map(|assignment| assignment.source)
    {
        Some(AssignmentSource::File) => String::from("file"),
        Some(AssignmentSource::Legacy(key)) => format!("legacy:{key}"),
        Some(AssignmentSource::Default) => String::from("default"),
        None => String::from("unassigned"),
    }
}

fn agent_options(agents: &[AgentStatusDto]) -> Vec<AgentOptionDto> {
    let mut options: Vec<AgentOptionDto> = AgentId::ALL
        .into_iter()
        .filter(|id| id.can_own_section())
        .map(|id| {
            let found = agents.iter().find(|agent| agent.id == AgentIdDto(id));
            AgentOptionDto {
                id: AgentIdDto(id),
                name: agent_name(id).to_string(),
                installed: found.map(|agent| agent.installed),
                sign_in: found.map_or(SignInDto::Unknown, |agent| agent.sign_in),
                disabled_reason: found
                    .filter(|agent| !agent.installed)
                    .map(|_| String::from("Not installed on this machine")),
            }
        })
        .collect();
    options.sort_by_key(|option| option.installed != Some(true));
    options
}

fn bare_key(key: &str) -> String {
    if !key.is_empty()
        && key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        key.to_string()
    } else {
        format!("{key:?}")
    }
}

fn render_table(out: &mut String, name: &str, table: &Table) {
    out.push_str(&format!("[{name}]\n"));
    for (key, value) in table.iter().filter(|(_, value)| value.as_table().is_none()) {
        out.push_str(&format!("{} = {}\n", bare_key(key), value.to_toml()));
    }
    for (key, value) in table.iter() {
        if let Some(inner) = value.as_table() {
            render_table(out, &format!("{name}.{}", bare_key(key)), inner);
        }
    }
}

/// The `[sections]` and `[stages]` tables as the file holds them.
fn config_tables(values: &Table) -> String {
    let mut out = String::new();
    for name in ["sections", "stages"] {
        if let Some(table) = values.get(name).and_then(Value::as_table) {
            if !out.is_empty() {
                out.push('\n');
            }
            render_table(&mut out, name, table);
        }
    }
    out
}

fn independence_notices(effective: &Effective) -> Vec<NoticeDto> {
    let label = |id: &str| {
        effective
            .sections
            .iter()
            .find(|section| section.id == id)
            .map_or_else(|| id.to_string(), |section| section.label.clone())
    };
    independence_breaks(effective)
        .into_iter()
        .map(|(section, other, agent)| {
            NoticeDto::warning(
                codes::REVIEW_NOT_INDEPENDENT,
                &format!("{section}:{other}"),
                format!(
                    "{} is done by the same agent as {} ({}).",
                    label(&section),
                    label(&other),
                    agent_name(agent)
                ),
            )
        })
        .collect()
}

/// What the Team Workflow page and `oah sections show` show for the project at `root`; reads only.
pub fn view(env: &Env, root: &Path, agents: &[AgentStatusDto]) -> Result<WorkflowDto, Error> {
    let config = ProjectConfig::read(&config_path(root))?;
    Ok(view_from_config(env, root, &config, agents))
}

fn view_from_config(
    env: &Env,
    root: &Path,
    config: &ProjectConfig,
    agents: &[AgentStatusDto],
) -> WorkflowDto {
    let effective = config.effective();
    let sections = effective
        .sections
        .iter()
        .map(|section| SectionDto {
            id: section.id.clone(),
            label: section.label.clone(),
            agent: effective.agent_for(&section.id).map(AgentIdDto),
            source: source(effective, &section.id),
            independent_of: section.independent_of.clone(),
            skills: section.skills.clone(),
        })
        .collect();
    let block = read_block(root);
    let mut notes = Vec::new();
    if effective
        .stages
        .iter()
        .any(|assignment| assignment.agent == AgentId::Copilot)
    {
        notes.push(String::from(
            "Copilot CLI may not read AGENTS.md (not verified yet), so this assignment may not reach it.",
        ));
    }
    let mut notices: Vec<NoticeDto> = config
        .notices()
        .iter()
        .map(|notice| NoticeDto::from_config(env, notice))
        .collect();
    notices.extend(independence_notices(effective));
    WorkflowDto {
        sections,
        agents: agent_options(agents),
        banner: status(effective, agents),
        presets: PRESETS
            .iter()
            .map(|preset| preset.to_dto(effective, agents))
            .collect(),
        rules: rules_state(env, root, config, agents),
        config_format: match config.format() {
            Format::Missing => ConfigFormatDto::Missing,
            Format::Legacy => ConfigFormatDto::Legacy,
            Format::Current => ConfigFormatDto::Current,
        },
        config_sha256: config
            .file()
            .source_bytes()
            .map(|bytes| Fingerprint::of(bytes).to_hex()),
        upgrade_warning: (config.format() == Format::Legacy).then(|| UPGRADE_WARNING.to_string()),
        advanced: AdvancedDto {
            config_tables: config_tables(config.file().values()),
            block,
        },
        notes,
        notices,
    }
}

/// Plans the rules from `effective` and writes them; records every verified write in the session.
fn sync_rules(
    ctx: &Ctx<'_>,
    root: &Path,
    config: &ProjectConfig,
    agents: &[AgentStatusDto],
) -> SyncResult {
    let (always, detected) = (always_skills(), installed(agents));
    let snapshot = match RulesSnapshot::read(root, &detected) {
        Ok(snapshot) => snapshot,
        Err(error) => {
            return SyncResult {
                error: Some(error.into()),
                ..SyncResult::default()
            };
        }
    };
    let plan = match plan_sync(&snapshot, &sync_input(config, &always)) {
        Ok(plan) => plan,
        Err(error) => {
            return SyncResult {
                error: Some(error.into()),
                ..SyncResult::default()
            };
        }
    };
    let result = apply_sync(&plan, ctx.cancel, LockOptions::default());
    for receipt in &result.receipts {
        ctx.session.self_writes().record(receipt);
    }
    result
}

/// Re-reads the session's project when it is the one at `root`, so its config state follows a save.
fn refresh_session(ctx: &Ctx<'_>, root: &Path) {
    if let Some((stamp, held)) = ctx.session.stamp_refresh()
        && held == root
        && let Ok(read) = crate::project::open(ctx.env, root)
    {
        ctx.session.publish_project(stamp, read);
    }
}

/// The one assignment operation (CONTRACTS C12) for the project at `root`.
///
/// 1. Every named section must be in `sections.order` (`OAH_SECTION_UNKNOWN`, nothing written).
/// 2. `config::save_project` writes the `[stages]` keys and `independent_of` lists in one save. A v0.4.0 file
///    needs `approve_upgrade` to be the SHA-256 of exactly the bytes the warning was shown for; otherwise the save
///    is `OAH_CONFIG_UPGRADE_REQUIRED` and nothing is written. The upgrade keeps `.oah/config.toml.v0.4.bak`.
///    `cursor` is refused by `config` (`OAH_AGENT_DEPLOY_ONLY`). A refused save is returned as the error.
/// 3. The rules are planned from the saved config and written. A refusal leaves the saved config in place and
///    returns `synced: false`, the reason, and the *Sync now* action.
pub fn set_stages(
    ctx: &Ctx<'_>,
    root: &Path,
    agents: &[AgentStatusDto],
    req: StagesSetReq,
) -> Result<StagesSetDto, Error> {
    let path = config_path(root);
    let snapshot = read_config_source(root, ConfigPathPhase::BeforeWrite)?;
    let effective = snapshot.effective();
    let known = |id: &str| effective.sections.iter().any(|section| section.id == id);
    let unknown = |id: &str| -> Error {
        WorkflowError::UnknownSection {
            section: id.to_string(),
        }
        .into()
    };
    let mut edits = Vec::new();
    for change in &req.stages {
        if !known(&change.section) {
            return Err(unknown(&change.section));
        }
        edits.push(Edit::stage(&change.section, change.agent.0));
    }
    for rule in &req.independent_of {
        if !known(&rule.section) {
            return Err(unknown(&rule.section));
        }
        if let Some(bad) = rule.of.iter().find(|id| !known(id) || **id == rule.section) {
            return Err(unknown(bad));
        }
        let list = rule.of.iter().map(|id| Value::from(id.as_str())).collect();
        edits.push(Edit::set(
            &["sections", rule.section.as_str(), "independent_of"],
            Value::Array(list),
        ));
    }
    let plan = plan_project_save(&snapshot, &edits)?;
    let approval = match (
        req.approve_upgrade.as_deref(),
        snapshot.file().source_bytes(),
        snapshot.upgrade_warning(),
    ) {
        (Some(approved), Some(bytes), Some(warning))
            if Fingerprint::of(bytes).to_hex() == approved =>
        {
            Some(warning.approve())
        }
        _ => None,
    };
    if plan.bytes.is_some() && (!plan.upgrade || approval.is_some()) {
        check_config_publication_paths(root, plan.upgrade)?;
    }
    let saved = save_project(&snapshot, &edits, approval.as_ref(), LockOptions::default())?;
    if let Some(crate::config::Backup::Written(receipt)) = &saved.backup {
        ctx.session.self_writes().record(receipt);
    }
    if let Some(receipt) = &saved.written {
        ctx.session.self_writes().record(receipt);
    }
    refresh_session(ctx, root);
    let backup = saved.backup.as_ref().map(|_| {
        let name = backup_path(&path);
        format!(
            ".oah/{}",
            name.file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default()
        )
    });
    let post_save_phase = if saved.written.is_some() {
        ConfigPathPhase::AfterWrite
    } else {
        ConfigPathPhase::BeforeWrite
    };
    let after = read_config_source(root, post_save_phase)?;
    let result = sync_rules(ctx, root, &after, agents);
    let mut workflow = view_from_config(ctx.env, root, &after, agents);
    let sync_error = result.error.as_ref().map(|error| error.to_dto(ctx.env));
    if let Some(error) = &sync_error {
        workflow.notices.push(NoticeDto {
            id: codes::RULES_NOT_SYNCED.to_string(),
            level: NoticeLevel::Warning,
            code: codes::RULES_NOT_SYNCED.to_string(),
            message: format!(
                "The assignment is saved, but AGENTS.md is not synced: {}: {}",
                error.code, error.message
            ),
            hint: hint_for(codes::RULES_NOT_SYNCED).map(str::to_string),
            actions: vec![SYNC_NOW.to_string()],
        });
    }
    Ok(StagesSetDto {
        saved: saved.written.is_some(),
        upgraded: saved.upgraded,
        backup,
        synced: result.synced(),
        actions: if result.synced() {
            Vec::new()
        } else {
            vec![SYNC_NOW.to_string()]
        },
        sync_error,
        workflow,
    })
}

/// `rules_check` for the project at `root`: drift and planned writes, or the refusal a sync would hit. Writes
/// nothing.
pub fn rules_check_at(
    env: &Env,
    root: &Path,
    agents: &[AgentStatusDto],
) -> Result<RulesStateDto, Error> {
    let config = ProjectConfig::read(&config_path(root))?;
    Ok(rules_state(env, root, &config, agents))
}

/// `rules_sync` for the project at `root`. A refusal before anything was written is the error; a stop after a
/// write is reported in the result with `synced: false`.
pub fn rules_sync_at(
    ctx: &Ctx<'_>,
    root: &Path,
    agents: &[AgentStatusDto],
) -> Result<RulesSyncDto, Error> {
    let config = ProjectConfig::read(&config_path(root))?;
    let result = sync_rules(ctx, root, &config, agents);
    let wrote = result
        .items
        .iter()
        .any(|(_, _, item)| item.status != ItemStatus::NotRun && item.status != ItemStatus::Stale);
    let mut result = result;
    if !wrote && let Some(error) = result.error.take() {
        return Err(error);
    }
    let synced = result.error.is_none();
    Ok(RulesSyncDto {
        synced,
        written: result
            .items
            .iter()
            .map(|(path, action, item)| RulesWriteDto {
                path: path.to_string(),
                action: *action,
                status: item.status,
            })
            .collect(),
        error: result.error.as_ref().map(|error| error.to_dto(ctx.env)),
    })
}

/// `sections_show`: the page of the open project with the session's agent statuses.
pub fn sections_show(ctx: &Ctx<'_>, _req: Empty) -> Result<WorkflowDto, Error> {
    show(ctx, None)
}

pub fn sections_show_for_project(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq,
) -> Result<WorkflowDto, Error> {
    show(ctx, Some(&req.project_id))
}

fn show(ctx: &Ctx<'_>, project_id: Option<&str>) -> Result<WorkflowDto, Error> {
    let root = open_root(ctx, project_id)?;
    let agents = agents_list(ctx, Empty {})?;
    view(ctx.env, &root, &agents)
}

/// `workflow_status`: the one banner of the open project.
pub fn workflow_status(ctx: &Ctx<'_>, _req: Empty) -> Result<BannerDto, Error> {
    let root = open_root(ctx, None)?;
    let agents = agents_list(ctx, Empty {})?;
    let config = ProjectConfig::read(&config_path(&root))?;
    Ok(status(config.effective(), &agents))
}

/// `stages_set` on the open project.
pub fn stages_set(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq<StagesSetReq>,
) -> Result<StagesSetDto, Error> {
    let root = open_root(ctx, Some(&req.project_id))?;
    let agents = agents_list(ctx, Empty {})?;
    set_stages(ctx, &root, &agents, req.input)
}

/// `preset_apply`: the preset's assignments for the sections that exist, as one `stages_set`. A preset that does
/// not exist, or names an agent not installed here, is `OAH_PRESET_UNAVAILABLE` and writes nothing.
pub fn preset_apply(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq<PresetApplyReq>,
) -> Result<StagesSetDto, Error> {
    let root = open_root(ctx, Some(&req.project_id))?;
    let req = req.input;
    let agents = agents_list(ctx, Empty {})?;
    let config = read_config_source(&root, ConfigPathPhase::BeforeWrite)?;
    let effective = config.effective();
    let unavailable = |reason: String| -> Error {
        WorkflowError::PresetUnavailable {
            preset: req.preset.clone(),
            reason,
        }
        .into()
    };
    let preset = PRESETS
        .iter()
        .find(|preset| preset.id == req.preset)
        .ok_or_else(|| unavailable(String::from("there is no such preset")))?;
    if let Some(reason) = preset.disabled_reason(effective, &agents) {
        return Err(unavailable(reason));
    }
    let stages = preset
        .stages_for(effective)
        .into_iter()
        .map(|(section, agent)| StageChangeReq {
            section,
            agent: AgentIdDto(agent),
        })
        .collect();
    set_stages(
        ctx,
        &root,
        &agents,
        StagesSetReq {
            stages,
            independent_of: Vec::new(),
            approve_upgrade: req.approve_upgrade,
        },
    )
}

/// `rules_check` on the open project.
pub fn rules_check(ctx: &Ctx<'_>, _req: Empty) -> Result<RulesStateDto, Error> {
    let root = open_root(ctx, None)?;
    let agents = agents_list(ctx, Empty {})?;
    rules_check_at(ctx.env, &root, &agents)
}

/// `rules_sync` on the open project.
pub fn rules_sync(ctx: &Ctx<'_>, _req: Empty) -> Result<RulesSyncDto, Error> {
    sync_open_project(ctx, None)
}

pub fn rules_sync_for_project(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq,
) -> Result<RulesSyncDto, Error> {
    sync_open_project(ctx, Some(&req.project_id))
}

fn sync_open_project(ctx: &Ctx<'_>, project_id: Option<&str>) -> Result<RulesSyncDto, Error> {
    let root = open_root(ctx, project_id)?;
    let agents = agents_list(ctx, Empty {})?;
    rules_sync_at(ctx, &root, &agents)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::Coded;
    use std::fs;

    #[test]
    fn read_config_source_ignores_auxiliary_sidecars_and_distinguishes_post_save_diagnostics() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        fs::create_dir_all(root.join(".oah")).unwrap();
        fs::write(
            root.join(".oah/config.toml"),
            b"[stages]\nplan = \"claude\"\n",
        )
        .unwrap();
        // Unused lock and backup sidecars are directories; source reads must ignore both.
        fs::create_dir_all(root.join(".oah/config.toml.lock")).unwrap();
        fs::create_dir_all(root.join(".oah/config.toml.v0.4.bak")).unwrap();

        let before = read_config_source(root, ConfigPathPhase::BeforeWrite).unwrap();
        let after = read_config_source(root, ConfigPathPhase::AfterWrite).unwrap();
        assert_eq!(before.format(), Format::Current);
        assert_eq!(after.format(), Format::Current);

        // Publication check without backup checks `.oah/config.toml.lock` and ignores `.v0.4.bak`.
        let lock_err = check_config_publication_paths(root, false).unwrap_err();
        assert!(matches!(
            lock_err,
            Error::Workflow(WorkflowError::UnsafeConfigPath {
                entry: ".oah/config.toml.lock",
                reason: ConfigPathRefusal::NotRegularFile,
            })
        ));
        fs::remove_dir(root.join(".oah/config.toml.lock")).unwrap();
        check_config_publication_paths(root, false).unwrap();

        // Publication check with backup checks `.oah/config.toml.v0.4.bak`.
        let backup_err = check_config_publication_paths(root, true).unwrap_err();
        assert!(matches!(
            backup_err,
            Error::Workflow(WorkflowError::UnsafeConfigPath {
                entry: ".oah/config.toml.v0.4.bak",
                reason: ConfigPathRefusal::NotRegularFile,
            })
        ));

        // Post-save reread on an unsafe `.oah/config.toml` reports committed state without claiming nothing was written.
        fs::remove_file(root.join(".oah/config.toml")).unwrap();
        fs::create_dir_all(root.join(".oah/config.toml")).unwrap();
        let post_err = read_config_source(root, ConfigPathPhase::AfterWrite).unwrap_err();
        assert_eq!(post_err.code(), "OAH_FILE_IO");
        assert!(matches!(
            post_err,
            Error::Workflow(WorkflowError::UnsafeConfigPathAfterSave {
                entry: ".oah/config.toml",
                reason: ConfigPathRefusal::NotRegularFile,
            })
        ));
        assert!(post_err.message().contains("the assignment was saved"));
        assert!(!post_err.message().contains("nothing was written"));
        assert!(!post_err.message().contains(&root.display().to_string()));

        // Post-save reread on an unsafe `.oah` directory likewise reports committed state.
        let file_oah = tempfile::tempdir().unwrap();
        fs::write(file_oah.path().join(".oah"), b"not a dir\n").unwrap();
        let dir_err = read_config_source(file_oah.path(), ConfigPathPhase::AfterWrite).unwrap_err();
        assert_eq!(dir_err.code(), "OAH_FILE_IO");
        assert!(matches!(
            dir_err,
            Error::Workflow(WorkflowError::UnsafeConfigPathAfterSave {
                entry: ".oah",
                reason: ConfigPathRefusal::NotDirectory,
            })
        ));
        assert!(dir_err.message().contains("the assignment was saved"));
        assert!(!dir_err.message().contains("nothing was written"));
    }

    #[cfg(unix)]
    #[test]
    fn post_save_reread_on_linked_source_omits_stored_target_and_does_not_claim_unwritten() {
        use std::os::unix::fs::symlink;

        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        fs::create_dir_all(root.join(".oah")).unwrap();
        let inert = root.join("inert-config-target.toml");
        fs::write(&inert, b"[stages]\nplan = \"claude\"\n").unwrap();
        symlink(&inert, root.join(".oah/config.toml")).unwrap();

        let err = read_config_source(root, ConfigPathPhase::AfterWrite).unwrap_err();
        assert_eq!(err.code(), "OAH_FILE_IO");
        assert!(matches!(
            err,
            Error::Workflow(WorkflowError::UnsafeConfigPathAfterSave {
                entry: ".oah/config.toml",
                reason: ConfigPathRefusal::LinkOrReparse,
            })
        ));
        assert!(err.message().contains("the assignment was saved"));
        assert!(!err.message().contains("nothing was written"));
        assert!(!err.message().contains("inert-config-target.toml"));
        assert!(!err.message().contains(&root.display().to_string()));
    }
}
