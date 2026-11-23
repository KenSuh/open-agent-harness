//! Read-only admission for publishing a reviewed GitHub skill in a project.

use std::collections::BTreeMap;
use std::fmt;

use super::gate::{GateDecision, GatePolicy, GateResult, gate};
use super::identity::{
    GITHUB_LOCATOR_PREFIX, Identity, SourceSpec, portable_paths, stored_identity,
};
use super::record::{ReviewedSkillV1, SnapshotReview, record_from_snapshot};
use super::snapshot::Snapshot;
use super::store::{Store, StoreError};
use crate::config::AgentId;
use crate::error::{Coded, codes};

/// Confirmation for a single immutable review, including any warnings shown for it.
#[derive(Debug, Clone, Copy)]
pub struct ReviewApproval<'a> {
    pub review_id: &'a str,
    pub acknowledge_warnings: bool,
}

/// The canonical repository and literal, case-sensitive folder of a reviewed GitHub skill.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitHubSkillSource {
    repo: String,
    path: String,
}

impl GitHubSkillSource {
    /// Validates the source identity of a record loaded from [`Store`]. Compare with `Eq` after
    /// matching the prior record to the installed acquisition. This does not reapply its old policy.
    pub fn from_record(record: &ReviewedSkillV1) -> Result<Self, DeliveryError> {
        let (source, _, _) = Self::check(record)?;
        Ok(source)
    }

    pub fn repo(&self) -> &str {
        &self.repo
    }

    pub fn path(&self) -> &str {
        &self.path
    }

    fn check(record: &ReviewedSkillV1) -> Result<(Self, Identity, SourceSpec), DeliveryError> {
        let source = &record.source;
        let invalid = || {
            DeliveryError::InvalidReview("The review does not name the exact GitHub source".into())
        };
        if record.version != 1 || source.provider != "github" {
            return Err(invalid());
        }
        let (identity, spec) =
            stored_identity(&source.locator, &source.path).map_err(|_| invalid())?;
        if identity.source_id != source.source_id || spec.spec_digest() != source.spec_digest {
            return Err(invalid());
        }
        let repo = source
            .locator
            .strip_prefix(GITHUB_LOCATOR_PREFIX)
            .ok_or_else(invalid)?;
        let source = Self {
            repo: repo.into(),
            path: source.path.clone(),
        };
        Ok((source, identity, spec))
    }
}

/// An immutable review and verified payload authorized for one engine.
pub struct ReviewedSkillPayload {
    review: ReviewedSkillV1,
    snapshot: Snapshot,
    source: GitHubSkillSource,
    target: AgentId,
    name: String,
    files: BTreeMap<String, String>,
}

impl ReviewedSkillPayload {
    pub fn review(&self) -> &ReviewedSkillV1 {
        &self.review
    }

    pub fn snapshot(&self) -> &Snapshot {
        &self.snapshot
    }

    pub fn source(&self) -> &GitHubSkillSource {
        &self.source
    }

    pub fn target(&self) -> AgentId {
        self.target
    }

    /// The verified `SKILL.md` name as one portable, visible destination component.
    pub fn name(&self) -> &str {
        &self.name
    }

    /// Every selected file as a slash-relative path and its exact bytes, in path order.
    pub fn files(&self) -> impl ExactSizeIterator<Item = (&str, &[u8])> {
        // Only admission constructs this index, from keys already verified against this snapshot.
        self.files
            .iter()
            .map(|(path, sha256)| (path.as_str(), self.snapshot.blobs[sha256].as_slice()))
    }
}

#[derive(Debug)]
pub enum DeliveryError {
    Store(StoreError),
    ReviewMissing,
    InvalidReview(String),
    Blocked(GateDecision),
    UnsupportedTarget(AgentId),
    IncompatibleTarget(AgentId),
    ApprovalRequired,
}

impl Coded for DeliveryError {
    fn code(&self) -> &'static str {
        match self {
            Self::Store(error) => error.code(),
            Self::ReviewMissing => "OAH_SKILL_REVIEW_MISSING",
            Self::InvalidReview(_) => "OAH_SKILL_REVIEW_INVALID",
            Self::Blocked(_) => "OAH_SKILL_INSTALL_BLOCKED",
            Self::UnsupportedTarget(_) => "OAH_SKILL_TARGET_UNSUPPORTED",
            Self::IncompatibleTarget(_) => "OAH_SKILL_TARGET_INCOMPATIBLE",
            Self::ApprovalRequired => codes::SKILL_APPROVAL_REQUIRED,
        }
    }

    fn message(&self) -> String {
        match self {
            Self::Store(error) => error.message(),
            Self::ReviewMissing => {
                "The named review could not be found. Review the skill again.".into()
            }
            Self::InvalidReview(detail) => detail.clone(),
            Self::Blocked(decision) => decision
                .reasons
                .iter()
                .filter(|reason| reason.kind.blocks())
                .map(|reason| reason.detail.as_str())
                .collect::<Vec<_>>()
                .join(". "),
            Self::UnsupportedTarget(target) => {
                format!("Project skill installation does not support {target}")
            }
            Self::IncompatibleTarget(target) => format!("This skill is incompatible with {target}"),
            Self::ApprovalRequired => {
                "Acknowledge the warnings for this review before continuing".into()
            }
        }
    }
}

