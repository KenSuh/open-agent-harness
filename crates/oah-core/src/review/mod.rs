// 스킬 검토 핵심: v0.4.0 검토·라이선스 판단을 옮긴 순수 모듈들과, 공유 실행기가 붙을 공개 연결점이다.
//! Skill review and license core (M1-S3): the v0.4.0 review policy `oah-skill-review/v1` and its license
//! classifier, ported field for field, and the new install gate `oah-install-gate/v1`.
//!
//! * [`identity`]: the GitHub `owner/repo` and literal folder checks, the `sk-` source id and the spec.
//! * [`frontmatter`], [`screening`]: what a review reads from `SKILL.md`, as data.
//! * [`license`]: strict text classification, expressions, declared manifests, discovery, verdict and eligibility.
//! * [`snapshot`]: the collector seam ([`snapshot::Collector`], [`snapshot::Tree`]) and the v0.4.0 snapshot manifest.
//! * [`record`]: `ReviewedSkillV1` and [`record::review`]; [`gate`]: the pure install gate.
//! * [`store`]: the reviews and snapshots under `OAH_HOME/state` (CONTRACTS C9) and [`store::review_and_store`],
//!   the review operation over a collector: identity check, record, `unchanged`, writes, gate.
//! * [`delivery`]: admission of a stored review and its exact snapshot for a confirmed project skill operation.
//!
//! Skill text and metadata are only ever data here. Collection uses the caller's collector, and admission
//! returns verified bytes for the caller to publish. The
//! v0.4.0 regular expressions are kept as written and read by a private matcher with Python's rules; the limits of
//! the Unicode tables behind it are listed in its helper module.
//!
//! Errors keep v0.4.0's codes and texts: [`ReviewError`]'s `Display` is the exact v0.4.0 exception text, and
//! [`Coded::message`] the part after the code.

mod json;
mod license_texts;
mod pattern;
mod pystr;

pub mod delivery;
pub mod frontmatter;
pub mod gate;
pub mod identity;
pub mod license;
pub mod record;
pub mod screening;
pub mod snapshot;
pub mod store;

pub use gate::{GATE, GateDecision, GatePolicy, GateReason, GateResult, ReasonKind, gate, refusal};
pub use identity::{Identity, POLICY_VERSION, SourceSpec, hit_paths, identity};
pub use record::{ReviewRequest, ReviewedSkillV1, review};
pub use store::{
    Failure, Prepared, ReviewOutcome, Store, StoreError, commit, prepare, review_and_store,
};

use crate::error::Coded;
use std::fmt;

/// Why a review ended without a record (v0.4.0 `OAH_SKILL_REVIEW_FAILED(<reason>)`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailReason {
    /// The folder or its `SKILL.md` is not at the current commit.
    PathUnresolved,
    /// Another source already holds the truncated source id (needs the review store).
    IdentityCollision,
    /// The collector could not reach or verify the upstream now: an environment failure (exit 69).
    Collector,
}

impl FailReason {
    pub fn as_str(self) -> &'static str {
        match self {
            FailReason::PathUnresolved => "path-unresolved",
            FailReason::IdentityCollision => "identity-collision",
            FailReason::Collector => "collector",
        }
    }
}

/// A review refusal or failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReviewError {
    /// `OAH_SKILL_SOURCE_UNSUPPORTED`: not a GitHub `owner/repo` folder this review accepts.
    SourceUnsupported(String),
    /// `OAH_SKILL_REVIEW_FAILED`.
    Failed { reason: FailReason, detail: String },
}

impl ReviewError {
    /// v0.4.0's code for an unsupported source.
    pub const SOURCE_UNSUPPORTED: &'static str = "OAH_SKILL_SOURCE_UNSUPPORTED";
    /// v0.4.0's code for a failed review.
    pub const REVIEW_FAILED: &'static str = "OAH_SKILL_REVIEW_FAILED";

    /// The failure reason, for a failed review.
    pub fn reason(&self) -> Option<FailReason> {
        match self {
            ReviewError::SourceUnsupported(_) => None,
            ReviewError::Failed { reason, .. } => Some(*reason),
        }
    }

    /// Whether the collector environment failed (v0.4.0 exits 69), rather than the skill.
    pub fn environment(&self) -> bool {
        self.reason() == Some(FailReason::Collector)
    }
}

impl Coded for ReviewError {
    fn code(&self) -> &'static str {
        match self {
            ReviewError::SourceUnsupported(_) => ReviewError::SOURCE_UNSUPPORTED,
            ReviewError::Failed { .. } => ReviewError::REVIEW_FAILED,
        }
    }

    fn message(&self) -> String {
        match self {
            ReviewError::SourceUnsupported(detail) | ReviewError::Failed { detail, .. } => {
                detail.clone()
            }
        }
    }
}

impl fmt::Display for ReviewError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ReviewError::SourceUnsupported(detail) => {
                write!(f, "{}: {detail}", ReviewError::SOURCE_UNSUPPORTED)
            }
            ReviewError::Failed { reason, detail } => {
                write!(
                    f,
                    "{}({}): {detail}",
                    ReviewError::REVIEW_FAILED,
                    reason.as_str()
                )
            }
        }
    }
}

impl std::error::Error for ReviewError {}
