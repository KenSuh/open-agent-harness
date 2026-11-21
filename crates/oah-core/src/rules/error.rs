// 규칙 오류: v0.4.0 의 OAH_RULES_* 거절을 종류별 타입으로 두고, 거절이면 쓰기 계획이 생기지 않게 한다.
//! The typed refusals of the block algorithm. Codes and meanings are v0.4.0's (v0.4:src/oah/rules.py:53-187,
//! CONTRACTS C16); a refusal is returned instead of a plan, so nothing can be written.

use crate::error::Coded;
use std::fmt;

/// The stable codes.
pub mod codes {
    pub const BLOCK_TOO_LARGE: &str = "OAH_RULES_BLOCK_TOO_LARGE";
    pub const BUDGET: &str = "OAH_RULES_BUDGET";
    pub const DRIFT: &str = "OAH_RULES_DRIFT";
    pub const ENCODING: &str = "OAH_RULES_ENCODING";
    pub const LANG: &str = "OAH_RULES_LANG";
    pub const LINK: &str = "OAH_RULES_LINK";
    pub const MARKER_BROKEN: &str = "OAH_RULES_MARKER_BROKEN";
    pub const MARKER_DUPLICATE: &str = "OAH_RULES_MARKER_DUPLICATE";
    pub const NEWER: &str = "OAH_RULES_NEWER";
    /// Raised by the writer when a written file does not read back as planned.
    pub const VERIFY: &str = "OAH_RULES_VERIFY";
}

/// Which refusal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RulesErrorKind {
    BlockTooLarge,
    Budget,
    Encoding,
    Lang,
    Link,
    MarkerBroken,
    MarkerDuplicate,
    Newer,
    /// A written file did not read back as planned (the writer, [`super::sync`]).
    Verify,
}

impl RulesErrorKind {
    pub fn code(self) -> &'static str {
        match self {
            RulesErrorKind::BlockTooLarge => codes::BLOCK_TOO_LARGE,
            RulesErrorKind::Budget => codes::BUDGET,
            RulesErrorKind::Encoding => codes::ENCODING,
            RulesErrorKind::Lang => codes::LANG,
            RulesErrorKind::Link => codes::LINK,
            RulesErrorKind::MarkerBroken => codes::MARKER_BROKEN,
            RulesErrorKind::MarkerDuplicate => codes::MARKER_DUPLICATE,
            RulesErrorKind::Newer => codes::NEWER,
            RulesErrorKind::Verify => codes::VERIFY,
        }
    }
}

/// A refusal: its kind and an English message that ends by saying what to do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RulesError {
    pub kind: RulesErrorKind,
    pub message: String,
}

impl RulesError {
    pub(crate) fn new(kind: RulesErrorKind, message: impl Into<String>) -> RulesError {
        RulesError {
            kind,
            message: message.into(),
        }
    }

    pub fn code(&self) -> &'static str {
        self.kind.code()
    }
}

impl Coded for RulesError {
    fn code(&self) -> &'static str {
        self.kind.code()
    }

    fn message(&self) -> String {
        self.message.clone()
    }
}

impl fmt::Display for RulesError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code(), self.message)
    }
}

impl std::error::Error for RulesError {}