impl fmt::Display for DeliveryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code(), self.message())
    }
}

impl std::error::Error for DeliveryError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Store(error) => Some(error),
            Self::ReviewMissing
            | Self::InvalidReview(_)
            | Self::Blocked(_)
            | Self::UnsupportedTarget(_)
            | Self::IncompatibleTarget(_)
            | Self::ApprovalRequired => None,
        }
    }
}

impl From<StoreError> for DeliveryError {
    fn from(error: StoreError) -> Self {
        Self::Store(error)
    }
}

/// Authorizes only the stored review named by `approval`, using the current effective project
/// license list, with the project's defaults already resolved. Call this on every confirmed apply.
///
/// # Errors
/// Refuses unreadable or inconsistent review data, blocked or incompatible skills, and warnings
/// that were not acknowledged for this review. The caller binds the confirmation to the open
/// project, intended engine and, for updates, the exact installed acquisition.
pub fn authorize(
    store: &Store,
    license_allow: &[String],
    target: AgentId,
    approval: ReviewApproval<'_>,
) -> Result<ReviewedSkillPayload, DeliveryError> {
    if !matches!(target, AgentId::Claude | AgentId::Codex) {
        return Err(DeliveryError::UnsupportedTarget(target));
    }
    let review = store
        .review(approval.review_id)?
        .ok_or(DeliveryError::ReviewMissing)?;
    let (source, identity, spec) = GitHubSkillSource::check(&review)?;
    let decision = gate(
        &review,
        &GatePolicy {
            agents: vec![target],
            license_allow: license_allow.to_vec(),
        },
    );
    let warn = match decision.result {
        GateResult::Pass => false,
        GateResult::Warn => true,
        GateResult::Block => return Err(DeliveryError::Blocked(decision)),
    };
    if !decision.compatible.contains(&target) {
        return Err(DeliveryError::IncompatibleTarget(target));
    }

    let snapshot = store.snapshot(&review.source.snapshot)?.ok_or_else(|| {
        DeliveryError::InvalidReview("The snapshot named by the review could not be found".into())
    })?;
    snapshot
        .validate(&spec)
        .map_err(DeliveryError::InvalidReview)?;
    let expected = record_from_snapshot(
        &identity,
        &spec,
        &snapshot,
        SnapshotReview {
            repo: source.repo(),
            path: source.path(),
            candidate: review.candidate.clone(),
            allow: license_allow,
            checked_at: review.checked_at.clone(),
        },
    )
    .map_err(|error| DeliveryError::InvalidReview(error.message()))?;
    if expected != review {
        return Err(DeliveryError::InvalidReview(
            "The review record does not match its verified snapshot".into(),
        ));
    }

    let name = review
        .frontmatter
        .name
        .as_ref()
        .filter(|name| {
            !name.starts_with('.')
                && !name.contains('/')
                && portable_paths(std::slice::from_ref(name)).is_ok()
        })
        .cloned()
        .ok_or_else(|| {
            DeliveryError::InvalidReview(
                "SKILL.md must name one visible, portable skill folder".into(),
            )
        })?;
    let prefix = format!("{}/", source.path());
    let invalid = || {
        DeliveryError::InvalidReview(
            "The manifest must select files inside the exact reviewed folder".into(),
        )
    };
    let files = snapshot.manifest["files"]
        .as_array()
        .ok_or_else(invalid)?
        .iter()
        .map(|file| {
            let path = file["path"]
                .as_str()
                .and_then(|path| path.strip_prefix(&prefix))
                .filter(|path| !path.is_empty())
                .ok_or_else(invalid)?;
            let sha256 = file["sha256"].as_str().ok_or_else(invalid)?;
            Ok((path.to_owned(), sha256.to_owned()))
        })
        .collect::<Result<_, DeliveryError>>()?;
    if warn && !approval.acknowledge_warnings {
        return Err(DeliveryError::ApprovalRequired);
    }
    Ok(ReviewedSkillPayload {
        review,
        snapshot,
        source,
        target,
        name,
        files,
    })
}
