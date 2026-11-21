// 규칙 쓰기: 블록·CLAUDE.md·얇은 파일 계획을 프로젝트 안 고정 경로에만 계획 엔진으로 원자적으로 쓰고 다시 읽어 확인한다.
//! The rules writer (design §3.4 "When files are written", CONTRACTS C12). [`RulesSnapshot::read`] reads the
//! eligible files; [`plan_sync`] renders the 1.0 body from the config snapshot and plans
//! the block, `CLAUDE.md` and the thin files (pure, nothing written); [`check_sync`] turns a plan into drift lines
//! without writing; [`apply_sync`] uses C4 to check the source and target bases first and then writes one target
//! at a time under its own lock, each write atomic and then read back. A file
//! that does not read back as planned is `OAH_RULES_VERIFY`.
//!
//! Every target is a fixed project-relative path under the project root: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` and
//! `.cursor/rules/oah.mdc`. A missing folder on the way to `.cursor/rules/` is created only after every existing
//! component was inspected and found to be a real folder, never a link. Nothing here removes a file.

use super::body::{AlwaysSkill, SkillsForBlock, render_bodies};
use super::check::{Drift, drift};
use super::error::{RulesError, RulesErrorKind as Kind, codes};
use super::input::{
    AGENTS_MD, CLAUDE_MD, LOCAL_RULES, LocalRulesRead, ProjectFile, RulesFiles,
    read_local_rules_checked,
};
use super::markers::find_block;
use super::plan::{RulesInput, RulesPlan, decode, plan};
use super::text::split_lines;
use super::thin::{
    CURSOR_MDC, GEMINI_MD, IMPORT, ThinFilePlan, ThinInput, cursor_trigger, plan_thin,
};
use crate::config::{AgentId, ProjectConfig};
use crate::error::Error;
use crate::fsx::{self, FsxError, FsxErrorKind, LockOptions, WriteReceipt, links::Entry};
use crate::plan::{
    Action, Basis, ChangePlan, ItemResult, Outcome, Owner, PlanError, PlanItem, Target,
    write_target,
};
use crate::tasks::CancelToken;
use std::path::{Path, PathBuf};

/// The project files a rules sync reads, as read once from one root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RulesSnapshot<'a> {
    root: PathBuf,
    files: RulesFiles,
    /// Which in-project local Rules entry was refused as a link or reparse entry; `files.local_rules` is `None`.
    local_rules_refused: Option<&'static str>,
    gemini_md: ProjectFile,
    cursor_mdc: ProjectFile,
    detected: &'a [AgentId],
}

impl<'a> RulesSnapshot<'a> {
    /// Reads the block inputs and Gemini's possible existing file. Cursor's file is read only when detected.
    pub fn read(root: &Path, detected: &'a [AgentId]) -> Result<Self, FsxError> {
        let agents = ProjectFile::read(root, AGENTS_MD)?;
        let claude = ProjectFile::read(root, CLAUDE_MD)?;
        let (local_rules, local_rules_refused) = match read_local_rules_checked(root)? {
            LocalRulesRead::Missing => (None, None),
            LocalRulesRead::Regular(bytes) => (Some(bytes), None),
            LocalRulesRead::RefusedLink { entry } => (None, Some(entry)),
        };
        let files = RulesFiles {
            agents,
            claude,
            local_rules,
        };
        Ok(RulesSnapshot {
            root: root.to_path_buf(),
            files,
            local_rules_refused,
            gemini_md: ProjectFile::read(root, GEMINI_MD)?,
            cursor_mdc: if cursor_trigger(detected).is_some() {
                ProjectFile::read(root, CURSOR_MDC)?
            } else {
                ProjectFile::Missing
            },
            detected,
        })
    }
}

/// The managed block of `AGENTS.md` as it is on disk, markers included.
pub(crate) fn read_block(root: &Path) -> Option<String> {
    let file = ProjectFile::read(root, AGENTS_MD).ok()?;
    let text = std::str::from_utf8(file.bytes()?).ok()?;
    let lines = split_lines(text);
    let (begin, end) = find_block(&lines).ok()??;
    Some(lines[begin..=end].concat())
}

