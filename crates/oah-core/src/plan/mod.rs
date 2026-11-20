// 변경 계획: 계획 항목·근거 지문·결과 분류를 두고, 하나뿐인 적용 엔진과 단일 대상 쓰기 도우미를 내보낸다.
//! Change plans and the one apply engine (CONTRACTS C4).
//!
//! A planner builds a [`ChangePlan`] as a pure function over a snapshot: items naming one target each, and a
//! [`Basis`] fingerprint for every file the plan depends on. [`apply`] runs the accepted items: an initial
//! preflight re-reads every basis with no lock held (any difference is `OAH_PLAN_STALE` and nothing is written),
//! then one target at a time, each through its own writer, which takes that target's lock, re-checks the basis,
//! publishes and releases before the next target starts. The engine itself holds no lock, keeps no journal and
//! rolls nothing back; a stop after the first publish is reported item by item.
//!
//! Targets are canonical absolute paths. [`ChangePlan::to_dto`] shows them with `~` for the UI.

mod apply;

pub use apply::{
    Accepted, ApplyReport, ItemResult, ItemStatus, Outcome, Target, apply, config_outcome,
    write_target,
};

use crate::dto::{ApplyReportDto, BasisDto, ChangePlanDto, ItemResultDto, NoticeDto, PlanItemDto};
use crate::error::{Coded, Error};
use crate::fsx::{Fingerprint, read_optional};
use crate::paths::Env;
use serde::Serialize;
use std::path::{Path, PathBuf};

/// A plan: what would be written, shown before anything is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChangePlan {
    pub id: String,
    pub items: Vec<PlanItem>,
    pub warnings: Vec<NoticeDto>,
    pub basis: Vec<Basis>,
}

/// One change to one target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanItem {
    /// Unique within the plan; `apply` takes accepted ids.
    pub id: String,
    /// The canonical absolute path written.
    pub target: PathBuf,
    pub owner: Owner,
    pub action: Action,
    pub summary: String,
    /// The exact text or entry, when there is one.
    pub detail: Option<String>,
}

/// Whose file a target is.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Owner {
    Project,
    User,
    /// An agent's home, by agent id.
    Agent(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Action {
    Create,
    Update,
    Append,
    ReplaceBlock,
    Link,
    Unlink,
    Switch,
    Move,
    Remove,
    Keep,
}

/// The state of one file the plan was computed from: its SHA-256, or `None` when it did not exist.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Basis {
    pub target: PathBuf,
    pub fingerprint: Option<Fingerprint>,
}

impl Basis {
    /// Reads `target` now (without locking) and records what it holds.
    pub fn observe(target: &Path) -> Result<Basis, Error> {
        Ok(Basis {
            target: target.to_path_buf(),
            fingerprint: read_optional(target)?.map(|bytes| Fingerprint::of(&bytes)),
        })
    }

    /// Whether `target` still holds what this basis recorded. Reads without locking.
    pub fn holds(&self) -> Result<bool, Error> {
        let now = read_optional(&self.target)?.map(|bytes| Fingerprint::of(&bytes));
        Ok(now == self.fingerprint)
    }
}

impl ChangePlan {
    /// The items whose ids are in `ids`, in plan order. Ids that name no item are ignored, so nothing outside the
    /// plan can ever be written.
    pub fn accept<S: AsRef<str>>(&self, ids: &[S]) -> Accepted<'_> {
        Accepted::new(self, ids)
    }

    /// The plan as the UI shows it, with targets written with `~`.
    pub fn to_dto(&self, env: &Env) -> ChangePlanDto {
        ChangePlanDto {
            id: self.id.clone(),
            items: self
                .items
                .iter()
                .map(|item| PlanItemDto {
                    id: item.id.clone(),
                    target: crate::dto::display_path(env, &item.target),
                    owner: item.owner.clone(),
                    action: item.action,
                    summary: item.summary.clone(),
                    detail: item.detail.clone(),
                })
                .collect(),
            warnings: self.warnings.clone(),
            basis: self
                .basis
                .iter()
                .map(|basis| BasisDto {
                    target: crate::dto::display_path(env, &basis.target),
                    fingerprint: basis.fingerprint.map(|print| print.to_hex()),
                })
                .collect(),
        }
    }
}

impl ApplyReport {
    /// The report as the UI shows it.
    pub fn to_dto(&self, env: &Env) -> ApplyReportDto {
        ApplyReportDto {
            items: self
                .items
                .iter()
                .map(|item| ItemResultDto {
                    id: item.id.clone(),
                    target: crate::dto::display_path(env, &item.target),
                    status: item.status,
                })
                .collect(),
            error: self.stop.as_ref().map(|error| error.to_dto(env)),
        }
    }
}

/// A plan failure that is not another module's error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PlanError {
    MissingBasis {
        target: PathBuf,
    },
    /// A basis target changed after the plan was computed.
    Stale {
        target: PathBuf,
    },
}

impl Coded for PlanError {
    fn code(&self) -> &'static str {
        match self {
            PlanError::MissingBasis { .. } | PlanError::Stale { .. } => {
                crate::error::codes::PLAN_STALE
            }
        }
    }

    fn message(&self) -> String {
        match self {
            PlanError::MissingBasis { target } => format!(
                "{} has no recorded basis; make a new plan",
                target.display()
            ),
            PlanError::Stale { target } => format!(
                "{} changed after the plan was made; make a new plan",
                target.display()
            ),
        }
    }
}
