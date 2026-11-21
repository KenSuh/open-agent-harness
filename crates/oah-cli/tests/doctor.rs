// oah doctor·옛 hook 시험: 가짜 PATH·가짜 홈의 가짜 실행 파일로 실제 oah 를 돌려 JSON·글 출력, 종료 코드, 오류 모양을 본다(Cursor 는 보지 않는다).
#![cfg(unix)]

use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

/// A synthetic text the fake status commands print; it must never reach the output.
const SENTINEL: &str = "status-text-sentinel-7f3a";

/// A temporary root with `home`, `bin` (the only `PATH` folder) and `work` (the invocation folder).
struct Machine {
    _dir: tempfile::TempDir,
    root: PathBuf,
}

impl Machine {
    fn new(codex_login_exit: i32) -> Machine {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        for folder in ["home", "bin", "work"] {
            fs::create_dir_all(root.join(folder)).unwrap();
        }
        let machine = Machine { _dir: dir, root };
        let claude = format!(
            "case \"$1\" in\n--version) echo '2.1.318 (Claude Code)' ;;\nauth) echo '{{\"loggedIn\": true, \"account\": \"{SENTINEL}\"}}' ;;\nesac\n"
        );
        machine.script("claude", &claude);
        let codex = format!(
            "case \"$1\" in\n--version) echo 'codex-cli 0.163.0' ;;\nlogin) echo {SENTINEL}; exit {codex_login_exit} ;;\nesac\n"
        );
        machine.script("codex", &codex);
        machine
    }

    fn script(&self, name: &str, body: &str) {
        let path = self.root.join("bin").join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    fn command(&self, program: &str) -> Command {
        let mut command = Command::new(program);
        command
            .env_clear()
            .env("PATH", self.root.join("bin"))
            .env("HOME", self.root.join("home"));
        command
            .env("GIT_CEILING_DIRECTORIES", &self.root)
            .current_dir(self.root.join("work"));
        command
    }

    fn oah(&self, args: &[&str]) -> Output {
        self.command(env!("CARGO_BIN_EXE_oah"))
            .args(args)
            .output()
            .unwrap()
    }
}

/// Every path below `root` with its kind, mode and bytes.
fn tree(root: &Path) -> BTreeMap<PathBuf, String> {
    let (mut entries, mut folders) = (BTreeMap::new(), vec![root.to_path_buf()]);
    while let Some(folder) = folders.pop() {
        for entry in fs::read_dir(&folder).unwrap() {
            let path = entry.unwrap().path();
            let meta = fs::symlink_metadata(&path).unwrap();
            if meta.is_dir() {
                folders.push(path.clone());
            }
            let bytes = if meta.is_file() {
                fs::read(&path).unwrap()
            } else {
                Vec::new()
            };
            entries.insert(
                path.clone(),
                format!("{:o} {bytes:?}", meta.permissions().mode()),
            );
        }
    }
    entries
}

fn status_of<'a>(rows: &'a [serde_json::Value], id: &str) -> &'a str {
    rows.iter()
        .find(|row| row["id"] == id)
        .and_then(|row| row["status"].as_str())
        .unwrap()
}