/// The config snapshot, the always-on skills (empty until `skills::lock` exists, D-14) and the `--lang` value
/// (`None`: detect from the block, else `en`).
#[derive(Debug, Clone, Copy)]
pub struct SyncInput<'a> {
    pub config: &'a ProjectConfig,
    pub always: &'a [AlwaysSkill],
    pub lang: Option<&'a str>,
}

/// One file a sync would write: its fixed project-relative path, its action, and its bytes before and after.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileWrite {
    pub path: &'static str,
    pub action: Action,
    pub before: Option<Vec<u8>>,
    pub after: Vec<u8>,
}

impl FileWrite {
    pub fn needs_write(&self) -> bool {
        self.before.as_deref() != Some(self.after.as_slice())
    }

    pub(crate) fn plan_item(&self, root: &Path) -> PlanItem {
        let target = target_path(root, self.path);
        PlanItem {
            id: format!(
                "rules:{}:{}",
                crate::paths::folder_id(&target),
                action_id(self.action)
            ),
            target,
            owner: Owner::Project,
            action: self.action,
            summary: summary(self),
            detail: None,
        }
    }

    pub(crate) fn basis(&self, root: &Path) -> Basis {
        basis(target_path(root, self.path), self.before.as_deref())
    }
}

fn action_id(action: Action) -> &'static str {
    match action {
        Action::Create => "create",
        Action::Update => "update",
        Action::Append => "append",
        Action::ReplaceBlock => "replace-block",
        Action::Link => "link",
        Action::Unlink => "unlink",
        Action::Switch => "switch",
        Action::Move => "move",
        Action::Remove => "remove",
        Action::Keep => "keep",
    }
}

/// The planned `.oah/rules.local.md` source state, anchored to the project root it was read from so apply
/// rechecks it with the same defensive local reader rather than `plan::Basis::holds`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct LocalRulesBasis {
    pub(crate) root: PathBuf,
    pub(crate) fingerprint: Option<fsx::Fingerprint>,
}

impl LocalRulesBasis {
    pub(crate) fn target(&self) -> PathBuf {
        target_path(&self.root, LOCAL_RULES)
    }

    pub(crate) fn recheck(&self) -> Result<(), Error> {
        let now = match read_local_rules_checked(&self.root)? {
            LocalRulesRead::Missing => None,
            LocalRulesRead::Regular(bytes) => Some(fsx::Fingerprint::of(&bytes)),
            LocalRulesRead::RefusedLink { entry } => {
                return Err(refuse_local_rules_link(entry).into());
            }
        };
        if now != self.fingerprint {
            return Err(PlanError::Stale {
                target: self.target(),
            }
            .into());
        }
        Ok(())
    }
}

/// A computed sync: the block plan, the thin-file plans, and every planned file in write order (`AGENTS.md`,
/// `CLAUDE.md`, then the thin files), including the ones that already match.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncPlan {
    root: PathBuf,
    pub(crate) config_source: Basis,
    pub(crate) local_rules_source: LocalRulesBasis,
    pub rules: RulesPlan,
    pub thin: Vec<ThinFilePlan>,
    pub files: Vec<FileWrite>,
}

/// One drift finding of [`check_sync`]: always `OAH_RULES_DRIFT`, the file it is about, and v0.4.0's line for
/// `AGENTS.md` and `CLAUDE.md`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DriftLine {
    pub code: &'static str,
    pub path: &'static str,
    pub message: String,
}

impl SyncPlan {
    /// The files that differ from what is on disk.
    pub fn writes(&self) -> impl Iterator<Item = &FileWrite> {
        self.files.iter().filter(|file| file.needs_write())
    }

