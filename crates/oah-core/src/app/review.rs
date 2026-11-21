// 검토 작업: 붙여 넣은 GitHub 스킬 폴더 하나를 제한된 HTTPS 수집기로 검토해 OAH_HOME/state 에 남기고 설치 게이트 결정을 돌려준다.
//! The review operation (M1-S3): one explicit GitHub skill folder, read at the default branch's commit through the
//! HTTPS collector, kept in the review store and judged by the install gate. `oah skills review --github` and the
//! desktop's `start_review` both run [`review`] and print or send its [`ReviewOutcomeDto`].
//!
//! The token is read here, from the session's environment (`GITHUB_TOKEN`, else `GH_TOKEN`), and only the
//! collector's API requests carry it; nothing logs it, stores it or puts it in a DTO. Cancellation is honoured before
//! the first request and again after the collection, before anything is written. The gate offers a passing skill to
//! every agent and allows the default licenses of DESIGN-C2 §5.2; a project's `license_allow` is not read here.

use super::Ctx;
use crate::config::AgentId;
use crate::dto::{ReviewOutcomeDto, ReviewReq};
use crate::error::{Coded, Error};
use crate::net::Token;
use crate::paths;
use crate::registry::github::{Cause, GitHub};
use crate::review::gate::DEFAULT_LICENSE_ALLOW;
use crate::review::{FailReason, Failure, ReviewError, ReviewRequest, Store, commit, prepare};
use crate::tasks::{Progress, TaskKey};
use serde_json::{Map, Value};
use std::fmt;
use std::time::SystemTime;

/// The task kind of a review.
pub const REVIEW: &str = "review";

/// The progress phase of a review.
const PHASE: &str = "review";

/// A review that ended without an outcome: the review's refusal or failure and, for a collector failure, the
/// typed cause the collector kept. Its code is the cause's `OAH_NET_*` code when it has one, else the review's.
#[derive(Debug)]
pub struct ReviewFailed {
    pub error: ReviewError,
    pub cause: Option<Cause>,
}

impl ReviewFailed {
    /// Whether the collector environment failed (exit 69), rather than the request or the skill.
    pub fn environment(&self) -> bool {
        self.error.environment()
    }
}

impl Coded for ReviewFailed {
    fn code(&self) -> &'static str {
        self.cause
            .as_ref()
            .and_then(Cause::code)
            .unwrap_or_else(|| self.error.code())
    }

    fn message(&self) -> String {
        match &self.cause {
            Some(cause) => format!("{}: {cause}", self.error.message()),
            None => self.error.message(),
        }
    }
}

impl fmt::Display for ReviewFailed {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code(), self.message())
    }
}

impl std::error::Error for ReviewFailed {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.cause
            .as_ref()
            .map(|cause| cause as &(dyn std::error::Error + 'static))
    }
}

/// One review of a folder runs at a time.
pub fn review_key(req: &ReviewReq) -> TaskKey {
    TaskKey::exclusive(
        REVIEW,
        format!(
            "{}:{}",
            req.spec.repo.trim().to_ascii_lowercase(),
            req.spec.path
        ),
    )
}

fn unsupported(detail: impl Into<String>) -> Error {
    Error::Review(ReviewFailed {
        error: ReviewError::SourceUnsupported(detail.into()),
        cause: None,
    })
}

/// The candidate of a pasted GitHub folder (v0.4.0 `--github`): no search, hit or seed.
fn candidate() -> Map<String, Value> {
    let mut candidate = Map::new();
    candidate.insert("provider".to_string(), Value::from("github"));
    for key in ["query_id", "hit", "seed"] {
        candidate.insert(key.to_string(), Value::Null);
    }
    candidate
}

/// Reviews `req.spec` at the default branch's commit, keeps the review and its snapshot under
/// `OAH_HOME/state`, and returns the record with the gate's decision.
pub fn review(ctx: &Ctx<'_>, req: ReviewReq) -> Result<ReviewOutcomeDto, Error> {
    ctx.cancel.check()?;
    if let Some(reference) = req.spec.reference.as_deref().filter(|name| *name != "HEAD") {
        return Err(unsupported(format!(
            "ref {reference:?} is not reviewed in this version; leave it out to review the default branch"
        )));
    }
    if req.search_name.is_some() {
        return Err(unsupported(
            "a search name comes with a search result, which this version does not review; leave it out",
        ));
    }
    let store = Store::new(&paths::user_state_dir(ctx.env)?);
    ctx.progress.progress(Progress {
        phase: PHASE,
        done: 0,
        total: Some(2),
    });
    let transport = ctx.session.transport();
    let collector = GitHub::new(transport.as_ref(), Token::from_env(ctx.env));
    let candidate = candidate();
    let allow: Vec<String> = DEFAULT_LICENSE_ALLOW
        .iter()
        .map(|name| name.to_string())
        .collect();
    let request = ReviewRequest {
        repo: &req.spec.repo,
        path: &req.spec.path,
        candidate: &candidate,
        allow: &allow,
        now: SystemTime::now(),
    };
    let failed = |failure: Failure| match failure {
        Failure::Review(error) => {
            let cause = if error.reason() == Some(FailReason::Collector) {
                collector.take_cause()
            } else {
                None
            };
            Error::Review(ReviewFailed { error, cause })
        }
        Failure::Store(error) => Error::ReviewStore(error),
    };
    let prepared = prepare(&store, &collector, &request).map_err(failed)?;
    ctx.cancel.check()?;
    ctx.progress.progress(Progress {
        phase: PHASE,
        done: 1,
        total: Some(2),
    });
    let outcome = commit(&store, prepared, &AgentId::ALL).map_err(failed)?;
    ctx.progress.progress(Progress {
        phase: PHASE,
        done: 2,
        total: Some(2),
    });
    Ok(ReviewOutcomeDto::from(outcome))
}