#[test]
fn doctor_json_prints_every_agent_and_fails_while_a_stage_agent_is_signed_out() {
    let machine = Machine::new(1);
    let before = tree(&machine.root);
    let out = machine.oah(&["doctor", "--json"]);
    assert_eq!(
        tree(&machine.root),
        before,
        "doctor creates and changes nothing"
    );
    let stdout = String::from_utf8(out.stdout).unwrap();
    assert!(!stdout.contains(SENTINEL), "{stdout}");
    assert!(
        out.stderr.is_empty(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let rows: Vec<serde_json::Value> = serde_json::from_str(&stdout).unwrap();
    let ids: Vec<&str> = rows.iter().map(|row| row["id"].as_str().unwrap()).collect();
    assert_eq!(
        ids,
        [
            "claude",
            "codex",
            "gemini",
            "copilot",
            "opencode",
            "antigravity",
            "cursor"
        ]
    );
    assert_eq!(
        (status_of(&rows, "claude"), status_of(&rows, "codex")),
        ("ok", "not-signed-in")
    );
    assert_eq!(status_of(&rows, "gemini"), "missing");
    assert_eq!(
        rows[0]["source"],
        machine
            .root
            .join("bin")
            .join("claude")
            .display()
            .to_string()
    );
    assert_eq!(out.status.code(), Some(1), "the default stages use Codex");
}

#[test]
fn doctor_exits_zero_once_every_stage_agent_is_usable() {
    let machine = Machine::new(0);
    let out = machine.oah(&["doctor"]);
    assert_eq!(out.status.code(), Some(0));
    let stdout = String::from_utf8(out.stdout).unwrap();
    let lines: Vec<&str> = stdout.lines().collect();
    assert_eq!(lines.len(), 7, "{stdout}");
    assert!(
        lines[0].starts_with("Claude Code: ok (2.1.318 (Claude Code), "),
        "{stdout}"
    );
    assert!(
        lines[1].starts_with("Codex: ok (codex-cli 0.163.0, "),
        "{stdout}"
    );
    assert_eq!(
        lines[2],
        "Gemini CLI: missing; install: https://github.com/google-gemini/gemini-cli"
    );
    assert!(!stdout.contains(SENTINEL));
}

#[test]
fn a_signed_out_agent_shows_how_to_sign_in() {
    let out = Machine::new(1).oah(&["doctor"]);
    let stdout = String::from_utf8(out.stdout).unwrap();
    let codex = stdout
        .lines()
        .find(|line| line.starts_with("Codex:"))
        .unwrap();
    assert!(
        codex.starts_with("Codex: not-signed-in (codex-cli 0.163.0, "),
        "{codex}"
    );
    assert!(codex.ends_with("; sign in: codex login"), "{codex}");
}

#[test]
fn a_core_error_is_one_coded_value_on_stdout_with_json_and_one_line_on_stderr_without() {
    let machine = Machine::new(0);
    let gone = machine.root.join("gone");
    for (args, json) in [("doctor --json", true), ("doctor", false)] {
        fs::create_dir_all(&gone).unwrap();
        let script = format!(
            "cd '{}' && /bin/rmdir '{}' && exec '{}' {args}",
            gone.display(),
            gone.display(),
            env!("CARGO_BIN_EXE_oah")
        );
        let out = machine
            .command("/bin/sh")
            .args(["-c", &script])
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(1), "{args}");
        let (stdout, stderr) = (
            String::from_utf8(out.stdout).unwrap(),
            String::from_utf8(out.stderr).unwrap(),
        );
        if json {
            let value: serde_json::Value = serde_json::from_str(&stdout).unwrap();
            let error = value["error"].as_object().unwrap();
            assert_eq!(
                error.keys().collect::<Vec<_>>(),
                ["code", "hint", "message"]
            );
            assert!(error["code"].as_str().unwrap().starts_with("OAH_"));
            assert!(stderr.is_empty());
        } else {
            assert!(stdout.is_empty());
            assert_eq!(stderr.lines().count(), 1, "{stderr}");
            assert!(
                stderr.starts_with("OAH_") && stderr.contains(": "),
                "{stderr}"
            );
        }
    }
}

#[test]
fn a_legacy_hook_call_exits_zero_with_one_line_and_runs_nothing() {
    let machine = Machine::new(0);
    let before = tree(&machine.root);
    for args in [
        &["hook", "stop"][..],
        &[
            "hook",
            "pre-push",
            "origin",
            "https://example.invalid/r.git",
        ],
        &["hook", "a\nb"],
    ] {
        let out = machine.oah(args);
        assert_eq!(out.status.code(), Some(0), "{args:?}");
        assert!(out.stdout.is_empty());
        let stderr = String::from_utf8(out.stderr).unwrap();
        assert_eq!(stderr.lines().count(), 1, "{stderr}");
        assert!(
            stderr.contains("0.4") && stderr.contains("did not run"),
            "{stderr}"
        );
    }
    assert_eq!(tree(&machine.root), before);
    assert_eq!(
        machine.oah(&["hook"]).status.code(),
        Some(2),
        "the kind is required"
    );
}

#[test]
fn doctor_help_identifies_the_project_commands_source() {
    let out = Machine::new(0).oah(&["doctor", "--help"]);
    assert_eq!(out.status.code(), Some(0));
    assert!(out.stderr.is_empty());
    let help = String::from_utf8(out.stdout).unwrap();
    assert!(help.contains("--use-project-commands"), "{help}");
    assert!(
        help.contains(".oah/config.toml") && help.contains("[agents.<id>] command"),
        "{help}"
    );
}

#[test]
fn doctor_ignores_project_commands_by_default_and_opts_in_with_flag() {
    let machine = Machine::new(1);
    machine.script("git", "echo 'fatal: not a git repository' >&2\nexit 128\n");
    let project_claude = format!(
        "case \"$1\" in\n--version) echo '2.2.0-project (Claude Code)' ;;\nauth) echo '{{\"loggedIn\": true, \"account\": \"{SENTINEL}\"}}' ;;\nesac\n"
    );
    machine.script("claude-project", &project_claude);
    let config_dir = machine.root.join("work").join(".oah");
    fs::create_dir_all(&config_dir).unwrap();
    fs::write(
        config_dir.join("config.toml"),
        "[stages]\nplan = \"claude\"\nbuild = \"claude\"\nreview = \"claude\"\nfix = \"claude\"\n\n\
         [agents.claude]\ncommand = [\"claude-project\"]\n",
    )
    .unwrap();
    let before = tree(&machine.root);

    let default_out = machine.oah(&["doctor", "--json"]);
    assert_eq!(
        tree(&machine.root),
        before,
        "default doctor creates and changes nothing"
    );
    assert_eq!(
        default_out.status.code(),
        Some(0),
        "project [stages] assigns Claude only"
    );
    assert!(default_out.stderr.is_empty());
    let default_stdout = String::from_utf8(default_out.stdout).unwrap();
    assert!(!default_stdout.contains(SENTINEL), "{default_stdout}");
    let default_rows: Vec<serde_json::Value> = serde_json::from_str(&default_stdout).unwrap();
    assert_eq!(default_rows[0]["status"], "ok");
    assert_eq!(default_rows[0]["version"], "2.1.318 (Claude Code)");
    assert_eq!(
        default_rows[0]["source"],
        machine
            .root
            .join("bin")
            .join("claude")
            .display()
            .to_string()
    );
    assert_eq!(status_of(&default_rows, "codex"), "not-signed-in");

    let explicit_out = machine.oah(&["doctor", "--use-project-commands", "--json"]);
    assert_eq!(
        tree(&machine.root),
        before,
        "explicit doctor creates and changes nothing"
    );
    assert_eq!(
        explicit_out.status.code(),
        Some(0),
        "project [stages] still governs readiness in explicit mode"
    );
    assert!(explicit_out.stderr.is_empty());
    let explicit_stdout = String::from_utf8(explicit_out.stdout).unwrap();
    assert!(!explicit_stdout.contains(SENTINEL), "{explicit_stdout}");
    let explicit_rows: Vec<serde_json::Value> = serde_json::from_str(&explicit_stdout).unwrap();
    let default_ids: Vec<&str> = default_rows
        .iter()
        .map(|row| row["id"].as_str().unwrap())
        .collect();
    let explicit_ids: Vec<&str> = explicit_rows
        .iter()
        .map(|row| row["id"].as_str().unwrap())
        .collect();
    assert_eq!(explicit_ids, default_ids);
    assert_eq!(explicit_rows[0]["status"], "ok");
    assert_eq!(explicit_rows[0]["version"], "2.2.0-project (Claude Code)");
    assert_eq!(
        explicit_rows[0]["source"],
        machine
            .root
            .join("bin")
            .join("claude-project")
            .display()
            .to_string()
    );
    assert_eq!(status_of(&explicit_rows, "codex"), "not-signed-in");
    assert_eq!(
        explicit_rows[1]["source"],
        machine.root.join("bin").join("codex").display().to_string()
    );

    let text_out = machine.oah(&["doctor", "--use-project-commands"]);
    assert_eq!(tree(&machine.root), before);
    assert_eq!(text_out.status.code(), Some(0));
    assert!(text_out.stderr.is_empty());
    let text_stdout = String::from_utf8(text_out.stdout).unwrap();
    let lines: Vec<&str> = text_stdout.lines().collect();
    assert_eq!(lines.len(), 7, "{text_stdout}");
    assert!(
        lines[0].starts_with("Claude Code: ok (2.2.0-project (Claude Code), ")
            && lines[0].contains("claude-project"),
        "{text_stdout}"
    );
    assert!(!text_stdout.contains(SENTINEL), "{text_stdout}");
}
