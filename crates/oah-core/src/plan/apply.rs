// 계획 적용 엔진: 잠금 없이 모든 근거를 먼저 확인하고, 대상 하나씩 그 대상의 작성기로 쓰며, 결과를 항목별로 정직하게 남긴다.
use super::{Basis, ChangePlan, PlanError, PlanItem};
use crate::config::{ConfigError, SaveOutcome, codes};
use crate::error::Error;
use crate::fsx::{
    FileLock, Fingerprint, FsxErrorKind, LockOptions, WriteReceipt, read_optional, write_atomic,
};
use crate::tasks::CancelToken;
use serde::Serialize;
use std::path::{Path, PathBuf};

/// The accepted items of one plan, in plan order.
#[derive(Debug, Clone)]
pub struct Accepted<'p> {
    plan: &'p ChangePlan,
    items: Vec<&'p PlanItem>,
}

impl<'p> Accepted<'p> {
    pub(super) fn new<S: AsRef<str>>(plan: &'p ChangePlan, ids: &[S]) -> Accepted<'p> {
        let items = plan
            .items
            .iter()
            .filter(|item| ids.iter().any(|id| id.as_ref() == item.id))
            .collect();
        Accepted { plan, items }
    }

    pub fn items(&self) -> &[&'p PlanItem] {
        &self.items
    }
}

/// One target's share of an apply: its accepted items in plan order and the plan's basis for it.
#[derive(Debug, Clone)]
pub struct Target<'p> {
    pub path: &'p Path,
    pub items: Vec<&'p PlanItem>,
    pub basis: Option<&'p Basis>,
}

/// What one target's writer did.
#[derive(Debug)]
pub enum Outcome {
    /// Published and read back; the receipts of every file written.
    Done(Vec<WriteReceipt>),
    /// The basis no longer held under the target's lock; nothing was written.
    Stale,
    /// A failure before the publish step; the target is untouched.
    Failed(Error),
    /// A failure after the publish step; the target may hold the old or the new bytes.
    Unknown(Error),
}

/// The result class of one item.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ItemStatus {
    Done,
    Stale,
    Failed,
    Unknown,
    NotRun,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ItemResult {
    pub id: String,
    pub target: PathBuf,
    pub status: ItemStatus,
}

/// What an apply did once it got past the initial preflight.
#[derive(Debug, Default)]
pub struct ApplyReport {
    /// Every accepted item, in plan order.
    pub items: Vec<ItemResult>,
    /// The receipts of every verified write, for the session's self-write registry.
    pub receipts: Vec<WriteReceipt>,
    /// Why the apply stopped early; `None` when every item is `done`.
    pub stop: Option<Error>,
}

impl ApplyReport {
    pub fn status_of(&self, id: &str) -> Option<ItemStatus> {
        self.items
            .iter()
            .find(|item| item.id == id)
            .map(|item| item.status)
    }
}

