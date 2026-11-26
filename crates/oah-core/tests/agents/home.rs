// 가짜 홈 시험: 가짜 실행 파일과 가짜 홈으로 일곱 에이전트를 실제 실행 통로로 확인하고, 모든 바이트가 그대로이며 상태 문구가 남지 않는지 본다.
use crate::fake_agents::STATUS_SENTINEL;
use crate::machine::{Machine, probe, tree};
use crate::session::req;
use oah_core::agents::Status;
use oah_core::app;
use oah_core::config::AgentId;
use oah_core::dto::{AgentsProbeReq, Empty, SignInDto};
use oah_core::tasks::{CancelToken, NoProgress};
use std::fs;
use std::path::Path;

#[test]
fn probing_every_agent_changes_no_byte_and_keeps_no_status_text() {
    let machine = Machine::new();
    let claude = "case \"$1\" in\n--version) echo '2.1.318 (Claude Code)' ;;\nauth) echo '{\"loggedIn\": true, \"account\": \"SENTINEL\"}'; echo SENTINEL >&2 ;;\n*) exit 64 ;;\nesac\n";
    machine.script(
        &machine.bin,
        "claude",
        &claude.replace("SENTINEL", STATUS_SENTINEL),
    );
    let codex = "case \"$1\" in\n--version) echo 'codex-cli 0.163.0' ;;\nlogin) echo SENTINEL; exit 1 ;;\n*) exit 64 ;;\nesac\n";
    machine.script(
        &machine.bin,
        "codex",
        &codex.replace("SENTINEL", STATUS_SENTINEL),
    );
    let agent = machine.tripwire(&machine.bin, "agent");
    for skills in [
        ".gemini/skills/demo",
        ".agents/skills/demo",
        ".copilot/skills/demo",
    ] {
        fs::create_dir_all(machine.home.join(skills)).unwrap();
        fs::write(
            machine.home.join(skills).join("SKILL.md"),
            "---\nname: demo\n---\n",
        )
        .unwrap();
    }
    let before = tree(&machine.root);
    let session = machine.session();
    let rows = probe(
        &machine,
        &session,
        AgentsProbeReq {
            agents: None,
            sign_in: true,
            focus: false,
            opencode_details: None,
        },
    );
    assert_eq!(
        tree(&machine.root),
        before,
        "no folder, link or file was created or changed"
    );
    let status = |id: AgentId| {
        rows.iter()
            .find(|row| row.id.0 == id)
            .map(|row| (row.status, row.sign_in))
            .unwrap()
    };
    assert_eq!(status(AgentId::Claude), (Status::Ok, SignInDto::SignedIn));
    assert_eq!(
        status(AgentId::Codex),
        (Status::NotSignedIn, SignInDto::SignedOut)
    );
    for id in [
        AgentId::Gemini,
        AgentId::Copilot,
        AgentId::Opencode,
        AgentId::Antigravity,
        AgentId::Cursor,
    ] {
        assert_eq!(
            status(id),
            (Status::Missing, SignInDto::Unknown),
            "{id}: a skills folder is not an installation"
        );
    }
    assert!(
        !agent.exists(),
        "the agent command is never run for Cursor (N-7)"
    );
    let cancel = CancelToken::new();
    let listed = app::agents_list(&session.ctx(&cancel, &NoProgress), Empty {}).unwrap();
    let shown = format!(
        "{} {rows:?} {listed:?}",
        serde_json::to_string(&listed).unwrap()
    );
    assert!(!shown.contains(STATUS_SENTINEL), "{shown}");
    assert!(rows.iter().all(|row| row.problem.is_none()));
}

#[test]
fn antigravity_at_its_documented_folder_is_found_without_being_run() {
    let machine = Machine::new();
    let folder = machine.home.join(".local").join("bin");
    fs::create_dir_all(&folder).unwrap();
    let marker = machine.tripwire(&folder, "agy");
    let rows = probe(
        &machine,
        &machine.session(),
        req(&[AgentId::Antigravity], true, false),
    );
    assert_eq!(
        (
            rows[0].status,
            rows[0].installed,
            rows[0].version.as_deref()
        ),
        (Status::SignInUnknown, true, None)
    );
    assert_eq!(
        rows[0].source.as_deref(),
        Some(Path::new("~").join(".local/bin/agy").to_str().unwrap())
    );
    assert!(!marker.exists());
}

#[test]
fn a_refresh_re_probes_the_agents_the_session_holds_and_no_other() {
    let machine = Machine::new();
    let version = machine.root.join("version.txt");
    fs::write(&version, "2.1.318 (Claude Code)\n").unwrap();
    let body = format!(
        "case \"$1\" in\n--version) /bin/cat '{}' ;;\nauth) echo '{{}}' ;;\nesac\n",
        version.display()
    );
    machine.script(&machine.bin, "claude", &body);
    let codex = machine.tripwire(&machine.bin, "codex");
    let session = machine.session();
    let cancel = CancelToken::new();
    app::refresh(&session.ctx(&cancel, &NoProgress), Empty {}).unwrap();
    assert!(
        !codex.exists(),
        "before the first probe a refresh starts no agent program"
    );
    probe(&machine, &session, req(&[AgentId::Claude], false, false));
    fs::write(&version, "2.1.400 (Claude Code)\n").unwrap();
    app::refresh(&session.ctx(&cancel, &NoProgress), Empty {}).unwrap();
    let listed = app::agents_list(&session.ctx(&cancel, &NoProgress), Empty {}).unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].version.as_deref(), Some("2.1.400 (Claude Code)"));
    assert_eq!(
        (listed[0].status, listed[0].sign_in),
        (Status::Ok, SignInDto::SignedIn),
        "the refresh checked sign-in"
    );
    assert!(!codex.exists());
}

#[test]
fn failing_and_unidentified_programs_are_errors_that_change_no_byte() {
    let machine = Machine::new();
    machine.script(&machine.bin, "gemini", "echo 'gemini 0.1.0'\nexit 3\n");
    machine.script(&machine.bin, "copilot", "exit 1\n");
    machine.script(&machine.bin, "codex", "echo 'another-tool 1.0'\n");
    let before = tree(&machine.root);
    let rows = probe(
        &machine,
        &machine.session(),
        AgentsProbeReq {
            agents: None,
            sign_in: true,
            focus: false,
            opencode_details: None,
        },
    );
    assert_eq!(
        tree(&machine.root),
        before,
        "an error creates no folder, link or file"
    );
    for (id, code) in [
        (AgentId::Gemini, "OAH_AGENT_PROBE_FAILED"),
        (AgentId::Copilot, "OAH_AGENT_PROBE_FAILED"),
        (AgentId::Codex, "OAH_AGENT_UNIDENTIFIED"),
    ] {
        let row = rows.iter().find(|row| row.id.0 == id).unwrap();
        let problem = row.problem.as_ref().map(|problem| problem.code.as_str());
        assert_eq!(
            (row.status, row.installed, problem),
            (Status::Error, false, Some(code)),
            "{id}"
        );
    }
}
