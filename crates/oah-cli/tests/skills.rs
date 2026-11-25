// oah skills review 시험: 실제 oah 를 돌려 요청 전에 끝나는 사용법 오류·지원하지 않는 출처를 보고, 환경의 토큰이 출력과 상태에 남지 않음을 확인한다.
//! `oah skills review` through the built binary, for what ends before any request: usage errors, a source the
//! review does not accept, and a token in the environment that never reaches the output or the state. Reviews
//! over a recording transport run in the command's unit tests.

use std::path::Path;
use std::process::{Command, Output};
use std::{env, fs};

const OAH_BIN: &str = env!("CARGO_BIN_EXE_oah");
const CREDENTIAL: &str = "not-a-real-credential";

fn oah(home: &Path, args: &[&str]) -> Output {
    oah_from(home, home, args)
}

fn oah_from(home: &Path, cwd: &Path, args: &[&str]) -> Output {
    let mut command = Command::new(OAH_BIN);
    command
        .env_clear()
        .env("HOME", home)
        .env("USERPROFILE", home)
        .env("OAH_HOME", home.join("oah"))
        .env("GITHUB_TOKEN", CREDENTIAL)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CEILING_DIRECTORIES", home.parent().unwrap())
        .current_dir(cwd)
        .args(args);
    for name in ["PATH", "SYSTEMROOT"] {
        if let Some(value) = env::var_os(name) {
            command.env(name, value);
        }
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

#[test]
fn list_reports_project_and_user_copies_from_a_plain_working_folder() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let cwd = home.join("project");
    let args = ["skills", "list", "--json"];
    let catalog = cwd.join("skills/catalog-only");
    fs::create_dir_all(&catalog).unwrap();
    fs::write(catalog.join("SKILL.md"), b"catalog entry").unwrap();

    let empty = oah_from(&home, &cwd, &args);
    assert_eq!(empty.status.code(), Some(0), "{}", text(&empty.stderr));
    assert_eq!(text(&empty.stdout).trim(), "[]");

    for path in [
        cwd.join(".claude/skills/tidy"),
        home.join(".agents/skills/tidy"),
        home.join(".codex/skills/legacy"),
        home.join(".config/opencode/skills/open"),
        home.join(".gemini/config/skills/gravity"),
    ] {
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("SKILL.md"), [0xff]).unwrap();
    }

    let out = oah_from(&home, &cwd, &args);
    assert_eq!(out.status.code(), Some(0), "{}", text(&out.stderr));
    let actual: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let path = |parts: &[&str]| parts.iter().collect::<std::path::PathBuf>();
    assert_eq!(
        actual,
        serde_json::json!([
            {"name": "tidy", "engine": "claude", "compatible_agents": ["opencode", "copilot", "cursor"], "scope": "project", "location": path(&["~", "project", ".claude", "skills", "tidy"])},
            {"name": "tidy", "engine": "codex", "compatible_agents": ["opencode", "gemini", "copilot", "cursor"], "scope": "user", "location": path(&["~", ".agents", "skills", "tidy"])},
            {"name": "legacy", "engine": "codex", "compatible_agents": ["cursor"], "scope": "user", "location": path(&["~", ".codex", "skills", "legacy"])},
            {"name": "open", "engine": "opencode", "scope": "user", "location": path(&["~", ".config", "opencode", "skills", "open"])},
            {"name": "gravity", "engine": "antigravity", "scope": "user", "location": path(&["~", ".gemini", "config", "skills", "gravity"])}
        ])
    );
    let out = oah_from(&home, &cwd, &["skills", "list"]);
    assert_eq!(out.status.code(), Some(0), "{}", text(&out.stderr));
    let summary = text(&out.stdout);
    assert_eq!(summary.lines().count(), 5);
    assert!(summary.contains("[also: opencode, copilot, cursor]"));
    assert!(summary.contains("open (opencode, user)"));
    assert!(summary.contains("gravity (antigravity, user)"));
    assert!(!home.join("oah").exists());
}

#[test]
fn list_keeps_valid_skills_when_an_optional_path_is_a_file() {
    for file in [
        "project/.opencode",
        "project/.opencode/skills",
        ".cursor",
        ".cursor/skills",
        ".config",
    ] {
        let home = tempfile::tempdir().unwrap();
        let cwd = home.path().join("project");
        fs::create_dir(&cwd).unwrap();
        for skill in [".agents/skills/keep", ".gemini/config/skills/seen"] {
            let folder = home.path().join(skill);
            fs::create_dir_all(&folder).unwrap();
            fs::write(folder.join("SKILL.md"), b"# Skill").unwrap();
        }
        let blocked = home.path().join(file);
        fs::create_dir_all(blocked.parent().unwrap()).unwrap();
        fs::write(blocked, b"regular file").unwrap();

        let out = oah_from(home.path(), &cwd, &["skills", "list", "--json"]);
        assert_eq!(out.status.code(), Some(0), "{file}: {}", text(&out.stdout));
        let rows: Vec<serde_json::Value> = serde_json::from_slice(&out.stdout).unwrap();
        let names: Vec<_> = rows.iter().map(|row| row["name"].as_str()).collect();
        assert_eq!(names, [Some("keep"), Some("seen")], "{file}");
    }
}

#[cfg(any(unix, windows))]
#[test]
fn list_uses_only_user_scope_when_the_working_folder_resolves_to_home() {
    use oah_core::fsx::links::{NativeLinker, SystemLinker};

    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let alias = root.path().join("home-alias");
    for folder in [".claude", ".agents"] {
        let skill = home.join(folder).join("skills/tidy");
        fs::create_dir_all(&skill).unwrap();
        fs::write(skill.join("SKILL.md"), [0xff]).unwrap();
    }
    SystemLinker.link(&home, &alias, &home).unwrap();

    for configured_home in [&home, &alias] {
        let out = oah_from(configured_home, &home, &["skills", "list", "--json"]);
        assert_eq!(out.status.code(), Some(0), "{}", text(&out.stderr));
        let actual: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        let path = |engine| Path::new("~").join(engine).join("skills").join("tidy");
        assert_eq!(
            actual,
            serde_json::json!([
                {"name": "tidy", "engine": "claude", "compatible_agents": ["opencode", "cursor"], "scope": "user", "location": path(".claude")},
                {"name": "tidy", "engine": "codex", "compatible_agents": ["opencode", "gemini", "copilot", "cursor"], "scope": "user", "location": path(".agents")},
            ])
        );
    }
    assert!(!home.join("oah").exists());
}
