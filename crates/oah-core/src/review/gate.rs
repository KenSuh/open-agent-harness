// 설치 관문: 검토 기록 하나를 설계 표의 조건대로 통과·경고·차단으로 판정하고 에이전트별 호환을 계산하는 순수 함수다.
//! The install gate `oah-install-gate/v1` (DESIGN-C2 §5.2, CONTRACTS C9): a pure decision over one v1 record.
//!
//! | Condition | Gate |
//! |---|---|
//! | malformed or inconsistent record or compatibility state | block |
//! | status `rejected`; a review that ended in `path-unresolved`, `identity-collision` or `collector` | block |
//! | license policy mismatch, inconsistent license verdict, or eligibility not `eligible` (so `eligible-by-choice` blocks too) | block |
//! | `marker_collision` | block |
//! | any screening flag (one reason per kind, with its lines) | block |
//! | a listed file over 1 MiB, listed files over 8 MiB in all, or more than 500 listed files | block |
//! | `missing-reference` problems | warn |
//! | `scripts_present` | warn |
//!
//! Reasons come in that order. `overridable` is `false` for every reason until owner choice OC-3 is answered.
//! Compatibility is computed here, not stored: a skill whose `allowed-tools` names a Claude-only tool is offered to
//! Claude Code only; any other skill to every agent of the policy. Malformed or inconsistent records offer no agent.
//! The size row can only see the files the record lists (at most 256); a larger folder is refused earlier, when the
//! collector leaves its snapshot unverified.

use super::FailReason;
use super::ReviewError;
use super::license::{EligibilityStatus, LicenseForm, Verdict, eligibility, parse_expression};
use super::record::{
    CLAUDE_ONLY_TOOLS, Host, LicenseWord, RejectReason, ReviewStatus, ReviewedSkillV1,
    policy_digest,
};
use super::screening::FlagKind;
use super::snapshot::{FILE_COUNT_LIMIT, FILE_LIMIT, SNAPSHOT_LIMIT};
use crate::config::AgentId;

/// The gate's id.
pub const GATE: &str = "oah-install-gate/v1";

/// Default 1.0 allowed licenses (DESIGN-C2 §5.2).
pub const DEFAULT_LICENSE_ALLOW: [&str; 5] =
    ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC"];

/// What the gate weighs besides the record: the agents a passing skill may be offered to (in display order),
/// and the active allowed licenses (`license_allow`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatePolicy {
    pub agents: Vec<AgentId>,
    pub license_allow: Vec<String>,
}

/// The gate's answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateResult {
    Pass,
    Warn,
    Block,
}

/// Which row of the gate a reason comes from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReasonKind {
    SourceUnsupported,
    PathUnresolved,
    IdentityCollision,
    Collector,
    InvalidRecord,
    Rejected,
    License,
    MarkerCollision,
    /// One screening flag kind; its lines are the flagged `SKILL.md` lines.
    Screening(FlagKind),
    Size,
    MissingReference,
    Scripts,
}

impl ReasonKind {
    /// Whether the reason blocks the install; the others warn.
    pub fn blocks(self) -> bool {
        !matches!(self, ReasonKind::MissingReference | ReasonKind::Scripts)
    }
}

/// One reason, shown as it is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateReason {
    pub kind: ReasonKind,
    pub detail: String,
    /// The `SKILL.md` lines a screening reason is about; empty otherwise.
    pub lines: Vec<u32>,
    pub overridable: bool,
}

/// The gate's decision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateDecision {
    pub gate: &'static str,
    pub result: GateResult,
    pub reasons: Vec<GateReason>,
    pub compatible: Vec<AgentId>,
}

fn reason(kind: ReasonKind, detail: impl Into<String>, lines: Vec<u32>) -> GateReason {
    GateReason {
        kind,
        detail: detail.into(),
        lines,
        overridable: false,
    }
}

fn decide(reasons: Vec<GateReason>, compatible: Vec<AgentId>) -> GateDecision {
    let result = if reasons.iter().any(|reason| reason.kind.blocks()) {
        GateResult::Block
    } else if reasons.is_empty() {
        GateResult::Pass
    } else {
        GateResult::Warn
    };
    GateDecision {
        gate: GATE,
        result,
        reasons,
        compatible,
    }
}