    /// The drift lines: v0.4.0's for `AGENTS.md` and `CLAUDE.md`, then one per planned thin file that differs.
    pub fn drift(&self) -> Vec<DriftLine> {
        let mut lines: Vec<DriftLine> = drift(&self.rules)
            .into_iter()
            .map(|found| DriftLine {
                code: codes::DRIFT,
                path: match found {
                    Drift::AgentsMissing | Drift::BlockDiffers => AGENTS_MD,
                    Drift::ClaudeMissing | Drift::ClaudeNoImport => CLAUDE_MD,
                },
                message: found.message(&self.rules),
            })
            .collect();
        for thin in self.thin.iter().filter(|thin| thin.needs_write()) {
            let message = match (thin.path, &thin.before) {
                (path, None) => format!("{path} is missing"),
                (GEMINI_MD, Some(_)) => format!("{GEMINI_MD} has no {IMPORT} line"),
                (path, Some(_)) => format!("{path} differs from the OAH rule"),
            };
            lines.push(DriftLine {
                code: codes::DRIFT,
                path: thin.path,
                message,
            });
        }
        lines
    }

    /// The C4 plan includes the files to write and the config source; `.oah/rules.local.md` is retained separately
    /// on [`SyncPlan`] so it is never re-read through `plan::Basis::holds`'s link-following reader.
    pub fn to_change_plan(&self) -> ChangePlan {
        ChangePlan {
            id: String::from("rules-sync"),
            items: self
                .writes()
                .map(|file| file.plan_item(&self.root))
                .collect(),
            warnings: Vec::new(),
            basis: std::iter::once(self.config_source.clone())
                .chain(self.writes().map(|file| file.basis(&self.root)))
                .collect(),
        }
    }
}

fn basis(target: PathBuf, bytes: Option<&[u8]>) -> Basis {
    Basis {
        target,
        fingerprint: bytes.map(fsx::Fingerprint::of),
    }
}

/// The absolute target of a fixed project-relative path.
pub fn target_path(root: &Path, relative: &str) -> PathBuf {
    relative
        .split('/')
        .fold(root.to_path_buf(), |path, part| path.join(part))
}

fn summary(file: &FileWrite) -> String {
    match file.action {
        Action::Create => format!("Create {}", file.path),
        Action::ReplaceBlock => format!("Replace the OAH block in {}", file.path),
        Action::Append if file.path == AGENTS_MD => format!("Add the OAH block to {}", file.path),
        Action::Append => format!("Add the {IMPORT} line to {}", file.path),
        _ => format!("Update {}", file.path),
    }
}

fn refuse_local_rules_link(entry: &'static str) -> RulesError {
    let message = if entry == LOCAL_RULES {
        format!(
            "{LOCAL_RULES} is a link or reparse entry; oah only reads regular project files for \
             {LOCAL_RULES}. Replace it with a regular file."
        )
    } else {
        format!(
            "{entry} is a link or reparse entry on the path to {LOCAL_RULES}; oah only reads \
             regular project files for {LOCAL_RULES}. Replace it with a regular folder."
        )
    };
    RulesError::new(Kind::Link, message)
}

