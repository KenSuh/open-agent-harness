// 프로젝트 위키 작업: 위키 카드의 개요·미리보기·적용·검색을 한 인터페이스로 내고, 프로젝트 고정과 설정 저장 계획에 함께 묶는다.
//! The Project Wiki actions that the desktop card and a command line share.
//!
//! [`overview_at`], [`plan_at`], [`apply_at`] and [`search_at`] work on a [`WikiScope`]: the id of a project, its
//! root and the native selection of that root ([`ProjectRoot`]). They take no path from the caller except a folder
//! name that must be the one the settings name or one [`wiki::discover`] found. The `project_wiki_*` functions are
//! the same operations for the session's open project: they check the caller's project id and use the selection
//! made when the project was opened.
//!
//! One reviewed plan covers the files and the settings. [`plan_at`] reads only. Its id names the project, the
//! request, the wiki plan (which binds the folders by identity and every new file's bytes), the settings plan
//! (which binds the settings file's bytes now and after the save) and the settings upgrade to approve.
//! [`apply_at`] makes both plans again and writes nothing unless they give that id, nothing is blocked, and an
//! upgrade is approved for exactly the bytes shown. Then the files are created, none replaced. The settings are
//! saved through [`apply_tools_save`] only when every file is in place and verified; otherwise the result says how
//! far it came. A second run is safe: files that are there are listed as kept, and the settings change is planned
//! again from the file as it is then.
//!
//! Nothing here starts a process, uses the network, or turns a key or an optional feature on.

use super::wiki::{
    self, ApplyItem, CopyAction, FileAction, ItemStatus, SetupMode, WikiCopyPlan, WikiCopyRequest,
    WikiError, WikiSearchRequest, WikiSetupPlan, WikiSetupRequest,
};
use crate::app::Ctx;
use crate::config::{
    Backup, BackupPlan, ConfigError, ToolsApproval, ToolsBinding, ToolsChange, ToolsPlan, Value,
    apply_tools_save, codes as config_codes, plan_tools_save,
};
use crate::dto::{
    Empty, ProjectWikiDto, WikiActionKind, WikiActionReq, WikiApplyDto, WikiApplyReq,
    WikiBackupDto, WikiBackupState, WikiConfiguredDto, WikiCountsDto, WikiFileAction, WikiPlanDto,
    WikiPlanFileDto, WikiPlanMode, WikiProblemDto, WikiSearchDto, WikiSearchReq,
    WikiSettingChangeDto, WikiSettingKeptDto, WikiSettingValue, WikiSettingsPlanDto,
    WikiSettingsResultDto, WikiSettingsState, WikiTargetDto, WikiTargetState, WikiUpgradeDto,
    WorkflowProjectReq, rfc3339_utc,
};
use crate::error::{Error, ProjectSkillError};
use crate::fsx::skill_publish::{ProjectRoot, PublishError};
use crate::fsx::{Fingerprint, LockOptions, WriteReceipt};
use crate::paths::Env;
use crate::tasks::{CancelToken, Progress, ProgressSink, TaskKey};
use crate::workflow::WorkflowError;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// The task kind of a wiki apply.
pub const WIKI_APPLY: &str = "project-wiki-apply";
/// The settings file, as every answer names it.
const CONFIG_LABEL: &str = ".oah/config.toml";
const PLAN_VERSION: &[u8] = b"oah-project-wiki-plan-v1";

/// One wiki apply per project at a time: a second start while one runs is `OAH_TASK_BUSY`.
pub fn wiki_apply_key(project_id: &str) -> TaskKey {
    TaskKey::exclusive(WIKI_APPLY, project_id)
}

/// The project the wiki actions work on.
#[derive(Debug, Clone)]
pub struct WikiScope {
    project_id: String,
    root: PathBuf,
    pin: ProjectRoot,
}

