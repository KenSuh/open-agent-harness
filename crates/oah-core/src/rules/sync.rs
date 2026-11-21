// 규칙 쓰기: 블록·CLAUDE.md·얇은 파일 계획을 프로젝트 안 고정 경로에만 계획 엔진으로 원자적으로 쓰고 다시 읽어 확인한다.
//! The rules writer (design §3.4 "When files are written", CONTRACTS C12). [`RulesSnapshot::read`] reads the five
//! project files without following links; [`plan_sync`] renders the 1.0 body from the effective config and plans
//! the block, `CLAUDE.md` and the thin files (pure, nothing written); [`check_sync`] turns a plan into drift lines
//! without writing; [`apply_sync`] writes every planned file through the one apply engine (C4): a basis per file,
//! the initial preflight, one target at a time under its own lock, each write atomic and then read back. A file
//! that does not read back as planned is `OAH_RULES_VERIFY`.
//!
//! Every target is a fixed project-relative path under the project root: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` and
//! `.cursor/rules/oah.mdc`. A missing folder on the way to `.cursor/rules/` is created only after every existing
//! component was inspected and found to be a real folder, never a link. Nothing here removes a file.

use super::body::{AlwaysSkill, SkillsForBlock, render_bodies};
use super::check::{Drift, drift};
use super::error::{RulesError, RulesErrorKind as Kind, codes};
use super::input::{AGENTS_MD, CLAUDE_MD, ProjectFile, RulesFiles};
use super::markers::find_block;
use super::plan::{RulesInput, RulesPlan, decode, plan};
use super::text::split_lines;
use super::thin::{CURSOR_MDC, GEMINI_MD, IMPORT, ThinFilePlan, ThinInput, plan_thin};
use crate::config::{AgentId, Effective};
use crate::error::Error;
use crate::fsx::{self, FsxError, FsxErrorKind, LockOptions, WriteReceipt, links::Entry};
use crate::plan::{
    Action, Basis, ChangePlan, ItemResult, Outcome, Owner, PlanItem, Target, write_target,
};
use crate::tasks::CancelToken;
use std::path::{Path, PathBuf};

/// The project files a rules sync reads, as read once from one root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RulesSnapshot {
    pub root: PathBuf,
    pub files: RulesFiles,
    pub gemini_md: ProjectFile,
    pub cursor_mdc: ProjectFile,
}

impl RulesSnapshot {
    /// Reads `AGENTS.md`, `CLAUDE.md`, `.oah/rules.local.md`, `GEMINI.md` and `.cursor/rules/oah.mdc` under
    /// `root`. Reading writes nothing.
    pub fn read(root: &Path) -> Result<RulesSnapshot, FsxError> {
        Ok(RulesSnapshot {
            root: root.to_path_buf(),
            files: RulesFiles::read(root)?,
            gemini_md: ProjectFile::read(root, GEMINI_MD)?,
            cursor_mdc: ProjectFile::read(root, CURSOR_MDC)?,
        })
    }

    /// The managed block of `AGENTS.md` as it is on disk, markers included; `None` when there is none.
    pub fn block_text(&self) -> Option<String> {
        let text = std::str::from_utf8(self.files.agents.bytes()?).ok()?;
        let lines = split_lines(text);
        let (begin, end) = find_block(&lines).ok()??;
        Some(lines[begin..=end].concat())
    }
}

/// What the writer plans from: the effective config, the always-on skills (empty until `skills::lock` exists,
/// D-14), the agents the caller detected as installed (a read, no `InstalledAgent` witness, D-33) and the
/// `--lang` value (`None`: detect from the block, else `en`).
#[derive(Debug, Clone, Copy)]
pub struct SyncInput<'a> {
    pub effective: &'a Effective,
    pub always: &'a [AlwaysSkill],
    pub detected: &'a [AgentId],
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
}

/// A computed sync: the block plan, the thin-file plans, and every planned file in write order (`AGENTS.md`,
/// `CLAUDE.md`, then the thin files), including the ones that already match.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncPlan {
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

    /// The plan as one C4 change plan over the files that need a write, each with its basis.
    pub fn to_change_plan(&self, root: &Path) -> ChangePlan {
        let mut items = Vec::new();
        let mut basis = Vec::new();
        for file in self.writes() {
            let target = target_path(root, file.path);
            items.push(PlanItem {
                id: item_id(file.path),
                target: target.clone(),
                owner: Owner::Project,
                action: file.action,
                summary: summary(file),
                detail: None,
            });
            basis.push(Basis {
                target,
                fingerprint: file.before.as_deref().map(fsx::Fingerprint::of),
            });
        }
        ChangePlan {
            id: String::from("rules-sync"),
            items,
            warnings: Vec::new(),
            basis,
        }
    }
}

/// The stable id of the item that writes `path` (the same work has the same id in a plan and in its apply).
pub fn item_id(path: &str) -> String {
    format!("rules:{path}")
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

/// Renders the 1.0 body from `input` and plans every file; writes nothing. A refusal plans nothing.
pub fn plan_sync(snapshot: &RulesSnapshot, input: &SyncInput<'_>) -> Result<SyncPlan, RulesError> {
    let skills = SkillsForBlock::from_config(input.effective, input.always.to_vec());
    let bodies = render_bodies(input.effective, &skills);
    let rules = plan(&RulesInput {
        files: &snapshot.files,
        lang: input.lang,
        bodies: &bodies,
    })?;
    let thin = plan_thin(&ThinInput {
        detected: input.detected,
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
    Ok(SyncPlan { rules, thin, files })
}

/// Plans and reports drift; writes nothing. A refusal is returned as the error a sync would hit.
pub fn check_sync(
    snapshot: &RulesSnapshot,
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
pub fn apply_sync(
    root: &Path,
    plan: &SyncPlan,
    cancel: &CancelToken,
    lock: LockOptions,
) -> SyncResult {
    apply_sync_with(root, plan, cancel, |target, bytes| {
        let relative = plan
            .files
            .iter()
            .find(|file| target_path(root, file.path) == target.path)
            .map_or("", |file| file.path);
        if let Err(error) = make_parents(root, relative) {
            return Outcome::Failed(error);
        }
        write_target(target, bytes, lock)
    })
}

/// Writes one planned rules file as a C6 writer and reads it back: the missing folders of `relative` (after a link
/// check), then [`write_target`] (lock, basis re-check, atomic write), then a read-back that must equal `bytes`
/// (`OAH_RULES_VERIFY` otherwise). This is the executor of sync step 3 (`app::sync`).
pub fn write_file(
    root: &Path,
    relative: &str,
    target: &Target<'_>,
    bytes: &[u8],
    lock: LockOptions,
) -> Outcome {
    if let Err(error) = make_parents(root, relative) {
        return Outcome::Failed(error);
    }
    match write_target(target, bytes, lock) {
        Outcome::Done(receipts) => match read_back(root, relative, bytes) {
            Ok(()) => Outcome::Done(receipts),
            Err(error) => Outcome::Unknown(error),
        },
        other => other,
    }
}

/// [`apply_sync`] with the writer as a parameter, so a test can supply one; every write it reports `done` is
/// read back without following links and compared with the planned bytes. A difference is `unknown` with
/// `OAH_RULES_VERIFY`, and the apply stops there.
pub fn apply_sync_with<W>(
    root: &Path,
    plan: &SyncPlan,
    cancel: &CancelToken,
    mut write: W,
) -> SyncResult
where
    W: FnMut(&Target<'_>, &[u8]) -> Outcome,
{
    let change = plan.to_change_plan(root);
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
