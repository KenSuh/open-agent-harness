// 세션 에이전트 작업 시험: 목록·확인·한 에이전트 실패의 격리·포커스 60초 제한·작업 키를 가짜 확인 통로로 본다.
use crate::fake_agents::{FakeProbe, STATUS_SENTINEL};
use oah_core::agents::{Os, Probe, RunError, Status};
use oah_core::app::{self, SIGN_IN_INTERVAL, Session};
use oah_core::config::AgentId;
use oah_core::dto::{AgentIdDto, AgentStatusDto, AgentsProbeReq, Empty, SignInDto};
use oah_core::paths::Env;
use oah_core::tasks::{CancelToken, NoProgress, Progress, ProgressSink, TaskEvent, TaskRegistry};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub(crate) const CLAUDE_VERSION: &str = "2.1.318 (Claude Code)\n";

pub(crate) fn session() -> Session {
    Session::new(Env::new(std::env::temp_dir()).unwrap())
}

pub(crate) fn req(agents: &[AgentId], sign_in: bool, focus: bool) -> AgentsProbeReq {
    let agents = (!agents.is_empty()).then(|| agents.iter().copied().map(AgentIdDto).collect());
    AgentsProbeReq {
        agents,
        sign_in,
        focus,
    }
}

pub(crate) fn probe(
    session: &Session,
    req: AgentsProbeReq,
    probe: &dyn Probe,
) -> Vec<AgentStatusDto> {
    let cancel = CancelToken::new();
    app::agents_probe_with(&session.ctx(&cancel, &NoProgress), req, probe).unwrap()
}

pub(crate) fn list(session: &Session) -> Vec<AgentStatusDto> {
    let cancel = CancelToken::new();
    app::agents_list(&session.ctx(&cancel, &NoProgress), Empty {}).unwrap()
}

pub(crate) fn claude(version: &str, logged_in: bool) -> FakeProbe {
    let status = format!("{{\"loggedIn\": {logged_in}}}");
    FakeProbe::new(Os::Macos)
        .program("claude", "/bin/claude")
        .run("/bin/claude --version", Some(0), version, "")
        .run("/bin/claude auth status", Some(0), &status, STATUS_SENTINEL)
}

fn row(rows: &[AgentStatusDto], id: AgentId) -> &AgentStatusDto {
    rows.iter().find(|row| row.id.0 == id).unwrap()
}

const OPENCODE_VERSION: &str = "/bin/opencode --version";
const OPENCODE_AUTH: &str = "/bin/opencode auth list --format json --log-level error";
const OPENCODE_MODEL: &str = "/bin/opencode api GET /api/model/default --log-level error";

fn opencode(version: &str) -> FakeProbe {
    FakeProbe::new(Os::Macos)
        .program("opencode", "/bin/opencode")
        .run(OPENCODE_VERSION, Some(0), version, "")
}

#[test]
fn opencode_reports_connection_counts_and_the_default_model_without_provider_metadata() {
    let session = session();
    let machine = opencode("opencode v2.4.1")
        .run(
            OPENCODE_AUTH,
            Some(0),
            &serde_json::json!([
                {"connections": [{"label": STATUS_SENTINEL}, {"id": STATUS_SENTINEL}]},
                {"connections": [{"id": STATUS_SENTINEL}]}
            ])
            .to_string(),
            STATUS_SENTINEL,
        )
        .run(
            OPENCODE_MODEL,
            Some(0),
            &serde_json::json!({
                "data": {
                    "modelID": "a-model", "providerID": "a-provider", "headers": {"token": STATUS_SENTINEL}
                }
            })
            .to_string(),
            "",
        );
    let id = AgentId::Opencode;
    let rows = probe(&session, req(&[id], true, false), &machine);
    let open = row(&rows, id);
    assert_eq!(open.status, Status::SignInUnknown);
    assert_eq!(open.sign_in, SignInDto::Unknown);
    let json = serde_json::to_string(&open).unwrap();
    assert!(!json.contains(STATUS_SENTINEL));
    let report: serde_json::Value = serde_json::from_str(&json).unwrap();
    assert_eq!(
        report["opencode"],
        serde_json::json!({"saved_connections": 3, "default_model": "a-provider/a-model"})
    );
    assert_eq!(
        machine.ran(),
        [OPENCODE_VERSION, OPENCODE_AUTH, OPENCODE_MODEL]
    );

    let version_only = opencode("opencode v2.4.1");
    let focused = probe(&session, req(&[id], true, true), &version_only);
    let cached = row(&focused, id);
    assert_eq!(cached.opencode, open.opencode);
    assert_eq!(version_only.ran(), [OPENCODE_VERSION]);
}

