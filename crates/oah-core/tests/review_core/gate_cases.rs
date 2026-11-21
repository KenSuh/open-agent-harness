// 설치 관문 사례: 고정 기록과 그 무해한 변형을 관문에 넣어 통과·경고·차단, 이유 순서와 줄, 에이전트 호환을 확인한다.
//! The install gate over the frozen records and benign variations of them. Skill text and metadata stay data:
//! nothing here fetches, installs or runs anything.

use crate::config::AgentId;
use crate::fixtures::{case, review_case, strings_at};
use crate::review::identity::identity;
use crate::review::license::{EligibilityStatus, LicenseForm};
use crate::review::record::RecordFile;
use crate::review::screening::FlagKind;
use crate::review::{
    GateDecision, GatePolicy, GateResult, ReasonKind, ReviewedSkillV1, gate, refusal,
};

fn policy_with(allow: Vec<String>) -> GatePolicy {
    GatePolicy {
        agents: AgentId::ALL.to_vec(),
        license_allow: allow,
    }
}

fn policy_for(name: &str) -> GatePolicy {
    policy_with(strings_at(&case("review", name).input["allow"]))
}

fn policy() -> GatePolicy {
    policy_with(vec!["MIT".to_string()])
}

fn record(name: &str) -> ReviewedSkillV1 {
    review_case(&case("review", name).input).unwrap_or_else(|error| panic!("{name}: {error}"))
}

fn kinds(decision: &GateDecision) -> Vec<ReasonKind> {
    decision.reasons.iter().map(|reason| reason.kind).collect()
}

#[test]
fn a_reviewed_allowed_skill_passes_for_every_agent() {
    for name in [
        "record-mit-reviewed",
        "record-apache-allowed",
        "record-size-at-limit",
    ] {
        let decision = gate(&record(name), &policy_for(name));
        assert_eq!(decision.gate, "oah-install-gate/v1");
        assert_eq!(decision.result, GateResult::Pass, "{name}: {decision:?}");
        assert!(decision.reasons.is_empty(), "{name}: {decision:?}");
        assert_eq!(decision.compatible, AgentId::ALL.to_vec(), "{name}");
    }
}

#[test]
fn screened_text_blocks_with_its_lines_and_claude_only_tools_narrow_the_agents() {
    let record = record("record-screened-claude-only");
    let decision = gate(&record, &policy());
    assert_eq!(decision.result, GateResult::Block);
    let mut flagged: Vec<FlagKind> = record
        .screening
        .flags
        .iter()
        .map(|flag| flag.kind)
        .collect();
    flagged.sort();
    flagged.dedup();
    assert!(!flagged.is_empty(), "the fixture flags lines");
    let mut want = vec![ReasonKind::MarkerCollision];
    want.extend(flagged.iter().map(|kind| ReasonKind::Screening(*kind)));
    want.extend([ReasonKind::MissingReference, ReasonKind::Scripts]);
    assert_eq!(kinds(&decision), want);
    for reason in &decision.reasons {
        if let ReasonKind::Screening(kind) = reason.kind {
            let lines: Vec<u32> = record
                .screening
                .flags
                .iter()
                .filter(|flag| flag.kind == kind)
                .map(|flag| flag.line)
                .collect();
            assert_eq!(reason.lines, lines, "{kind:?}");
        } else {
            assert!(reason.lines.is_empty(), "{reason:?}");
        }
    }
    let missing = decision
        .reasons
        .iter()
        .find(|reason| reason.kind == ReasonKind::MissingReference)
        .expect("missing");
    assert_eq!(
        missing.detail,
        "SKILL.md points at files the folder does not have: references/absent.md"
    );
    let scripts = decision
        .reasons
        .iter()
        .find(|reason| reason.kind == ReasonKind::Scripts)
        .expect("scripts");
    assert_eq!(
        scripts.detail,
        "This skill ships scripts your agent may run"
    );
    assert_eq!(decision.compatible, [AgentId::Claude]);
}

