// 감지 판정 시험: 버전 실행 실패·정체 확인·찾지 못함·거절된 shim, 확인된 상태 명령이 없는 다섯 에이전트의 감지를 미리 적은 결과로 본다.
use crate::adapters::probed;
use crate::fake_agents::FakeProbe;
use oah_core::agents::{
    AgentError, Failure, Os, Resolution, RunError, SignInCheck, Status, detect, installed,
};
use oah_core::config::AgentId;
use oah_core::error::Coded;
use std::path::PathBuf;
use std::time::Duration;

#[test]
fn version_failures_are_errors_and_build_no_witness() {
    let cases = [
        (
            RunError::Launch(String::from("exec format error")),
            "'codex --version' could not launch: exec format error",
        ),
        (RunError::TimedOut, "'codex --version' timed out after 10s"),
    ];
    for (error, message) in cases {
        let probe = FakeProbe::new(Os::Macos)
            .program("codex", "/bin/codex")
            .run_fails("/bin/codex --version", error);
        let detection = detect(AgentId::Codex, &probe, None);
        assert_eq!(detection.status, Status::Error);
        assert_eq!(
            detection.problem.as_ref().map(Coded::message).as_deref(),
            Some(message)
        );
        assert!(installed(&detection).is_none());
    }
    let probe = FakeProbe::new(Os::Macos)
        .program("codex", "/bin/codex")
        .run("/bin/codex --version", Some(2), "codex-cli 0.1.0", "");
    let detection = detect(AgentId::Codex, &probe, None);
    let failure = AgentError::ProbeFailed {
        command: String::from("codex --version"),
        failure: Failure::Exit(Some(2)),
    };
    assert_eq!(detection.problem, Some(failure));
    assert_eq!(detection.version.as_deref(), Some("codex-cli 0.1.0"));
}

#[test]
fn a_name_owned_by_another_program_is_unidentified() {
    for (id, name, line) in [
        (AgentId::Claude, "claude", "agent 1.4.0 (other vendor)"),
        (AgentId::Codex, "codex", "codex 9.9.9"),
    ] {
        let program = format!("/bin/{name}");
        let probe = FakeProbe::new(Os::Macos).program(name, &program).run(
            &format!("{program} --version"),
            Some(0),
            line,
            "",
        );
        let check = probed(id, &probe, None);
        assert_eq!(check.status(), Status::Error);
        assert_eq!(
            check.problem().map(Coded::code),
            Some("OAH_AGENT_UNIDENTIFIED")
        );
        assert_eq!(
            check.detection.version, None,
            "a foreign program's output is not kept"
        );
        assert!(installed(&check.detection).is_none());
        assert_eq!(
            probe.ran().len(),
            1,
            "no sign-in check for an unidentified program"
        );
    }
}

#[test]
fn missing_and_unsupported_shim_run_nothing() {
    let probe = FakeProbe::new(Os::Windows);
    let detection = detect(AgentId::Codex, &probe, None);
    assert_eq!(
        (detection.status, detection.source),
        (Status::Missing, None)
    );
    let shim = Resolution::UnsupportedShim {
        path: PathBuf::from("/npm/codex.cmd"),
        reason: String::from("no package"),
    };
    let probe = FakeProbe::new(Os::Windows).resolves("codex", shim);
    let detection = detect(AgentId::Codex, &probe, None);
    assert_eq!(detection.status, Status::UnsupportedShim);
    assert_eq!(
        detection.problem.as_ref().map(Coded::code),
        Some("OAH_UNSUPPORTED_BATCH_SHIM")
    );
    assert!(installed(&detection).is_none());
    assert!(probe.ran().is_empty());
}

