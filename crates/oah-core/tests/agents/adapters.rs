// 로그인 판정 시험: 미리 적은 찾기·실행 결과로 Claude Code·Codex 의 로그인·상태 명령 실패·설정 명령과 상태 문구가 남지 않음을 본다.
use crate::fake_agents::{FakeProbe, STATUS_SENTINEL};
use oah_core::agents::{
    Check, Failure, Os, RunError, SignIn, SignInCheck, Status, adapter, check_sign_in, detect,
    installed, profile,
};
use oah_core::config::AgentId;
use oah_core::error::Coded;
use std::time::Duration;

const CLAUDE_VERSION: &str = "2.1.318 (Claude Code)\n";

pub(crate) fn probed(id: AgentId, probe: &FakeProbe, command: Option<&[String]>) -> Check {
    let detection = detect(id, probe, command);
    let sign_in = check_sign_in(id, probe, command, &detection);
    Check {
        detection,
        sign_in,
        opencode: None,
    }
}

fn claude(status_exit: Option<i32>, status_stdout: &str) -> FakeProbe {
    FakeProbe::new(Os::Macos)
        .program("claude", "/bin/claude")
        .run("/bin/claude --version", Some(0), CLAUDE_VERSION, "")
        .run(
            "/bin/claude auth status",
            status_exit,
            status_stdout,
            STATUS_SENTINEL,
        )
}

#[test]
fn claude_code_signed_in_is_ok_with_its_version_line() {
    let probe = claude(Some(0), "{\"loggedIn\": true}");
    let check = probed(AgentId::Claude, &probe, None);
    assert_eq!(check.status(), Status::Ok);
    assert_eq!(check.sign_in, SignInCheck::Checked(SignIn::SignedIn));
    assert_eq!(
        check.detection.version.as_deref(),
        Some("2.1.318 (Claude Code)")
    );
    assert_eq!(check.detection.source.as_deref(), Some("/bin/claude"));
    assert_eq!(
        probe.ran(),
        ["/bin/claude --version", "/bin/claude auth status"]
    );
}

#[test]
fn a_false_logged_in_flag_or_a_failing_status_command_means_signed_out() {
    for (exit, stdout) in [
        (Some(0), "{\"loggedIn\": false}"),
        (Some(1), ""),
        (None, ""),
    ] {
        let check = probed(AgentId::Claude, &claude(exit, stdout), None);
        assert_eq!(check.status(), Status::NotSignedIn, "{exit:?} {stdout}");
        let login = profile(AgentId::Claude).login;
        assert_eq!(
            check.sign_in,
            SignInCheck::Checked(SignIn::SignedOut { login })
        );
        assert_eq!((login.text, login.is_command), ("claude auth login", true));
    }
}

#[test]
fn codex_reads_only_the_exit_code_of_login_status() {
    let probe = FakeProbe::new(Os::Windows)
        .program("codex", "/bin/codex")
        .run("/bin/codex --version", Some(0), "codex-cli 0.163.0\n", "")
        .run("/bin/codex login status", Some(1), STATUS_SENTINEL, "");
    let check = probed(AgentId::Codex, &probe, None);
    assert_eq!(check.status(), Status::NotSignedIn);
    assert!(installed(&check.detection).is_some());
}

#[test]
fn a_status_command_that_cannot_run_is_an_error_of_an_installed_agent() {
    let probe = FakeProbe::new(Os::Macos)
        .program("claude", "/bin/claude")
        .run("/bin/claude --version", Some(0), CLAUDE_VERSION, "")
        .run_fails("/bin/claude auth status", RunError::TimedOut);
    let check = probed(AgentId::Claude, &probe, None);
    assert_eq!(check.status(), Status::Error);
    let problem = check.problem().unwrap();
    assert_eq!(problem.code(), "OAH_AGENT_PROBE_FAILED");
    assert_eq!(
        problem.message(),
        "'claude auth status' timed out after 10s"
    );
    assert!(
        installed(&check.detection).is_some(),
        "the program was found and identified"
    );
}

#[test]
fn a_configured_command_replaces_the_path_lookup() {
    let command = vec![String::from("node"), String::from("/opt/claude/cli.js")];
    let probe = FakeProbe::new(Os::Macos)
        .program("node", "/bin/node")
        .run(
            "/bin/node /opt/claude/cli.js --version",
            Some(0),
            CLAUDE_VERSION,
            "",
        )
        .run(
            "/bin/node /opt/claude/cli.js auth status",
            Some(0),
            "{}",
            "",
        );
    let check = probed(AgentId::Claude, &probe, Some(&command));
    assert_eq!(check.status(), Status::Ok);
    assert!(!probe.asked().contains(&String::from("resolve claude")));
}

#[test]
fn no_status_text_survives_in_any_value() {
    for (exit, stdout) in [
        (
            Some(0),
            format!("{{\"loggedIn\": false, \"note\": \"{STATUS_SENTINEL}\"}}"),
        ),
        (Some(7), STATUS_SENTINEL.to_string()),
    ] {
        let check = probed(AgentId::Claude, &claude(exit, &stdout), None);
        let shown = format!("{check:?} {:?}", check.problem().map(Coded::message));
        assert!(!shown.contains(STATUS_SENTINEL), "{shown}");
    }
    let deadline = Duration::from_secs(10);
    assert_eq!(
        Failure::TimedOut(deadline),
        Failure::TimedOut(Duration::from_millis(10_000))
    );
}

#[test]
fn an_unchecked_or_unknown_sign_in_of_a_usable_agent_is_sign_in_unknown() {
    let probe = claude(Some(0), "{}");
    let detection = detect(AgentId::Claude, &probe, None);
    assert_eq!(detection.status, Status::Ok);
    for sign_in in [
        SignInCheck::NotChecked,
        SignInCheck::Checked(SignIn::Unknown),
    ] {
        let check = Check {
            detection: detection.clone(),
            sign_in,
            opencode: None,
        };
        assert_eq!(check.status(), Status::SignInUnknown);
    }
    assert_eq!(
        probe.ran(),
        ["/bin/claude --version"],
        "detection alone runs no status command"
    );
}

#[test]
fn an_adapter_checks_sign_in_only_for_a_usable_detection() {
    let probe = claude(Some(0), "{}");
    let mut detection = detect(AgentId::Claude, &probe, None);
    detection.status = Status::TooOld;
    assert_eq!(
        adapter(AgentId::Claude).sign_in(&probe, &detection),
        Ok(SignIn::Unknown)
    );
    assert_eq!(probe.ran(), ["/bin/claude --version"]);
}