/// Renders the 1.0 body from `input` and plans every file; writes nothing. A refusal plans nothing.
pub fn plan_sync(
    snapshot: &RulesSnapshot<'_>,
    input: &SyncInput<'_>,
) -> Result<SyncPlan, RulesError> {
    if let Some(entry) = snapshot.local_rules_refused {
        return Err(refuse_local_rules_link(entry));
    }
    let effective = input.config.effective();
    let skills = SkillsForBlock::from_config(effective, input.always.to_vec());
    let bodies = render_bodies(effective, &skills);
    let rules = plan(&RulesInput {
        files: &snapshot.files,
        lang: input.lang,
        bodies: &bodies,
    })?;
    let thin = plan_thin(&ThinInput {
        detected: snapshot.detected,
        gemini_md: &snapshot.gemini_md,
        cursor_mdc: &snapshot.cursor_mdc,
    })?;
    let agents_action = match rules.agents_before.as_deref() {
        None => Action::Create,
        Some(bytes) => match find_block(&split_lines(decode(bytes, AGENTS_MD)?))? {
            Some(_) => Action::ReplaceBlock,
            None => Action::Append,
        },
    };
    let import_action = |before: &Option<Vec<u8>>| match before {
        None => Action::Create,
        Some(_) => Action::Append,
    };
    let mut files = vec![
        FileWrite {
            path: AGENTS_MD,
            action: agents_action,
            before: rules.agents_before.clone(),
            after: rules.agents_after.clone(),
        },
        FileWrite {
            path: CLAUDE_MD,
            action: import_action(&rules.claude_before),
            before: rules.claude_before.clone(),
            after: rules.claude_after.clone(),
        },
    ];
    for thin in &thin {
        let action = match (thin.path, &thin.before) {
            (_, None) => Action::Create,
            (GEMINI_MD, Some(_)) => Action::Append,
            (_, Some(_)) => Action::Update,
        };
        files.push(FileWrite {
            path: thin.path,
            action,
            before: thin.before.clone(),
            after: thin.after.clone(),
        });
    }
    let config = input.config.file();
    Ok(SyncPlan {
        root: snapshot.root.clone(),
        config_source: basis(config.path().to_path_buf(), config.source_bytes()),
        local_rules_source: LocalRulesBasis {
            root: snapshot.root.clone(),
            fingerprint: snapshot
                .files
                .local_rules
                .as_deref()
                .map(fsx::Fingerprint::of),
        },
        rules,
        thin,
        files,
    })
}

/// Plans and reports drift; writes nothing. A refusal is returned as the error a sync would hit.
pub fn check_sync(
    snapshot: &RulesSnapshot<'_>,
    input: &SyncInput<'_>,
) -> Result<(SyncPlan, Vec<DriftLine>), RulesError> {
    let plan = plan_sync(snapshot, input)?;
    let lines = plan.drift();
    Ok((plan, lines))
}

/// What an apply did: every planned write with its result class, the receipts of verified writes, and why it
/// stopped, if it did. `error` is `None` exactly when every write is `done` and read back as planned.
#[derive(Debug, Default)]
pub struct SyncResult {
    pub items: Vec<(&'static str, Action, ItemResult)>,
    pub receipts: Vec<WriteReceipt>,
    pub error: Option<Error>,
}

impl SyncResult {
    pub fn synced(&self) -> bool {
        self.error.is_none()
    }
}

/// Writes every file of `plan` that needs a write, with the production writer: the missing folders on the way
/// (after a link check), then the C6 writer of [`write_target`] (lock, basis re-check, atomic write).
pub fn apply_sync(plan: &SyncPlan, cancel: &CancelToken, lock: LockOptions) -> SyncResult {
    apply_sync_with(plan, cancel, |target, bytes| {
        let root = &plan.root;
        match prepare_target(root, target.path) {
            Ok(_) => write_target(target, bytes, lock),
            Err(error) => Outcome::Failed(error),
        }
    })
}

/// Writes one planned rules file as a C6 writer and reads it back: the missing folders of the target (after a link
/// check), then [`write_target`] (lock, basis re-check, atomic write), then a read-back that must equal `bytes`
/// (`OAH_RULES_VERIFY` otherwise). This is the executor of sync step 3 (`app::sync`).
pub fn write_file(root: &Path, target: &Target<'_>, bytes: &[u8], lock: LockOptions) -> Outcome {
    let relative = match prepare_target(root, target.path) {
        Ok(relative) => relative,
        Err(error) => return Outcome::Failed(error),
    };
    match write_target(target, bytes, lock) {
        Outcome::Done(receipts) => match read_back(root, relative, bytes) {
            Ok(()) => Outcome::Done(receipts),
            Err(error) => Outcome::Unknown(error),
        },
        other => other,
    }
}

fn prepare_target(root: &Path, target: &Path) -> Result<&'static str, Error> {
    let relative = [AGENTS_MD, CLAUDE_MD, GEMINI_MD, CURSOR_MDC]
        .into_iter()
        .find(|relative| target_path(root, relative) == target)
        .ok_or_else(|| PlanError::MissingBasis {
            target: target.to_path_buf(),
        })?;
    make_parents(root, relative)?;
    Ok(relative)
}