fn screening_detail(kind: FlagKind) -> &'static str {
    match kind {
        FlagKind::IgnoreInstructions => {
            "SKILL.md asks the agent to ignore its earlier instructions"
        }
        FlagKind::RoleOverride => "SKILL.md tries to give the agent a new role or system prompt",
        FlagKind::ShellPipe => "SKILL.md pipes a download straight into a shell",
        FlagKind::DestructiveShell => "SKILL.md runs a recursive forced delete (rm -rf)",
    }
}

fn expected_hosts(tools: &[String]) -> Vec<Host> {
    let claude_only = tools
        .iter()
        .any(|tool| CLAUDE_ONLY_TOOLS.contains(&tool.split('(').next().unwrap_or(tool)));
    if claude_only {
        vec![Host::Claude]
    } else {
        vec![Host::Claude, Host::Codex]
    }
}

fn validate_record_state(record: &ReviewedSkillV1) -> Result<(), &'static str> {
    if record.version != 1 {
        return Err("The review record has an unsupported schema version");
    }
    if record.id.is_empty() || record.id != record.content_id() {
        return Err("The review record id does not match its contents");
    }
    if record.source.provider != "github"
        || record.source.source_id.is_empty()
        || record.source.spec_digest.is_empty()
        || record.source.locator.is_empty()
        || record.source.path.is_empty()
        || record.source.snapshot.is_empty()
        || record.source.skill_sha256.is_empty()
        || record.source.skill_lf_sha256.is_empty()
        || record.source.files.is_empty()
    {
        return Err("The review record is missing required source identity fields");
    }
    if record.status == ReviewStatus::Reviewed {
        if record.reason.is_some() {
            return Err("The review record has a rejection reason despite a reviewed status");
        }
        if record.frontmatter.name.is_none() || record.frontmatter.description.is_none() {
            return Err("The review record claims reviewed status without name and description");
        }
    }
    let tools = record.frontmatter.allowed_tools.as_deref().unwrap_or(&[]);
    if record.compatibility.requires_tools != tools {
        return Err("The review record compatibility tools do not match its front matter");
    }
    let want_hosts = expected_hosts(tools);
    if record.compatibility.hosts.is_empty() || record.compatibility.hosts != want_hosts {
        return Err("The review record has a malformed or inconsistent host list");
    }
    let scripts_prefix = format!("{}/scripts/", record.source.path);
    if !record.compatibility.scripts_present
        && record
            .source
            .files
            .iter()
            .any(|file| file.path.starts_with(&scripts_prefix))
    {
        return Err("The review record omits scripts_present despite listed scripts");
    }
    for problem in &record.compatibility.problems {
        if let Some(missing) = problem.strip_prefix("missing-reference: ") {
            if missing.is_empty() {
                return Err("The review record has an empty missing-reference problem");
            }
        } else if problem == "name-mismatch" {
            if record.status != ReviewStatus::Rejected {
                return Err("The review record has name-mismatch without a rejected status");
            }
        } else if problem == "marker-collision: not deliverable inline" {
            if !record.screening.marker_collision {
                return Err(
                    "The review record lists a marker collision without screening.marker_collision",
                );
            }
        } else {
            return Err("The review record carries an unrecognized compatibility problem");
        }
    }
    Ok(())
}

fn license_eligible_under_policy(record: &ReviewedSkillV1, allow: &[String]) -> bool {
    let license = &record.license;
    if license.verdict != LicenseWord::Recognized
        || !matches!(license.form, LicenseForm::Single | LicenseForm::Conjunction)
        || license.governing.is_empty()
    {
        return false;
    }
    let Some(expression) = license.expression.as_deref() else {
        return false;
    };
    let Some((parsed_form, ids)) = parse_expression(expression) else {
        return false;
    };
    if parsed_form != license.form || ids.is_empty() {
        return false;
    }
    let verdict = Verdict {
        form: parsed_form,
        expression: Some(expression.to_string()),
        ids,
        strict: true,
        problems: Vec::new(),
        files: Vec::new(),
    };
    eligibility(&verdict, allow).status == EligibilityStatus::Eligible
}