impl WikiScope {
    /// `project_id` is `paths::folder_id` of the canonical `root`, and `pin` the selection of that same folder
    /// (`ProjectRoot::open`). The settings writer checks the id against the root again; the files go through the
    /// pin, which holds the folder by identity.
    pub fn new(project_id: impl Into<String>, root: impl Into<PathBuf>, pin: ProjectRoot) -> Self {
        WikiScope {
            project_id: project_id.into(),
            root: root.into(),
            pin,
        }
    }

    pub fn project_id(&self) -> &str {
        &self.project_id
    }

    fn binding(&self) -> ToolsBinding {
        ToolsBinding::new(self.project_id.clone(), self.root.clone())
    }
}

/// Today's date in UTC, as the new log's first entry is dated.
fn today_utc() -> String {
    rfc3339_utc(SystemTime::now()).chars().take(10).collect()
}

/// `path` relative to `root`, written with `/`.
fn relative(root: &Path, path: &Path) -> String {
    match path.strip_prefix(root) {
        Ok(inside) => inside
            .components()
            .map(|part| part.as_os_str().to_string_lossy())
            .collect::<Vec<_>>()
            .join("/"),
        Err(_) => path
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default(),
    }
}

/// `[wiki] dir` when the file sets it. A default is not passed on, so the wiki rules decide the folder.
fn configured_dir(tools: &ToolsPlan) -> Option<String> {
    let dir = &tools.tools().wiki.dir;
    dir.explicit.then(|| dir.value.clone())
}