#[test]
fn opencode_keeps_install_detection_when_status_calls_fail_and_skips_older_versions() {
    let id = AgentId::Opencode;
    let session = session();
    let empty = opencode("opencode v2.4.1")
        .run(OPENCODE_AUTH, Some(0), "[]", "")
        .run_fails(OPENCODE_MODEL, RunError::TimedOut);
    let rows = probe(&session, req(&[id], true, false), &empty);
    let details = row(&rows, id).opencode.as_ref().unwrap();
    assert_eq!(details.saved_connections, Some(0));
    assert_eq!(details.default_model, None);

    let failed = opencode("opencode v2.4.1")
        .run(OPENCODE_AUTH, Some(0), "[{\"credentials\":[]}]", "")
        .run(
            OPENCODE_MODEL,
            Some(1),
            r#"{"data":{"modelID":"a-model","providerID":"a-provider"}}"#,
            "",
        );
    let rows = probe(&session, req(&[id], true, false), &failed);
    let open = row(&rows, id);
    assert!(open.installed);
    assert_eq!(open.sign_in, SignInDto::Unknown);
    let details = open.opencode.as_ref().unwrap();
    assert_eq!(details.saved_connections, None);
    assert_eq!(details.default_model, None);

    let legacy = opencode("opencode v1.2.3");
    let rows = probe(&session, req(&[id], true, false), &legacy);
    assert!(row(&rows, id).installed);
    assert_eq!(row(&rows, id).opencode, None);
    assert_eq!(legacy.ran(), [OPENCODE_VERSION]);
}

#[test]
fn the_list_is_empty_before_the_first_probe_and_then_holds_each_probed_agent() {
    let session = session();
    assert!(list(&session).is_empty());
    let rows = probe(
        &session,
        req(&[AgentId::Claude], true, false),
        &claude(CLAUDE_VERSION, true),
    );
    assert_eq!(rows.len(), 1);
    assert_eq!(
        (rows[0].status, rows[0].sign_in, rows[0].installed),
        (Status::Ok, SignInDto::SignedIn, true)
    );
    assert_eq!(list(&session), rows);
    probe(
        &session,
        req(&[AgentId::Codex], false, false),
        &FakeProbe::new(Os::Macos),
    );
    let held: Vec<AgentId> = list(&session).iter().map(|row| row.id.0).collect();
    assert_eq!(held, [AgentId::Claude, AgentId::Codex], "display order");
}

#[test]
fn a_focus_probe_inside_a_minute_keeps_the_last_sign_in_without_running_the_status_command() {
    let session = session();
    probe(
        &session,
        req(&[AgentId::Claude], true, false),
        &claude(CLAUDE_VERSION, true),
    );
    let again = claude(CLAUDE_VERSION, false);
    let rows = probe(&session, req(&[AgentId::Claude], true, true), &again);
    assert_eq!(
        again.ran(),
        ["/bin/claude --version"],
        "detection re-runs at once; sign-in waits"
    );
    assert_eq!(
        (rows[0].status, rows[0].sign_in),
        (Status::Ok, SignInDto::SignedIn)
    );
    let rescan = claude(CLAUDE_VERSION, false);
    let rows = probe(&session, req(&[AgentId::Claude], true, false), &rescan);
    assert_eq!(
        rescan.ran(),
        ["/bin/claude --version", "/bin/claude auth status"],
        "a rescan checks again"
    );
    assert_eq!(
        (rows[0].status, rows[0].sign_in),
        (Status::NotSignedIn, SignInDto::SignedOut)
    );
}

#[test]
fn sign_in_is_due_once_sixty_seconds_have_passed_since_the_last_check() {
    let now = Instant::now();
    assert_eq!(SIGN_IN_INTERVAL, Duration::from_secs(60));
    assert!(app::sign_in_due(None, now));
    assert!(!app::sign_in_due(Some(now), now + Duration::from_secs(59)));
    assert!(app::sign_in_due(Some(now), now + Duration::from_secs(60)));
    assert!(
        !app::sign_in_due(Some(now + Duration::from_secs(1)), now),
        "a clock step back is not due"
    );
}