/// Applies the accepted items (CONTRACTS C4).
///
/// The accepted items are grouped by target in the order the targets first appear, so several edits to one
/// target are one call of `write` and one publish. Before any write and with no lock held, every basis of the
/// plan is re-read: a difference is `Err(OAH_PLAN_STALE)` and nothing is written; a read failure is returned the
/// same way. A cancellation asked for by then stops here too. After that `write` runs for one target at a time
/// and the first target that is not `done` stops the apply: its items carry its class, later items are
/// `not-run`, and [`ApplyReport::stop`] holds the reason (`OAH_PLAN_STALE` for a stale target). No cancellation
/// check follows the first write.
pub fn apply<F>(
    accepted: &Accepted<'_>,
    cancel: &CancelToken,
    mut write: F,
) -> Result<ApplyReport, Error>
where
    F: FnMut(&Target<'_>) -> Outcome,
{
    for basis in &accepted.plan.basis {
        if !basis.holds()? {
            return Err(PlanError::Stale {
                target: basis.target.clone(),
            }
            .into());
        }
    }
    cancel.check()?;
    let mut report = ApplyReport::default();
    let mut statuses: Vec<(&Path, ItemStatus)> = Vec::new();
    for target in group(accepted) {
        let status = if report.stop.is_some() {
            ItemStatus::NotRun
        } else {
            settle(&mut report, &target, write(&target))
        };
        statuses.push((target.path, status));
    }
    report.items = accepted
        .items
        .iter()
        .map(|item| ItemResult {
            id: item.id.clone(),
            target: item.target.clone(),
            status: statuses
                .iter()
                .find(|(path, _)| *path == item.target.as_path())
                .map_or(ItemStatus::NotRun, |(_, status)| *status),
        })
        .collect();
    Ok(report)
}

fn settle(report: &mut ApplyReport, target: &Target<'_>, outcome: Outcome) -> ItemStatus {
    match outcome {
        Outcome::Done(receipts) => {
            report.receipts.extend(receipts);
            ItemStatus::Done
        }
        Outcome::Stale => {
            report.stop = Some(
                PlanError::Stale {
                    target: target.path.to_path_buf(),
                }
                .into(),
            );
            ItemStatus::Stale
        }
        Outcome::Failed(error) => {
            report.stop = Some(error);
            ItemStatus::Failed
        }
        Outcome::Unknown(error) => {
            report.stop = Some(error);
            ItemStatus::Unknown
        }
    }
}

fn group<'p>(accepted: &Accepted<'p>) -> Vec<Target<'p>> {
    let mut targets: Vec<Target<'p>> = Vec::new();
    for item in &accepted.items {
        match targets
            .iter_mut()
            .find(|target| target.path == item.target.as_path())
        {
            Some(target) => target.items.push(item),
            None => targets.push(Target {
                path: item.target.as_path(),
                items: vec![item],
                basis: accepted
                    .plan
                    .basis
                    .iter()
                    .find(|basis| basis.target == item.target),
            }),
        }
    }
    targets
}

/// Writes `bytes` to one target as a C6 writer: takes the target's lock, re-checks the target's basis under it,
/// publishes atomically and releases the lock. A lock that stays busy is `failed` (`OAH_FILE_BUSY`).
pub fn write_target(target: &Target<'_>, bytes: &[u8], lock: LockOptions) -> Outcome {
    let held = match FileLock::acquire(target.path, lock) {
        Ok(held) => held,
        Err(error) => return Outcome::Failed(error.into()),
    };
    if let Some(basis) = target.basis {
        match read_optional(target.path) {
            Ok(now) if now.as_deref().map(Fingerprint::of) == basis.fingerprint => {}
            Ok(_) => return Outcome::Stale,
            Err(error) => return Outcome::Failed(error.into()),
        }
    }
    let outcome = match write_atomic(target.path, bytes) {
        Ok(receipt) => Outcome::Done(vec![receipt]),
        Err(error) if error.kind() == FsxErrorKind::Unverified => Outcome::Unknown(error.into()),
        Err(error) => Outcome::Failed(error.into()),
    };
    // A lock file that cannot be removed is locked again by the next writer; the publish result stands.
    let _ = held.release();
    outcome
}

/// The outcome of a `config` writer (`save_project`, `save_settings`), which takes its own lock: its snapshot
/// check `OAH_CONFIG_CHANGED` is the target's basis check and is `stale`; `OAH_FILE_UNVERIFIED` is `unknown`; any
/// other error came before its publish and is `failed`.
pub fn config_outcome(result: Result<SaveOutcome, ConfigError>) -> Outcome {
    match result {
        Ok(saved) => {
            let mut receipts: Vec<WriteReceipt> = Vec::new();
            if let Some(crate::config::Backup::Written(receipt)) = saved.backup {
                receipts.push(receipt);
            }
            receipts.extend(saved.written);
            Outcome::Done(receipts)
        }
        Err(error) if error.code() == codes::CHANGED => Outcome::Stale,
        Err(error) if error.code() == FsxErrorKind::Unverified.code() => {
            Outcome::Unknown(error.into())
        }
        Err(error) => Outcome::Failed(error.into()),
    }
}