/// [`apply_sync`] with the writer as a parameter, so a test can supply one; every write it reports `done` is
/// read back without following links and compared with the planned bytes. A difference is `unknown` with
/// `OAH_RULES_VERIFY`, and the apply stops there.
pub fn apply_sync_with<W>(plan: &SyncPlan, cancel: &CancelToken, mut write: W) -> SyncResult
where
    W: FnMut(&Target<'_>, &[u8]) -> Outcome,
{
    let root = &plan.root;
    if let Err(error) = plan.local_rules_source.recheck() {
        return SyncResult {
            error: Some(error),
            ..SyncResult::default()
        };
    }
    let change = plan.to_change_plan();
    let ids: Vec<&str> = change.items.iter().map(|item| item.id.as_str()).collect();
    let accepted = change.accept(&ids);
    let applied = crate::plan::apply(&accepted, cancel, |target| {
        let Some(file) = plan
            .files
            .iter()
            .find(|file| target_path(root, file.path) == target.path)
        else {
            return Outcome::Failed(
                RulesError::new(
                    Kind::Verify,
                    format!("{} is not a planned rules file", target.path.display()),
                )
                .into(),
            );
        };
        match write(target, &file.after) {
            Outcome::Done(receipts) => match read_back(root, file.path, &file.after) {
                Ok(()) => Outcome::Done(receipts),
                Err(error) => Outcome::Unknown(error),
            },
            other => other,
        }
    });
    match applied {
        Ok(report) => SyncResult {
            items: report
                .items
                .iter()
                .map(|result| {
                    let file = plan
                        .files
                        .iter()
                        .find(|file| target_path(root, file.path) == result.target)
                        .expect("every item comes from a planned file");
                    (file.path, file.action, result.clone())
                })
                .collect(),
            receipts: report.receipts,
            error: report.stop,
        },
        Err(error) => SyncResult {
            error: Some(error),
            ..SyncResult::default()
        },
    }
}

/// Reads `relative` back without following links; anything but exactly `planned` is `OAH_RULES_VERIFY`.
fn read_back(root: &Path, relative: &str, planned: &[u8]) -> Result<(), Error> {
    match ProjectFile::read(root, relative)? {
        ProjectFile::Regular(bytes) if bytes == planned => Ok(()),
        _ => Err(RulesError::new(
            Kind::Verify,
            format!("{relative} did not read back as written; check the disk and retry"),
        )
        .into()),
    }
}

/// Creates the missing folders of `relative`'s parent under `root`. Every existing component must be a real
/// folder: a link or a file there is refused before anything is created.
fn make_parents(root: &Path, relative: &str) -> Result<(), Error> {
    let parts: Vec<&str> = relative.split('/').collect();
    let mut path = root.to_path_buf();
    for part in &parts[..parts.len().saturating_sub(1)] {
        path.push(part);
        match fsx::links::inspect(&path)? {
            Entry::Directory => {}
            Entry::Missing => {
                std::fs::create_dir(&path).map_err(|error| {
                    FsxError::io(FsxErrorKind::Io, &path, "cannot create the folder", error)
                })?;
            }
            Entry::Symlink { target } | Entry::Junction { target } => {
                return Err(RulesError::new(
                    Kind::Link,
                    format!(
                        "{} is a link to {}; oah never writes through links, which could reach global files. \
                         Replace it with a regular folder.",
                        path.display(),
                        target.display()
                    ),
                )
                .into());
            }
            _ => {
                return Err(FsxError::new(
                    FsxErrorKind::Io,
                    &path,
                    "not a folder; a folder is needed here",
                )
                .into());
            }
        }
    }
    Ok(())
}