#[test]
fn agents_without_a_verified_status_command_are_sign_in_unknown() {
    let rows = [
        (AgentId::Gemini, "gemini", "--version"),
        (AgentId::Copilot, "copilot", "version"),
        (AgentId::Opencode, "opencode", "--version"),
    ];
    for (id, name, args) in rows {
        let program = format!("/bin/{name}");
        let probe = FakeProbe::new(Os::Macos).program(name, &program).run(
            &format!("{program} {args}"),
            Some(0),
            "1.2.3\n",
            "",
        );
        let check = probed(id, &probe, None);
        assert_eq!(check.status(), Status::SignInUnknown, "{name}");
        assert_eq!(check.sign_in, SignInCheck::NotChecked);
        assert!(installed(&check.detection).is_some());
        assert_eq!(probe.ran(), [format!("{program} {args}")]);
    }
}

#[test]
fn antigravity_is_found_on_path_or_at_its_documented_folder_without_running_it() {
    let probe = FakeProbe::new(Os::Macos).program("agy", "/opt/bin/agy");
    let detection = detect(AgentId::Antigravity, &probe, None);
    assert_eq!(
        (detection.status, detection.version),
        (Status::SignInUnknown, None)
    );
    let home = FakeProbe::new(Os::Macos).home();
    let at_default = home.join(".local").join("bin").join("agy");
    let probe = FakeProbe::new(Os::Macos).located(
        "agy",
        &home.join(".local").join("bin"),
        &at_default.to_string_lossy(),
    );
    let detection = detect(AgentId::Antigravity, &probe, None);
    let shown = PathBuf::from("~").join(".local").join("bin").join("agy");
    assert_eq!(detection.source.as_deref(), shown.to_str());
    let local = std::env::temp_dir().join("fake-agents").join("local");
    let windows = FakeProbe::new(Os::Windows)
        .env("LOCALAPPDATA", &local.to_string_lossy())
        .located(
            "agy",
            &local.join("agy").join("bin"),
            "/local/agy/bin/agy.exe",
        );
    assert_eq!(
        detect(AgentId::Antigravity, &windows, None).status,
        Status::SignInUnknown
    );
    assert!(probe.ran().is_empty() && windows.ran().is_empty());
    assert_eq!(
        detect(AgentId::Antigravity, &FakeProbe::new(Os::Macos), None).status,
        Status::Missing
    );
}

#[test]
fn cursor_is_installed_only_by_its_application_bundle_on_macos() {
    let probe = FakeProbe::new(Os::Macos)
        .bundle("/Applications/Cursor.app")
        .program("agent", "/bin/agent");
    let detection = detect(AgentId::Cursor, &probe, None);
    assert_eq!(detection.status, Status::SignInUnknown);
    assert_eq!(
        detection.source.as_deref(),
        Some("/Applications/Cursor.app")
    );
    assert_eq!(
        probe.asked(),
        ["exists /Applications/Cursor.app"],
        "the agent command is never consulted (N-7)"
    );
    assert_eq!(
        detect(AgentId::Cursor, &FakeProbe::new(Os::Macos), None).status,
        Status::Missing
    );
    let windows = FakeProbe::new(Os::Windows).bundle("/Applications/Cursor.app");
    assert_eq!(
        detect(AgentId::Cursor, &windows, None).status,
        Status::Missing
    );
    assert!(
        windows.asked().is_empty(),
        "no Windows location is documented (U-9)"
    );
}

#[test]
fn the_v0_4_doctor_accepts_a_version_exactly_at_the_floor() {
    let node = oah_core::agents::catalog()
        .into_iter()
        .find(|tool| tool.name == "node")
        .unwrap();
    for (line, status) in [("v16.0.0\n", Status::Ok), ("v15.9.9\n", Status::TooOld)] {
        let probe = FakeProbe::new(Os::Macos).program("node", "/bin/node").run(
            "/bin/node --version",
            Some(0),
            line,
            "",
        );
        let row =
            oah_core::agents::doctor::check_tool(&node, &probe, Duration::from_secs(10), false);
        assert_eq!(row.status, status, "{line}");
    }
}