#[test]
fn a_detection_only_probe_keeps_the_sign_in_of_an_unchanged_detection_only() {
    let session = session();
    probe(
        &session,
        req(&[AgentId::Claude], true, false),
        &claude(CLAUDE_VERSION, true),
    );
    let same = probe(
        &session,
        req(&[AgentId::Claude], false, false),
        &claude(CLAUDE_VERSION, false),
    );
    assert_eq!(
        (same[0].status, same[0].sign_in),
        (Status::Ok, SignInDto::SignedIn)
    );
    let newer = probe(
        &session,
        req(&[AgentId::Claude], false, false),
        &claude("2.2.0 (Claude Code)", true),
    );
    assert_eq!(
        (newer[0].status, newer[0].sign_in),
        (Status::SignInUnknown, SignInDto::Unknown)
    );
}

#[test]
fn one_agents_failure_never_fails_the_others() {
    let probe_all = FakeProbe::new(Os::Macos)
        .program("claude", "/bin/claude")
        .run_fails("/bin/claude --version", RunError::TimedOut)
        .program("codex", "/bin/codex")
        .run("/bin/codex --version", Some(0), "codex-cli 0.163.0\n", "")
        .run("/bin/codex login status", Some(0), STATUS_SENTINEL, "");
    let rows = probe(
        &session(),
        AgentsProbeReq {
            agents: None,
            sign_in: true,
            focus: false,
        },
        &probe_all,
    );
    let ids: Vec<AgentId> = rows.iter().map(|row| row.id.0).collect();
    assert_eq!(ids, AgentId::ALL);
    let claude = row(&rows, AgentId::Claude);
    assert_eq!((claude.status, claude.installed), (Status::Error, false));
    assert_eq!(
        claude.problem.as_ref().map(|problem| problem.code.as_str()),
        Some("OAH_AGENT_PROBE_FAILED")
    );
    assert_eq!(
        (
            row(&rows, AgentId::Codex).status,
            row(&rows, AgentId::Codex).installed
        ),
        (Status::Ok, true)
    );
    for id in [
        AgentId::Gemini,
        AgentId::Copilot,
        AgentId::Opencode,
        AgentId::Antigravity,
        AgentId::Cursor,
    ] {
        assert_eq!(row(&rows, id).status, Status::Missing, "{id}");
    }
}

#[test]
fn the_same_request_is_one_task_and_another_request_runs_beside_it() {
    let registry = TaskRegistry::new();
    let listener = || Arc::new(|_: &TaskEvent<Vec<AgentStatusDto>>| {});
    let first = app::agents_probe_key(&req(&[AgentId::Codex, AgentId::Claude], true, false));
    let begin = registry.begin(first, listener()).unwrap();
    assert!(matches!(begin, oah_core::tasks::Begin::Started(_)));
    let same = app::agents_probe_key(&req(&[AgentId::Claude, AgentId::Codex], true, false));
    let busy = registry.begin(same, listener()).err().unwrap();
    assert_eq!(oah_core::Coded::code(&busy), "OAH_TASK_BUSY");
    let focus = app::agents_probe_key(&req(&[AgentId::Claude, AgentId::Codex], true, true));
    assert!(registry.begin(focus, listener()).is_ok());
}

/// Keeps every progress report.
#[derive(Default)]
struct Recorded(Mutex<Vec<Progress>>);

impl ProgressSink for Recorded {
    fn progress(&self, progress: Progress) {
        self.0.lock().unwrap().push(progress);
    }
}

#[test]
fn a_probe_reports_its_progress_once_per_finished_agent() {
    let (session, cancel, recorded) = (session(), CancelToken::new(), Recorded::default());
    let ctx = session.ctx(&cancel, &recorded);
    let two = req(&[AgentId::Claude, AgentId::Gemini], false, false);
    app::agents_probe_with(&ctx, two, &claude(CLAUDE_VERSION, true)).unwrap();
    let steps: Vec<(&str, u32, Option<u32>)> = recorded
        .0
        .lock()
        .unwrap()
        .iter()
        .map(|step| (step.phase, step.done, step.total))
        .collect();
    let phase = "probe-agents";
    assert_eq!(
        steps,
        [
            (phase, 0, Some(2)),
            (phase, 1, Some(2)),
            (phase, 2, Some(2))
        ]
    );
}