/// The install decision for one record.
pub fn gate(record: &ReviewedSkillV1, policy: &GatePolicy) -> GateDecision {
    let mut reasons = Vec::new();
    let record_valid = match validate_record_state(record) {
        Ok(()) => true,
        Err(detail) => {
            reasons.push(reason(ReasonKind::InvalidRecord, detail, Vec::new()));
            false
        }
    };
    if record.status == ReviewStatus::Rejected {
        let detail = match record.reason {
            Some(RejectReason::MalformedFrontmatter) => {
                "The review rejected it: SKILL.md has no name or description"
            }
            Some(RejectReason::TooLarge) => {
                "The review rejected it: SKILL.md is larger than 192 KiB"
            }
            Some(RejectReason::NameMismatch) => {
                "The review rejected it: its front-matter name differs from the search result"
            }
            None => "The review rejected it",
        };
        reasons.push(reason(ReasonKind::Rejected, detail, Vec::new()));
    }
    let license = &record.license;
    let expression = license.expression.as_deref().unwrap_or("of this skill");
    let license_detail = if record.policy_digest != policy_digest(&policy.license_allow) {
        Some(
            "The review record was checked under a different license policy; review it again under the active policy"
                .to_string(),
        )
    } else {
        match license.eligibility {
            EligibilityStatus::Eligible => {
                if license_eligible_under_policy(record, &policy.license_allow) {
                    None
                } else {
                    Some(
                        "The stored license verdict is inconsistent with the active license policy"
                            .to_string(),
                    )
                }
            }
            EligibilityStatus::EligibleByChoice => Some(format!(
                "The license {expression} is a choice; only a license that is allowed as a whole installs"
            )),
            EligibilityStatus::Excluded => Some(format!(
                "The license {expression} is not one of the allowed licenses"
            )),
            EligibilityStatus::Unknown => Some(
                "The license could not be confirmed from the repository's license files"
                    .to_string(),
            ),
        }
    };
    if let Some(detail) = license_detail {
        reasons.push(reason(ReasonKind::License, detail, Vec::new()));
    }
    if record.screening.marker_collision {
        let detail = "SKILL.md has a line starting with <!-- oah, which OAH cannot deliver safely";
        reasons.push(reason(ReasonKind::MarkerCollision, detail, Vec::new()));
    }
    let mut kinds: Vec<FlagKind> = record
        .screening
        .flags
        .iter()
        .map(|flag| flag.kind)
        .collect();
    kinds.sort();
    kinds.dedup();
    for kind in kinds {
        let lines = record
            .screening
            .flags
            .iter()
            .filter(|flag| flag.kind == kind)
            .map(|flag| flag.line)
            .collect();
        reasons.push(reason(
            ReasonKind::Screening(kind),
            screening_detail(kind),
            lines,
        ));
    }
    let files = &record.source.files;
    let total: u64 = files
        .iter()
        .map(|file| file.bytes)
        .fold(0, u64::saturating_add);
    if files.iter().any(|file| file.bytes > FILE_LIMIT)
        || total > SNAPSHOT_LIMIT
        || files.len() > FILE_COUNT_LIMIT
    {
        let detail =
            "The folder is over the fetch limits (1 MiB per file, 8 MiB in all, 500 files)";
        reasons.push(reason(ReasonKind::Size, detail, Vec::new()));
    }
    let missing: Vec<&str> = record
        .compatibility
        .problems
        .iter()
        .filter_map(|problem| problem.strip_prefix("missing-reference: "))
        .filter(|path| !path.is_empty())
        .collect();
    if !missing.is_empty() {
        let detail = format!(
            "SKILL.md points at files the folder does not have: {}",
            missing.join(", ")
        );
        reasons.push(reason(ReasonKind::MissingReference, detail, Vec::new()));
    }
    if record.compatibility.scripts_present {
        reasons.push(reason(
            ReasonKind::Scripts,
            "This skill ships scripts your agent may run",
            Vec::new(),
        ));
    }
    let compatible = if !record_valid {
        Vec::new()
    } else if record.compatibility.hosts == [Host::Claude] {
        policy
            .agents
            .iter()
            .copied()
            .filter(|agent| *agent == AgentId::Claude)
            .collect()
    } else if record.compatibility.hosts == [Host::Claude, Host::Codex] {
        policy.agents.clone()
    } else {
        Vec::new()
    };
    decide(reasons, compatible)
}

/// The decision for a review that ended without a record: always a block, offered to no agent.
pub fn refusal(error: &ReviewError) -> GateDecision {
    let kind = match error {
        ReviewError::SourceUnsupported(_) => ReasonKind::SourceUnsupported,
        ReviewError::Failed {
            reason: FailReason::PathUnresolved,
            ..
        } => ReasonKind::PathUnresolved,
        ReviewError::Failed {
            reason: FailReason::IdentityCollision,
            ..
        } => ReasonKind::IdentityCollision,
        ReviewError::Failed {
            reason: FailReason::Collector,
            ..
        } => ReasonKind::Collector,
    };
    decide(
        vec![reason(kind, error.to_string(), Vec::new())],
        Vec::new(),
    )
}