#[test]
fn license_and_review_rejections_block() {
    for (name, first) in [
        ("record-no-license", ReasonKind::License),
        ("record-apache-excluded", ReasonKind::License),
        ("record-name-mismatch", ReasonKind::Rejected),
        ("record-malformed-frontmatter", ReasonKind::Rejected),
        ("record-size-over-limit", ReasonKind::Rejected),
    ] {
        let decision = gate(&record(name), &policy());
        assert_eq!(decision.result, GateResult::Block, "{name}: {decision:?}");
        assert_eq!(
            decision.reasons.first().map(|reason| reason.kind),
            Some(first),
            "{name}: {decision:?}"
        );
    }
}

#[test]
fn a_review_without_a_record_blocks_and_offers_no_agent() {
    for (name, kind) in [
        ("record-path-missing", ReasonKind::PathUnresolved),
        ("record-no-skill-file", ReasonKind::PathUnresolved),
    ] {
        let error = review_case(&case("review", name).input).expect_err(name);
        let decision = refusal(&error);
        assert_eq!(
            (decision.result, kinds(&decision)),
            (GateResult::Block, vec![kind]),
            "{name}"
        );
        assert_eq!(decision.reasons[0].detail, error.to_string());
        assert!(decision.compatible.is_empty());
    }
    let unsupported = identity("gitlab.com/acme/skills", "skills/x").expect_err("not GitHub");
    assert_eq!(
        kinds(&refusal(&unsupported)),
        [ReasonKind::SourceUnsupported]
    );
}

#[test]
fn a_license_choice_blocks_until_a_whole_license_is_allowed() {
    let mut record = record("record-mit-reviewed");
    record.license.form = LicenseForm::Choice;
    record.license.expression = Some("MIT OR ISC".to_string());
    record.license.eligibility = EligibilityStatus::EligibleByChoice;
    record.id = record.content_id();
    let decision = gate(&record, &policy());
    assert_eq!(
        (decision.result, kinds(&decision)),
        (GateResult::Block, vec![ReasonKind::License])
    );
    assert!(
        decision.reasons[0].detail.contains("MIT OR ISC"),
        "{decision:?}"
    );
}

#[test]
fn scripts_and_missing_references_only_warn() {
    let mut record = record("record-mit-reviewed");
    record.compatibility.scripts_present = true;
    record.id = record.content_id();
    assert_eq!(gate(&record, &policy()).result, GateResult::Warn);
    record
        .compatibility
        .problems
        .push("missing-reference: references/gone.md".to_string());
    record.id = record.content_id();
    let decision = gate(&record, &policy());
    assert_eq!(
        (decision.result, kinds(&decision)),
        (
            GateResult::Warn,
            vec![ReasonKind::MissingReference, ReasonKind::Scripts]
        )
    );
    assert_eq!(decision.compatible, AgentId::ALL.to_vec());
}

#[test]
fn listed_files_over_the_fetch_limits_block() {
    let file = |path: &str, bytes: u64| RecordFile {
        path: path.to_string(),
        bytes,
        sha256: String::new(),
    };
    let mut record = record("record-mit-reviewed");
    record
        .source
        .files
        .push(file("skills/grill/big.bin", (1 << 20) + 1));
    record.id = record.content_id();
    assert_eq!(kinds(&gate(&record, &policy())), [ReasonKind::Size]);
    let mut record = self::record("record-mit-reviewed");
    record
        .source
        .files
        .extend((0..9).map(|index| file(&format!("skills/grill/part{index}.bin"), 1 << 20)));
    record.id = record.content_id();
    assert_eq!(gate(&record, &policy()).result, GateResult::Block);
}

#[test]
fn the_gate_is_pure_and_never_overridable() {
    let policy = policy();
    for name in [
        "record-mit-reviewed",
        "record-screened-claude-only",
        "record-no-license",
        "record-name-mismatch",
    ] {
        let record = record(name);
        let before = record.clone();
        let first = gate(&record, &policy);
        assert_eq!(first, gate(&record, &policy), "{name}");
        assert_eq!(record, before, "{name}");
        assert!(
            first.reasons.iter().all(|reason| !reason.overridable),
            "{name}"
        );
    }
    let narrow = GatePolicy {
        agents: vec![AgentId::Codex, AgentId::Claude],
        license_allow: vec!["MIT".to_string()],
    };
    assert_eq!(
        gate(&record("record-mit-reviewed"), &narrow).compatible,
        [AgentId::Codex, AgentId::Claude]
    );
    assert_eq!(
        gate(&record("record-screened-claude-only"), &narrow).compatible,
        [AgentId::Claude]
    );
}
