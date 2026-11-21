// 규칙 점검: 계획과 현재 바이트를 비교해 어긋남만 보고하는 순수 함수다. 아무것도 쓰지 않는다.
//! `oah rules check` without the reading (v0.4:src/oah/rules.py:192-208): drift lines when a file differs from
//! what a sync would write, or the refusal a sync would hit.

use super::error::{RulesError, codes};
use super::plan::{RulesInput, RulesPlan, plan};
use super::thin::IMPORT;

/// One drift finding.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Drift {
    AgentsMissing,
    /// The block in `AGENTS.md` is missing or differs from the body plus `.oah/rules.local.md`.
    BlockDiffers,
    ClaudeMissing,
    ClaudeNoImport,
}

impl Drift {
    pub fn code(self) -> &'static str {
        codes::DRIFT
    }

    /// v0.4.0's drift line, after the code.
    pub fn message(self, plan: &RulesPlan) -> String {
        match self {
            Drift::AgentsMissing => "AGENTS.md is missing".to_string(),
            Drift::BlockDiffers => format!(
                "the managed block in AGENTS.md is missing or differs from the {} template plus .oah/rules.local.md",
                plan.lang.as_str()
            ),
            Drift::ClaudeMissing => "CLAUDE.md is missing".to_string(),
            Drift::ClaudeNoImport => format!("CLAUDE.md has no {IMPORT} line"),
        }
    }
}

/// The drift of a computed plan; empty when both files already match it.
pub fn drift(plan: &RulesPlan) -> Vec<Drift> {
    let mut found = Vec::new();
    match &plan.agents_before {
        None => found.push(Drift::AgentsMissing),
        Some(before) if *before != plan.agents_after => found.push(Drift::BlockDiffers),
        Some(_) => {}
    }
    match &plan.claude_before {
        None => found.push(Drift::ClaudeMissing),
        Some(before) if *before != plan.claude_after => found.push(Drift::ClaudeNoImport),
        Some(_) => {}
    }
    found
}

/// Plans and reports drift; a refusal is returned as the error a sync would hit.
pub fn check(input: &RulesInput<'_>) -> Result<(RulesPlan, Vec<Drift>), RulesError> {
    let plan = plan(input)?;
    let found = drift(&plan);
    Ok((plan, found))
}
