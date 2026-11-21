// 검토 DTO: 데스크톱 start_review 와 oah skills review --json 이 함께 쓰는 요청·결과 모양이다.
//! The review request and outcome on the wire (M1-S3 desktop contract, CONTRACTS C9).
//!
//! `start_review` takes [`ReviewReq`] under the usual `req` key; its terminal task event and
//! `oah skills review --json` carry the same [`ReviewOutcomeDto`]: the stored `ReviewedSkillV1` with its own
//! serialized fields, the install gate's decision as packet §8 shows it, and whether an equal review was already
//! stored. A gate result is `pass`, `warn` or `block`; a reason kind is its kebab name, or `screening:<flag>`
//! for a screening flag (`screening:shell-pipe`).

use super::AgentIdDto;
use crate::review::screening::FlagKind;
use crate::review::{
    GateDecision, GateReason, GateResult, ReasonKind, ReviewOutcome, ReviewedSkillV1,
};
use serde::{Deserialize, Serialize};

/// The folder to review: a GitHub `owner/repo`, the folder inside it, and an optional ref. Only the default
/// branch (`ref` absent, `null` or `"HEAD"`) is reviewed in this increment.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewSpecDto {
    pub repo: String,
    pub path: String,
    #[serde(default, rename = "ref")]
    pub reference: Option<String>,
}

/// `start_review`'s request: `{ spec, search_name? }`. `search_name` comes with a search hit, which this
/// increment does not have; it must be absent or `null`.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewReq {
    pub spec: ReviewSpecDto,
    #[serde(default)]
    pub search_name: Option<String>,
}

/// One gate reason: `kind` as a string, its detail, the flagged `SKILL.md` lines and `overridable` (always
/// `false` until OC-3 is answered).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct GateReasonDto {
    pub kind: String,
    pub detail: String,
    pub lines: Vec<u32>,
    pub overridable: bool,
}

/// The install gate's decision: `{ gate, result, reasons, compatible }`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct GateDecisionDto {
    pub gate: &'static str,
    pub result: &'static str,
    pub reasons: Vec<GateReasonDto>,
    pub compatible: Vec<AgentIdDto>,
}

/// One review's outcome: `{ record, gate, unchanged }`. A passed gate means the review is ready for the later
/// install flow.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ReviewOutcomeDto {
    pub record: ReviewedSkillV1,
    pub gate: GateDecisionDto,
    pub unchanged: bool,
}

/// `pass`, `warn` or `block`.
pub fn result_name(result: GateResult) -> &'static str {
    match result {
        GateResult::Pass => "pass",
        GateResult::Warn => "warn",
        GateResult::Block => "block",
    }
}

/// A reason kind on the wire: its kebab name, or `screening:<flag>`.
pub fn kind_name(kind: ReasonKind) -> String {
    let name = match kind {
        ReasonKind::SourceUnsupported => "source-unsupported",
        ReasonKind::PathUnresolved => "path-unresolved",
        ReasonKind::IdentityCollision => "identity-collision",
        ReasonKind::Collector => "collector",
        ReasonKind::InvalidRecord => "invalid-record",
        ReasonKind::Rejected => "rejected",
        ReasonKind::License => "license",
        ReasonKind::MarkerCollision => "marker-collision",
        ReasonKind::Screening(flag) => return screening_name(flag),
        ReasonKind::Size => "size",
        ReasonKind::MissingReference => "missing-reference",
        ReasonKind::Scripts => "scripts",
    };
    name.to_string()
}

fn screening_name(flag: FlagKind) -> String {
    format!("screening:{}", flag.as_str())
}

impl From<&GateReason> for GateReasonDto {
    fn from(reason: &GateReason) -> GateReasonDto {
        GateReasonDto {
            kind: kind_name(reason.kind),
            detail: reason.detail.clone(),
            lines: reason.lines.clone(),
            overridable: reason.overridable,
        }
    }
}

impl From<&GateDecision> for GateDecisionDto {
    fn from(decision: &GateDecision) -> GateDecisionDto {
        GateDecisionDto {
            gate: decision.gate,
            result: result_name(decision.result),
            reasons: decision.reasons.iter().map(GateReasonDto::from).collect(),
            compatible: decision
                .compatible
                .iter()
                .copied()
                .map(AgentIdDto)
                .collect(),
        }
    }
}

impl From<ReviewOutcome> for ReviewOutcomeDto {
    fn from(outcome: ReviewOutcome) -> ReviewOutcomeDto {
        ReviewOutcomeDto {
            gate: GateDecisionDto::from(&outcome.gate),
            record: outcome.record,
            unchanged: outcome.unchanged,
        }
    }
}