/// `dir` must be a folder [`wiki::discover`] lists, spelled as listed. Returns how many folders it lists.
fn discovered(scope: &WikiScope, dir: &str) -> Result<usize, WikiError> {
    let found = wiki::discover(&scope.pin)?;
    if found
        .candidates
        .iter()
        .any(|candidate| candidate.dir == dir)
    {
        Ok(found.candidates.len())
    } else {
        Err(WikiError::DirInvalid {
            dir: dir.chars().filter(|c| !c.is_control()).take(120).collect(),
            reason: "it is not one of the wiki folders found in this project; check again and choose one"
                .to_string(),
        })
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------------------------------------------

/// The folders that look like a wiki, the folder the settings name and what a setup without a choice would do.
/// Reads the settings file and bounded folder listings; never fails: what cannot be read is said in the answer.
pub fn overview_at(env: &Env, scope: &WikiScope) -> ProjectWikiDto {
    let shown = |error: Error| error.to_dto(env);
    let settings = plan_tools_save(&scope.binding(), &[]);
    let (configured, settings_error) = match &settings {
        Ok(plan) => {
            let dir = &plan.tools().wiki.dir;
            (
                Some(WikiConfiguredDto {
                    dir: dir.value.clone(),
                    explicit: dir.explicit,
                }),
                None,
            )
        }
        Err(error) => (None, Some(shown(error.clone().into()))),
    };
    let unavailable = |error| WikiTargetDto {
        state: WikiTargetState::Unavailable,
        dir: None,
        creates: 0,
        sets_dir: false,
        error,
    };
    let found = wiki::discover(&scope.pin);
    let target = match (&settings, &found) {
        (Err(_), _) => unavailable(settings_error.clone()),
        (_, Err(error)) => unavailable(Some(shown(error.clone().into()))),
        (Ok(tools), Ok(_)) => {
            let request = WikiSetupRequest {
                configured: configured_dir(tools),
                selected: None,
                today: today_utc(),
            };
            match wiki::plan_setup(&scope.pin, &request) {
                Ok(plan) => {
                    let count = |action| plan.files.iter().filter(|f| f.action == action).count();
                    let creates = count(FileAction::Create);
                    let sets_dir = plan.config_dir.is_some();
                    let settled = creates == 0 && count(FileAction::Blocked) == 0 && !sets_dir;
                    WikiTargetDto {
                        state: match plan.mode {
                            SetupMode::Create => WikiTargetState::None,
                            SetupMode::Adopt if settled => WikiTargetState::Ready,
                            SetupMode::Adopt => WikiTargetState::Incomplete,
                        },
                        dir: Some(plan.dir),
                        creates,
                        sets_dir,
                        error: None,
                    }
                }
                Err(WikiError::Ambiguous { .. }) => WikiTargetDto {
                    state: WikiTargetState::Choose,
                    ..unavailable(None)
                },
                Err(error) => {
                    let (state, dir) = match &error {
                        WikiError::Missing { dir } => {
                            (WikiTargetState::ConfiguredMissing, Some(dir.clone()))
                        }
                        _ => (WikiTargetState::Unavailable, None),
                    };
                    WikiTargetDto {
                        state,
                        dir,
                        ..unavailable(Some(shown(error.into())))
                    }
                }
            }
        }
    };
    let (candidates, problems, truncated) = match found {
        Ok(found) => (
            found.candidates,
            found
                .problems
                .into_iter()
                .map(|problem| WikiProblemDto {
                    dir: problem.dir,
                    error: shown(problem.error.into()),
                })
                .collect(),
            found.truncated,
        ),
        Err(_) => (Vec::new(), Vec::new(), false),
    };
    ProjectWikiDto {
        project_id: scope.project_id.clone(),
        default_dir: wiki::DEFAULT_DIR.to_string(),
        configured,
        settings_error,
        candidates,
        problems,
        truncated,
        target,
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Plan
// ---------------------------------------------------------------------------------------------------------------

/// The wiki half of a reviewed plan, with the request that gives it.
enum Reviewed {
    Setup {
        request: WikiSetupRequest,
        plan: WikiSetupPlan,
    },
    Copy {
        request: WikiCopyRequest,
        plan: WikiCopyPlan,
    },
}

/// Both halves of a plan as the disk gives them now.
struct Survey {
    reviewed: Reviewed,
    /// The settings plan for `changes`; it is also the read of the settings the wiki half was made from.
    tools: ToolsPlan,
    changes: Vec<ToolsChange>,
    id: String,
}

fn plan_id(scope: &WikiScope, request: &WikiActionReq, wiki_id: &str, tools: &ToolsPlan) -> String {
    // The request as it was sent: an approval covers this action, folder and date and no other.
    let request = serde_json::to_vec(request).unwrap_or_default();
    let upgrade = tools
        .upgrade_warning()
        .and(tools.before())
        .map(Fingerprint::to_hex);
    let parts: [&[u8]; 6] = [
        PLAN_VERSION,
        scope.project_id.as_bytes(),
        &request,
        wiki_id.as_bytes(),
        tools.id().as_bytes(),
        upgrade.as_deref().unwrap_or("none").as_bytes(),
    ];
    let mut buffer = Vec::new();
    for part in parts {
        buffer.extend_from_slice(&(part.len() as u64).to_le_bytes());
        buffer.extend_from_slice(part);
    }
    format!("sha256:{}", Fingerprint::of(&buffer).to_hex())
}

fn request_of(source: &str) -> WikiActionReq {
    WikiActionReq::Copy {
        source: source.to_string(),
    }
}

fn survey(scope: &WikiScope, request: &WikiActionReq) -> Result<Survey, Error> {
    let binding = scope.binding();
    let current = plan_tools_save(&binding, &[])?;
    // The request with the date filled in, as the apply sends it back.
    let (reviewed, changes, effective, wiki_id) = match request {
        WikiActionReq::Setup { selected, today } => {
            let listed = match selected {
                Some(selected) => discovered(scope, selected)?,
                None => 0,
            };
            let request = WikiSetupRequest {
                configured: configured_dir(&current),
                selected: selected.clone(),
                today: today.clone().unwrap_or_else(today_utc),
            };
            let plan = wiki::plan_setup(&scope.pin, &request)?;
            let changes = if listed > 1 && request.configured.is_none() {
                vec![ToolsChange::ExplicitWikiDir(plan.dir.clone())]
            } else {
                plan.config_dir
                    .iter()
                    .cloned()
                    .map(ToolsChange::WikiDir)
                    .collect()
            };
            let effective = WikiActionReq::Setup {
                selected: selected.clone(),
                today: Some(request.today.clone()),
            };
            let id = plan.id.clone();
            (Reviewed::Setup { request, plan }, changes, effective, id)
        }
        WikiActionReq::Copy { source } => {
            discovered(scope, source)?;
            let request = WikiCopyRequest {
                source: source.clone(),
            };
            let plan = wiki::plan_copy(&scope.pin, &request)?;
            // A source without a file leaves no wiki at the destination to switch to.
            let changes = if plan.files.is_empty() {
                Vec::new()
            } else {
                vec![ToolsChange::ExplicitWikiDir(plan.destination.clone())]
            };
            let id = plan.id.clone();
            (
                Reviewed::Copy { request, plan },
                changes,
                request_of(source),
                id,
            )
        }
    };
    let tools = if changes.is_empty() {
        current
    } else {
        let planned = plan_tools_save(&binding, &changes)?;
        // The folder was chosen from the settings read first; both reads must be of the same bytes.
        if planned.before() != current.before() {
            return Err(WikiError::Stale.into());
        }
        planned
    };
    let id = plan_id(scope, &effective, &wiki_id, &tools);
    Ok(Survey {
        reviewed,
        tools,
        changes,
        id,
    })
}

fn setting_value(value: &Value) -> WikiSettingValue {
    match value {
        Value::String(text) => WikiSettingValue::Text(text.clone()),
        Value::Boolean(on) => WikiSettingValue::Switch(*on),
        Value::Array(items) if items.iter().all(|item| item.as_str().is_some()) => {
            WikiSettingValue::List(
                items
                    .iter()
                    .filter_map(|item| item.as_str().map(str::to_string))
                    .collect(),
            )
        }
        other => WikiSettingValue::Text(other.to_toml()),
    }
}

fn settings_plan(tools: &ToolsPlan) -> Option<WikiSettingsPlanDto> {
    tools.after_bytes()?;
    Some(WikiSettingsPlanDto {
        config_path: CONFIG_LABEL.to_string(),
        changes: tools
            .changes()
            .iter()
            .map(|change| WikiSettingChangeDto {
                key: change.key.clone(),
                before: change.before.as_ref().map(setting_value),
                after: change.after.as_ref().map(setting_value),
            })
            .collect(),
        kept: tools
            .kept()
            .iter()
            .map(|kept| WikiSettingKeptDto {
                key: kept.key.to_string(),
                value: setting_value(&kept.value),
                reason: kept.reason.as_str().to_string(),
            })
            .collect(),
        upgrade: tools
            .upgrade_warning()
            .zip(tools.before())
            .map(|(warning, fingerprint)| WikiUpgradeDto {
                message: warning.message().to_string(),
                fingerprint: fingerprint.to_hex(),
            }),
        backup: tools.backup().map(|backup| WikiBackupDto {
            state: match backup {
                BackupPlan::Write(_) => WikiBackupState::Write,
                BackupPlan::Reuse(_) => WikiBackupState::Reuse,
                BackupPlan::Conflict(_) => WikiBackupState::Conflict,
            },
            path: relative(tools.root(), backup.path()),
        }),
    })
}

fn plan_dto(scope: &WikiScope, survey: &Survey) -> WikiPlanDto {
    let settings = settings_plan(&survey.tools);
    let project_id = scope.project_id.clone();
    let plan_id = survey.id.clone();
    match &survey.reviewed {
        Reviewed::Setup { request, plan } => WikiPlanDto {
            project_id,
            plan_id,
            action: WikiActionKind::Setup,
            dir: plan.dir.clone(),
            source: None,
            mode: match plan.mode {
                SetupMode::Create => WikiPlanMode::Create,
                SetupMode::Adopt => WikiPlanMode::Adopt,
            },
            today: Some(request.today.clone()),
            folders: plan.folders.clone(),
            files: plan
                .files
                .iter()
                .map(|file| WikiPlanFileDto {
                    path: file.path.clone(),
                    action: match file.action {
                        FileAction::Create => WikiFileAction::Create,
                        FileAction::Keep => WikiFileAction::Keep,
                        FileAction::Blocked => WikiFileAction::Blocked,
                    },
                    bytes: None,
                })
                .collect(),
            writes: plan
                .files
                .iter()
                .filter(|file| file.action == FileAction::Create)
                .count(),
            blocked: Vec::new(),
            blocked_total: 0,
            left: Vec::new(),
            unresolved_links: Vec::new(),
            unresolved_total: 0,
            settings,
        },
        Reviewed::Copy { plan, .. } => WikiPlanDto {
            project_id,
            plan_id,
            action: WikiActionKind::Copy,
            dir: plan.destination.clone(),
            source: Some(plan.source.clone()),
            mode: WikiPlanMode::Copy,
            today: None,
            folders: plan.folders.clone(),
            files: plan
                .files
                .iter()
                .map(|file| WikiPlanFileDto {
                    path: file.path.clone(),
                    action: match file.action {
                        CopyAction::Copy => WikiFileAction::Copy,
                        CopyAction::Same => WikiFileAction::Same,
                    },
                    bytes: Some(file.bytes),
                })
                .collect(),
            writes: plan.copies(),
            blocked: plan.blocked.clone(),
            blocked_total: plan.blocked_total,
            left: plan.left.clone(),
            unresolved_links: plan.unresolved_links.clone(),
            unresolved_total: plan.unresolved_total,
            settings,
        },
    }
}

/// What `request` would do: the folder, every file created, copied or kept, and the settings change that follows.
/// Reads only; nothing is created, locked or written.
pub fn plan_at(scope: &WikiScope, request: &WikiActionReq) -> Result<WikiPlanDto, Error> {
    survey(scope, request).map(|survey| plan_dto(scope, &survey))
}

// ---------------------------------------------------------------------------------------------------------------
// Apply
// ---------------------------------------------------------------------------------------------------------------

/// What an apply did, with the receipts of the settings files it wrote for the caller's self-write record.
#[derive(Debug, Clone)]
pub struct WikiApplied {
    pub dto: WikiApplyDto,
    pub receipts: Vec<WriteReceipt>,
}

/// The approval the settings save needs, or the refusal that must come before any file is written.
fn approval(
    tools: &ToolsPlan,
    approve_upgrade: Option<&str>,
) -> Result<ToolsApproval, ConfigError> {
    if tools.after_bytes().is_none() || tools.upgrade_warning().is_none() {
        return Ok(tools.approve(false));
    }
    let approved = approve_upgrade.and_then(Fingerprint::from_hex);
    if approved.is_none() || approved != tools.before() {
        return Err(ConfigError::new(
            config_codes::UPGRADE_REQUIRED,
            format!(
                "{CONFIG_LABEL}: saving the wiki folder upgrades this v0.4.0 file to the 1.0 format; read the \
                 upgrade warning and apply again with its approval; nothing was written"
            ),
        ));
    }
    if let Some(BackupPlan::Conflict(path)) = tools.backup() {
        return Err(ConfigError::new(
            config_codes::BACKUP_CONFLICT,
            format!(
                "{} already exists and is not the file being upgraded; move it away or delete it, then preview \
                 again; nothing was written",
                relative(tools.root(), path)
            ),
        ));
    }
    Ok(ToolsApproval::new(tools.id(), approved))
}

fn counts(items: &[ApplyItem]) -> WikiCountsDto {
    let mut counts = WikiCountsDto::default();
    for item in items {
        match item.status {
            ItemStatus::Created => counts.created += 1,
            ItemStatus::Kept => counts.kept += 1,
            ItemStatus::Failed => counts.failed += 1,
            ItemStatus::Pending => counts.pending += 1,
        }
    }
    counts
}

fn step(progress: &dyn ProgressSink, phase: &'static str, done: usize, total: usize) {
    progress.progress(Progress {
        phase,
        done: u32::try_from(done).unwrap_or(u32::MAX),
        total: Some(u32::try_from(total).unwrap_or(u32::MAX)),
    });
}

/// Applies the plan the caller reviewed, files first and the settings last.
///
/// Before anything is written: both plans are made again and must give `request.plan_id` (`OAH_PLAN_STALE`),
/// nothing may be blocked (`OAH_WIKI_PLAN_BLOCKED`), and a settings upgrade must be approved for the bytes shown
/// with a usable backup path (`OAH_CONFIG_UPGRADE_REQUIRED`, `OAH_CONFIG_BACKUP_CONFLICT`). These are `Err`, as
/// is a cancellation asked for until then.
///
/// After that the answer is `Ok` and says what happened. A stop asked for through `cancel` takes effect before
/// the next file: files already written stay, the rest is `pending`, `stop` is `OAH_WIKI_STOPPED` and the
/// settings are `skipped`. The settings are saved only when every file is created or kept and verified. When the
/// settings file changed after the preview they are `stale`: the files stay, and a new preview lists them as kept
/// with the settings change still to make.
pub fn apply_at(
    env: &Env,
    scope: &WikiScope,
    request: &WikiApplyReq,
    cancel: &CancelToken,
    progress: &dyn ProgressSink,
) -> Result<WikiApplied, Error> {
    apply_guarded(env, scope, request, cancel, progress, &|| Ok(()))
}

fn apply_guarded(
    env: &Env,
    scope: &WikiScope,
    request: &WikiApplyReq,
    cancel: &CancelToken,
    progress: &dyn ProgressSink,
    before_write: &dyn Fn() -> Result<(), Error>,
) -> Result<WikiApplied, Error> {
    cancel.check()?;
    step(progress, "verify", 0, 1);
    let survey = survey(scope, &request.request)?;
    if survey.id != request.plan_id {
        return Err(WikiError::Stale.into());
    }
    let blocked: Vec<String> = match &survey.reviewed {
        Reviewed::Setup { plan, .. } => plan
            .files
            .iter()
            .filter(|file| file.action == FileAction::Blocked)
            .map(|file| format!("{}/{}", plan.dir, file.path))
            .collect(),
        Reviewed::Copy { plan, .. } => plan
            .blocked
            .iter()
            .map(|blocked| format!("{}/{}", plan.source, blocked.path))
            .collect(),
    };
    let blocks = match &survey.reviewed {
        Reviewed::Setup { .. } => blocked.len(),
        Reviewed::Copy { plan, .. } => plan.blocked_total,
    };
    if blocks > 0 {
        return Err(WikiError::Blocked { paths: blocked }.into());
    }
    let approval = approval(&survey.tools, request.approve_upgrade.as_deref())?;
    before_write()?;
    cancel.check()?;
    step(progress, "verify", 1, 1);

    let mut watch = |done: usize, total: usize| {
        step(progress, "write-files", done, total);
        !cancel.is_cancelled()
    };
    let (action, dir, source, folders_created, items, stop) = match &survey.reviewed {
        Reviewed::Setup { request, plan } => {
            let report = wiki::apply_setup_watched(&scope.pin, request, &plan.id, &mut watch)?;
            (
                WikiActionKind::Setup,
                report.dir,
                None,
                report.folders_created,
                report.items,
                report.stop,
            )
        }
        Reviewed::Copy { request, plan } => {
            let report = wiki::apply_copy_watched(&scope.pin, request, &plan.id, &mut watch)?;
            (
                WikiActionKind::Copy,
                report.destination,
                Some(report.source),
                report.folders_created,
                report.items,
                report.stop,
            )
        }
    };
    let counts = counts(&items);
    let files_done = stop.is_none() && counts.failed == 0 && counts.pending == 0;
    if files_done {
        step(progress, "write-files", counts.created, counts.created);
    }

    let mut receipts = Vec::new();
    let mut settings = WikiSettingsResultDto {
        state: WikiSettingsState::Unchanged,
        config_path: CONFIG_LABEL.to_string(),
        upgraded: false,
        backup: None,
        error: None,
    };
    if survey.tools.after_bytes().is_some() {
        if files_done {
            step(progress, "save-settings", 0, 1);
            match apply_tools_save(
                &scope.binding(),
                &survey.changes,
                &approval,
                LockOptions::default(),
            ) {
                Ok(saved) => {
                    settings.upgraded = saved.upgraded;
                    settings.backup = saved.backup.as_ref().map(|backup| {
                        relative(
                            survey.tools.root(),
                            match backup {
                                Backup::Written(receipt) => receipt.path(),
                                Backup::Reused(path) => path,
                            },
                        )
                    });
                    if let Some(Backup::Written(receipt)) = saved.backup {
                        receipts.push(receipt);
                    }
                    if let Some(receipt) = saved.written {
                        settings.state = WikiSettingsState::Saved;
                        receipts.push(receipt);
                    }
                }
                Err(error) => {
                    settings.state = match error.code() {
                        config_codes::PLAN_STALE
                        | config_codes::CHANGED
                        | config_codes::UPGRADE_REQUIRED => WikiSettingsState::Stale,
                        _ => WikiSettingsState::Failed,
                    };
                    settings.error = Some(Error::from(error).to_dto(env));
                }
            }
            step(progress, "save-settings", 1, 1);
        } else {
            settings.state = WikiSettingsState::Skipped;
        }
    }
    let complete = files_done
        && matches!(
            settings.state,
            WikiSettingsState::Unchanged | WikiSettingsState::Saved
        );
    Ok(WikiApplied {
        dto: WikiApplyDto {
            project_id: scope.project_id.clone(),
            plan_id: survey.id,
            action,
            dir,
            source,
            folders_created,
            items,
            counts,
            stop: stop.map(|error| Error::from(error).to_dto(env)),
            settings,
            complete,
        },
        receipts,
    })
}

// ---------------------------------------------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------------------------------------------

/// Searches the Markdown text of the wiki folder: the folder in use, or the named one when it is the folder the
/// settings name or one that was found. Reads only, within the bounds of [`wiki::search`].
pub fn search_at(scope: &WikiScope, request: &WikiSearchReq) -> Result<WikiSearchDto, Error> {
    let configured = || -> Result<Option<String>, Error> {
        Ok(configured_dir(&plan_tools_save(&scope.binding(), &[])?))
    };
    let dir = match &request.dir {
        Some(dir) => {
            // An unreadable settings file does not stop a search in a folder that was found.
            if configured().ok().flatten().as_deref() != Some(dir.as_str()) {
                discovered(scope, dir)?;
            }
            dir.clone()
        }
        None => {
            let plan = wiki::plan_setup(
                &scope.pin,
                &WikiSetupRequest {
                    configured: configured()?,
                    selected: None,
                    today: today_utc(),
                },
            )?;
            if plan.mode == SetupMode::Create {
                return Err(WikiError::Missing { dir: plan.dir }.into());
            }
            plan.dir
        }
    };
    let found = wiki::search(
        &scope.pin,
        &WikiSearchRequest {
            dir,
            query: request.query.clone(),
            max_hits: request.max_hits,
        },
    )?;
    Ok(WikiSearchDto {
        project_id: scope.project_id.clone(),
        dir: found.dir,
        hits: found.hits,
        truncated: found.truncated,
        files_searched: found.files_searched,
        skipped: found.skipped,
        skipped_total: found.skipped_total,
    })
}

// ---------------------------------------------------------------------------------------------------------------
// The session's open project
// ---------------------------------------------------------------------------------------------------------------

/// Why the selection made at the open cannot be used for the wiki.
fn pin_error(error: &ProjectSkillError) -> Error {
    let root = || String::from(".");
    match error {
        ProjectSkillError::NoProject => WorkflowError::NoProject.into(),
        ProjectSkillError::ProjectChanged => WorkflowError::ProjectChanged.into(),
        ProjectSkillError::HomeScope | ProjectSkillError::HomeUnverified => WikiError::Home.into(),
        ProjectSkillError::Publish(error) => match error.as_ref() {
            PublishError::Unsupported => WikiError::Unsupported { path: root() },
            PublishError::UnsafePath | PublishError::Changed => WikiError::Unsafe { path: root() },
            PublishError::Io(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                WikiError::Denied { path: root() }
            }
            other => WikiError::Io {
                path: root(),
                detail: other.code().to_string(),
            },
        }
        .into(),
    }
}

/// The open project as a scope, with the number of its selection. `OAH_PROJECT_NOT_OPEN` without one and
/// `OAH_PROJECT_CHANGED` when `project_id` is not its id or the folder is no longer the one that was opened.
fn open_scope(ctx: &Ctx<'_>, project_id: &str) -> Result<(u64, WikiScope), Error> {
    let held = ctx.session.project_pin().ok_or(WorkflowError::NoProject)?;
    if held.project_id != project_id {
        return Err(WorkflowError::ProjectChanged.into());
    }
    let pin = held.pin.as_ref().map_err(pin_error)?.clone();
    Ok((
        held.selection,
        WikiScope::new(held.project_id, held.root, pin),
    ))
}

/// `project_wiki_overview`: [`overview_at`] for the open project.
pub fn project_wiki_overview(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq<Empty>,
) -> Result<ProjectWikiDto, Error> {
    let (_, scope) = open_scope(ctx, &req.project_id)?;
    Ok(overview_at(ctx.env, &scope))
}

/// `project_wiki_plan`: [`plan_at`] for the open project.
pub fn project_wiki_plan(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq<WikiActionReq>,
) -> Result<WikiPlanDto, Error> {
    let (_, scope) = open_scope(ctx, &req.project_id)?;
    plan_at(&scope, &req.input)
}

/// The work of `start_project_wiki_apply`: [`apply_at`] for the open project, with the task's progress and
/// cancellation. The project must still be the selected one right before the first write. A saved settings file is
/// recorded as OAH's own write and the session's project is read again.
pub fn project_wiki_apply(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq<WikiApplyReq>,
) -> Result<WikiApplyDto, Error> {
    let (selection, scope) = open_scope(ctx, &req.project_id)?;
    let applied = apply_guarded(
        ctx.env,
        &scope,
        &req.input,
        ctx.cancel,
        ctx.progress,
        &|| {
            if ctx.session.holds_selection(selection, &req.project_id) {
                Ok(())
            } else {
                Err(WorkflowError::ProjectChanged.into())
            }
        },
    )?;
    for receipt in &applied.receipts {
        ctx.session.self_writes().record(receipt);
    }
    if !applied.receipts.is_empty()
        && let Some((stamp, held)) = ctx.session.stamp_refresh()
        && held == scope.root
        && let Ok(read) = crate::project::open(ctx.env, &scope.root)
    {
        ctx.session.publish_project(stamp, read);
    }
    Ok(applied.dto)
}

/// `project_wiki_search`: [`search_at`] for the open project.
pub fn project_wiki_search(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq<WikiSearchReq>,
) -> Result<WikiSearchDto, Error> {
    let (_, scope) = open_scope(ctx, &req.project_id)?;
    search_at(&scope, &req.input)
}
