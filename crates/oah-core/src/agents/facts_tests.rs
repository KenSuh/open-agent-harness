// 사실 파일 시험: 들어 있는 facts.toml 이 모든 (에이전트, 능력)을 적고 열린 항목을 맞게 가리키는지, 그리고 D-07 판정을 본다.
use super::{FACTS_TOML, Facts};
use crate::agents::capability::{Basis, Capability, CapabilityName, CapabilityState, Evidence, Os};
use crate::config::AgentId;

const DEFAULT: &str = "status = \"unknown\"\nbasis = \"docs\"\nsource = \"test\"\nos = []\nverified_on = \"\"\nunverified = [\"U-1\"]\n";

/// A complete facts text: every entry `DEFAULT` unless `overrides` gives its table body, then the `[open]` table.
fn text_with(overrides: &[(&str, &str)], open: &str) -> String {
    let mut text = String::new();
    for agent in AgentId::ALL {
        for name in CapabilityName::ALL {
            let table = format!("{}.{}", agent.as_str(), name.as_str());
            let body = overrides
                .iter()
                .find(|(key, _)| *key == table)
                .map_or(DEFAULT, |(_, body)| body);
            text.push_str(&format!("[{table}]\n{body}\n"));
        }
    }
    text.push_str(&format!("[open]\n{open}\n"));
    text
}

fn parsed(overrides: &[(&str, &str)]) -> &'static Facts {
    let facts = Facts::parse(&text_with(overrides, "U-1 = \"test item\"")).unwrap();
    Box::leak(Box::new(facts))
}

fn refused(overrides: &[(&str, &str)], open: &str) -> String {
    Facts::parse(&text_with(overrides, open)).unwrap_err().0
}

fn entry(status: &str, basis: &str, os: &str, verified_on: &str) -> String {
    format!(
        "status = \"{status}\"\nbasis = \"{basis}\"\nsource = \"src\"\nos = {os}\nverified_on = \"{verified_on}\"\nunverified = []\n"
    )
}

#[test]
fn the_shipped_file_has_one_entry_per_agent_and_capability() {
    let facts = Facts::parse(FACTS_TOML).unwrap();
    for agent in AgentId::ALL {
        for name in CapabilityName::ALL {
            assert!(
                facts.get(agent, name).is_some(),
                "{agent}.{}",
                name.as_str()
            );
        }
    }
}

#[test]
fn only_detection_and_sign_in_of_claude_code_and_codex_are_carried() {
    let facts = Facts::parse(FACTS_TOML).unwrap();
    for agent in AgentId::ALL {
        for name in CapabilityName::ALL {
            let fact = facts.get(agent, name).unwrap();
            let pinned = matches!(agent, AgentId::Claude | AgentId::Codex)
                && matches!(name, CapabilityName::Detect | CapabilityName::SignIn);
            assert_eq!(
                fact.basis == Basis::Carried,
                pinned,
                "{agent}.{}",
                name.as_str()
            );
            assert_ne!(
                fact.basis,
                Basis::Measured,
                "nothing is measured yet: {agent}.{}",
                name.as_str()
            );
        }
    }
}

#[test]
fn every_windows_fact_starts_carried() {
    let facts = Facts::parse(FACTS_TOML).unwrap();
    for agent in AgentId::ALL {
        for name in CapabilityName::ALL {
            let fact = facts.get(agent, name).unwrap();
            if fact.os.contains(&Os::Windows) {
                assert_eq!(fact.basis, Basis::Carried, "{agent}.{}", name.as_str());
            }
        }
    }
}

#[test]
fn on_macos_no_write_bearing_capability_is_usable_yet() {
    let facts = Box::leak(Box::new(Facts::parse(FACTS_TOML).unwrap()));
    for agent in AgentId::ALL {
        for name in CapabilityName::ALL
            .into_iter()
            .filter(|name| name.writes_agent_home())
        {
            let state = facts.evaluate(agent, name, Os::Macos, None).state();
            assert_eq!(state, CapabilityState::Unknown, "{agent}.{}", name.as_str());
        }
    }
    let read = |agent, name| facts.evaluate(agent, name, Os::Macos, None).state();
    assert_eq!(
        read(AgentId::Claude, CapabilityName::SignIn),
        CapabilityState::Verified
    );
    assert_eq!(
        read(AgentId::Copilot, CapabilityName::Detect),
        CapabilityState::Verified
    );
    assert_eq!(
        read(AgentId::Cursor, CapabilityName::Detect),
        CapabilityState::Unknown
    );
    assert_eq!(
        read(AgentId::Gemini, CapabilityName::SignIn),
        CapabilityState::Unknown
    );
}

#[test]
fn a_documented_read_only_fact_is_verified_only_on_its_systems() {
    let facts = parsed(&[(
        "copilot.detect",
        &entry("verified", "docs", "[\"macos\"]", ""),
    )]);
    let expected = Capability::Verified {
        value: (),
        evidence: Evidence {
            basis: Basis::Docs,
            source: "src",
            verified_on: None,
            os: &[Os::Macos],
        },
    };
    assert_eq!(
        facts.evaluate(AgentId::Copilot, CapabilityName::Detect, Os::Macos, None),
        expected
    );
    let windows = facts.evaluate(AgentId::Copilot, CapabilityName::Detect, Os::Windows, None);
    assert_eq!(windows.state(), CapabilityState::Unknown);
}

#[test]
fn a_documented_write_bearing_fact_reads_unknown_and_a_carried_one_is_used() {
    let facts = parsed(&[
        (
            "claude.skills_link",
            &entry("verified", "docs", "[\"macos\"]", ""),
        ),
        (
            "claude.hook_prompt",
            &entry("none", "docs", "[\"macos\"]", ""),
        ),
        (
            "codex.skills_link",
            &entry("verified", "carried", "[\"macos\"]", ""),
        ),
        ("codex.detect", &entry("none", "docs", "[\"macos\"]", "")),
    ]);
    let state = |agent, name| facts.evaluate(agent, name, Os::Macos, None).state();
    assert_eq!(
        state(AgentId::Claude, CapabilityName::SkillsLink),
        CapabilityState::Unknown
    );
    assert_eq!(
        state(AgentId::Claude, CapabilityName::HookPrompt),
        CapabilityState::Unknown
    );
    assert_eq!(
        state(AgentId::Codex, CapabilityName::SkillsLink),
        CapabilityState::Verified
    );
    assert_eq!(
        state(AgentId::Codex, CapabilityName::Detect),
        CapabilityState::None
    );
    assert_eq!(
        state(AgentId::Gemini, CapabilityName::Detect),
        CapabilityState::Unknown
    );
}

#[test]
fn a_measured_fact_reads_unknown_once_the_agent_is_a_major_step_newer() {
    let facts = parsed(&[(
        "claude.model_read",
        &entry("verified", "measured", "[\"macos\"]", "2.1.0"),
    )]);
    let state = |version| {
        facts
            .evaluate(
                AgentId::Claude,
                CapabilityName::ModelRead,
                Os::Macos,
                version,
            )
            .state()
    };
    assert_eq!(
        state(Some("2.9.9 (Claude Code)")),
        CapabilityState::Verified
    );
    assert_eq!(state(Some("3.0.0 (Claude Code)")), CapabilityState::Unknown);
    assert_eq!(state(None), CapabilityState::Verified);
}

#[path = "facts_refusal_tests.rs"]
mod refusals;
