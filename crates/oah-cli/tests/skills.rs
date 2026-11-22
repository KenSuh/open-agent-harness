// oah skills review 시험: 실제 oah 를 돌려 요청 전에 끝나는 사용법 오류·지원하지 않는 출처를 보고, 환경의 토큰이 출력과 상태에 남지 않음을 확인한다.
//! `oah skills review` through the built binary, for what ends before any request: usage errors, a source the
//! review does not accept, and a token in the environment that never reaches the output or the state. Reviews
//! over a recording transport run in the command's unit tests.

use std::path::Path;
use std::process::{Command, Output};

const OAH_BIN: &str = env!("CARGO_BIN_EXE_oah");
const CREDENTIAL: &str = "not-a-real-credential";

fn oah(home: &Path, args: &[&str]) -> Output {
    let mut command = Command::new(OAH_BIN);
    command
        .env_clear()
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("OAH_HOME", home.join("oah"))
        .env("GITHUB_TOKEN", CREDENTIAL)
        .current_dir(home)
        .args(args);
    if let Some(root) = std::env::var_os("SYSTEMROOT") {
        command.env("SYSTEMROOT", root);
    }
    command.output().expect("run oah")
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

#[test]
fn a_missing_option_or_subcommand_is_a_usage_error() {
    let home = tempfile::tempdir().expect("home");
    let executable = Path::new(OAH_BIN)
        .file_name()
        .expect("oah executable basename")
        .to_string_lossy();
    let usage = format!("Usage: {executable} skills ");
    for args in [
        &["skills"][..],
        &["skills", "review", "--github", "acme/skills"],
        &["skills", "review", "--path", "skills/tidy"],
        &[
            "skills",
            "review",
            "--github",
            "acme/skills",
            "--path",
            "skills/tidy",
            "--ref",
            "main",
        ],
    ] {
        let out = oah(home.path(), args);
        assert_eq!(out.status.code(), Some(2), "{args:?}");
        assert!(out.stdout.is_empty(), "{args:?}");
        let stderr = text(&out.stderr);
        assert!(
            stderr.lines().any(|line| line.starts_with(&usage)),
            "{args:?}: {stderr}"
        );
    }
    assert!(!home.path().join("oah").exists());
}

#[test]
fn a_source_the_review_does_not_accept_is_refused_before_any_request() {
    let home = tempfile::tempdir().expect("home");
    let out = oah(
        home.path(),
        &[
            "skills",
            "review",
            "--github",
            "not a repository",
            "--path",
            "skills/tidy",
            "--json",
        ],
    );
    assert_eq!(out.status.code(), Some(1));
    assert!(out.stderr.is_empty(), "{}", text(&out.stderr));
    let shown: serde_json::Value = serde_json::from_slice(&out.stdout).expect("JSON");
    assert_eq!(shown["error"]["code"], "OAH_SKILL_SOURCE_UNSUPPORTED");
    assert_eq!(
        shown["error"]["hint"],
        "Give a public GitHub repository as OWNER/REPO and the skill folder inside it."
    );

    let out = oah(
        home.path(),
        &[
            "skills",
            "review",
            "--github",
            "acme/skills",
            "--path",
            "../up",
        ],
    );
    assert_eq!(out.status.code(), Some(1));
    assert!(out.stdout.is_empty());
    assert!(
        text(&out.stderr).starts_with("OAH_SKILL_SOURCE_UNSUPPORTED: "),
        "{}",
        text(&out.stderr)
    );
    assert!(
        !home.path().join("oah").exists(),
        "a refusal writes no state"
    );
    for output in [text(&out.stdout), text(&out.stderr)] {
        assert!(!output.contains(CREDENTIAL));
    }
}

#[test]
fn help_names_the_review_options() {
    let home = tempfile::tempdir().expect("home");
    let out = oah(home.path(), &["skills", "review", "--help"]);
    assert_eq!(out.status.code(), Some(0));
    let help = text(&out.stdout);
    assert!(
        help.contains("--github <OWNER/REPO>")
            && help.contains("--path <FOLDER>")
            && help.contains("--json"),
        "{help}"
    );
}
