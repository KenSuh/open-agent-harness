// 에이전트 DTO 표본 시험: 고정한 상태·요청·알림을 직렬화해 tests/dto_samples/agents 의 JSON 과 바이트 단위로 맞추고 확인 시각 문자열을 본다.
use crate::fake_agents::{FakeProbe, STATUS_SENTINEL};
use oah_core::agents::{Check, Os, Probe, RunError, check_sign_in, detect};
use oah_core::config::AgentId;
use oah_core::dto::{AgentIdDto, AgentStatusDto, AgentsChangedDto, AgentsProbeReq, rfc3339_utc};
use serde::Serialize;
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// 2026-11-18T06:00:00Z.
const CHECKED: u64 = 1_794_981_600;

fn sample(name: &str) -> PathBuf {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    root.join("tests")
        .join("dto_samples")
        .join("agents")
        .join(format!("{name}.json"))
}

/// The pretty JSON of `value`, with a final newline, must be the sample file byte for byte.
fn assert_sample(name: &str, value: &impl Serialize) {
    let actual = serde_json::to_string_pretty(value).unwrap() + "\n";
    let path = sample(name);
    let expected = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("{}: {error}\nactual:\n{actual}", path.display()));
    assert_eq!(actual, expected, "{} differs", path.display());
}

fn status(id: AgentId, probe: &FakeProbe) -> AgentStatusDto {
    let detection = detect(id, probe, None);
    let sign_in = check_sign_in(id, probe, None, &detection);
    let at = UNIX_EPOCH + Duration::from_secs(CHECKED);
    AgentStatusDto::new(
        Probe::env(probe),
        &Check {
            detection,
            sign_in,
            opencode: None,
        },
        probe.os(),
        at,
    )
}

#[test]
fn agent_statuses_match_their_samples() {
    let codex = FakeProbe::new(Os::Macos)
        .program("codex", "/bin/codex")
        .run("/bin/codex --version", Some(0), "codex-cli 0.163.0\n", "")
        .run("/bin/codex login status", Some(1), STATUS_SENTINEL, "");
    assert_sample("status-codex-signed-out", &status(AgentId::Codex, &codex));
    let claude = FakeProbe::new(Os::Macos)
        .program("claude", "/bin/claude")
        .run(
            "/bin/claude --version",
            Some(0),
            "2.1.318 (Claude Code)\n",
            "",
        )
        .run_fails("/bin/claude auth status", RunError::TimedOut);
    assert_sample("status-claude-error", &status(AgentId::Claude, &claude));
    assert_sample(
        "status-cursor-missing",
        &status(AgentId::Cursor, &FakeProbe::new(Os::Windows)),
    );
    let copilot = FakeProbe::new(Os::Macos)
        .program("copilot", "/bin/copilot")
        .run("/bin/copilot version", Some(0), "1.0.2\n", "");
    assert_sample("status-copilot-docs", &status(AgentId::Copilot, &copilot));
}

#[test]
fn the_probe_request_and_the_changed_event_match_their_samples() {
    let ids = vec![AgentIdDto(AgentId::Claude), AgentIdDto(AgentId::Codex)];
    let req = AgentsProbeReq {
        agents: Some(ids.clone()),
        sign_in: true,
        focus: false,
        opencode_details: None,
    };
    let text = std::fs::read_to_string(sample("probe-request")).unwrap();
    assert_eq!(serde_json::from_str::<AgentsProbeReq>(&text).unwrap(), req);
    assert_sample("probe-request", &req);
    assert_sample("agents-changed", &AgentsChangedDto { agents: ids });
}

#[test]
fn a_probe_request_may_leave_every_field_out_and_names_only_known_agents() {
    assert_eq!(
        serde_json::from_str::<AgentsProbeReq>("{}").unwrap(),
        AgentsProbeReq::default()
    );
    let unknown =
        serde_json::from_str::<AgentsProbeReq>("{\"agents\": [\"someone\"]}").unwrap_err();
    assert!(
        unknown.to_string().contains("unknown agent someone"),
        "{unknown}"
    );
    assert!(
        serde_json::from_str::<AgentsProbeReq>("{\"agent\": []}").is_err(),
        "unknown fields are refused"
    );
}

#[test]
fn checked_at_is_rfc3339_in_utc() {
    let at = |seconds: u64| rfc3339_utc(UNIX_EPOCH + Duration::from_secs(seconds));
    assert_eq!(at(0), "1970-01-01T00:00:00Z");
    assert_eq!(at(951_782_400), "2000-02-29T00:00:00Z");
    assert_eq!(at(1_700_000_000), "2023-11-14T22:13:20Z");
    assert_eq!(at(CHECKED), "2026-11-18T06:00:00Z");
    assert_eq!(at(4_102_444_799), "2099-12-31T23:59:59Z");
    let before = SystemTime::UNIX_EPOCH - Duration::from_secs(1);
    assert_eq!(rfc3339_utc(before), "1970-01-01T00:00:00Z");
}
