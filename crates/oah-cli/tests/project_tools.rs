// oah project-tools status 시험: 실제 oah 를 돌려 JSON·글 모양을 보고, 규칙 파일이 없는 Git 프로젝트에서 아무 바이트도 바뀌지 않으며 에이전트를 실행하지 않음을 본다.
#![cfg(unix)]

use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

/// A temporary root with `home`, `bin` (the only `PATH` folder: Git, and agents that leave a mark when run) and
/// `work` (the project).
struct Machine {
    _dir: tempfile::TempDir,
    root: PathBuf,
}

impl Machine {
    fn new() -> Machine {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        for folder in ["home", "bin", "work"] {
            fs::create_dir_all(root.join(folder)).unwrap();
        }
        let git = std::env::split_paths(&std::env::var_os("PATH").unwrap())
            .map(|folder| folder.join("git"))
            .find(|path| path.is_file())
            .expect("git is on PATH");
        std::os::unix::fs::symlink(git, root.join("bin").join("git")).unwrap();
        for agent in ["claude", "codex"] {
            let path = root.join("bin").join(agent);
            let mark = root.join("home").join(format!("{agent}-was-run"));
            fs::write(&path, format!("#!/bin/sh\ntouch '{}'\n", mark.display())).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        }
        Machine { _dir: dir, root }
    }

    fn work(&self) -> PathBuf {
        self.root.join("work")
    }

    fn run(&self, program: &Path, args: &[&str]) -> Output {
        Command::new(program)
            .env_clear()
            .env("PATH", self.root.join("bin"))
            .env("HOME", self.root.join("home"))
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CEILING_DIRECTORIES", &self.root)
            .current_dir(self.work())
            .args(args)
            .output()
            .unwrap()
    }

    fn oah(&self, args: &[&str]) -> Output {
        self.run(Path::new(env!("CARGO_BIN_EXE_oah")), args)
    }

    fn git(&self, folder: &Path, args: &[&str]) {
        let out = Command::new(self.root.join("bin").join("git"))
            .env_clear()
            .env("HOME", self.root.join("home"))
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .args([
                "-c",
                "user.name=Example",
                "-c",
                "user.email=example@example.invalid",
            ])
            .args(["-c", "commit.gpgsign=false"])
            .current_dir(folder)
            .args(args)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }

    /// `oah project-tools status --json` in `folder` with an empty `PATH`: no Git, no agent, nothing to start.
    fn states_without_any_program(&self, folder: &Path) -> Vec<String> {
        let out = Command::new(env!("CARGO_BIN_EXE_oah"))
            .env_clear()
            .env("PATH", "")
            .env("HOME", self.root.join("home"))
            .env("GIT_CEILING_DIRECTORIES", &self.root)
            .current_dir(folder)
            .args(["project-tools", "status", "--json"])
            .output()
            .unwrap();
        assert_eq!(
            out.status.code(),
            Some(0),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        json["tools"]
            .as_array()
            .unwrap()
            .iter()
            .map(|tool| tool["state"].as_str().unwrap().to_string())
            .collect()
    }
}

/// Every entry below `root` with its bytes.
fn tree(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
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
            entries.insert(path, bytes);
        }
    }
    entries
}

#[test]
fn status_reads_a_git_project_without_rules_files_and_changes_nothing() {
    let machine = Machine::new();
    let work = machine.work();
    fs::create_dir_all(work.join("docs/wiki")).unwrap();
    fs::write(work.join("docs/wiki/index.md"), "# Wiki\n").unwrap();
    let init = machine.run(&machine.root.join("bin").join("git"), &["init", "-q"]);
    assert!(init.status.success());
    let before = tree(&machine.root);

    let out = machine.oah(&["project-tools", "status", "--json"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(out.stderr.is_empty());
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let states: Vec<(&str, &str)> = json["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| {
            (
                tool["id"].as_str().unwrap(),
                tool["state"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        states,
        [
            ("wiki", "found"),
            ("impact-map", "no-commit"),
            ("jev", "off")
        ]
    );
    assert_eq!(json["project_id"].as_str().unwrap().len(), 16);
    assert!(
        !String::from_utf8_lossy(&out.stdout).contains(machine.root.to_str().unwrap()),
        "a full path is printed"
    );

    let text = machine.oah(&["project-tools", "status"]);
    assert_eq!(text.status.code(), Some(0));
    let text = String::from_utf8_lossy(&text.stdout);
    assert!(
        text.contains("Project Wiki: Wiki found at docs/wiki, the default location."),
        "{text}"
    );
    assert!(
        text.contains("Impact Map: This Git repository has no commits yet."),
        "{text}"
    );
    assert!(
        text.contains("  Next step: Make the first commit"),
        "{text}"
    );
    assert!(text.contains("Jev: Jev is off for this project."), "{text}");

    assert_eq!(
        tree(&machine.root),
        before,
        "status wrote something or ran an agent"
    );
}

#[test]
fn status_needs_its_subcommand_and_reports_a_broken_settings_file_per_card() {
    let machine = Machine::new();
    assert_eq!(machine.oah(&["project-tools"]).status.code(), Some(2));
    fs::create_dir_all(machine.work().join(".oah")).unwrap();
    fs::write(machine.work().join(".oah/config.toml"), "[wiki\n").unwrap();
    let out = machine.oah(&["project-tools", "status", "--json"]);
    assert_eq!(out.status.code(), Some(0));
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(json["tools"][0]["state"], "settings-unreadable");
    assert_eq!(json["tools"][1]["state"], "no-git");
    assert_eq!(json["tools"][2]["state"], "settings-unreadable");
}

#[test]
fn status_needs_no_git_program_for_plain_unborn_committed_and_linked_projects() {
    let machine = Machine::new();
    let work = machine.work();
    let (plain, unborn, repo, linked) = (
        work.join("plain"),
        work.join("unborn"),
        work.join("repo"),
        work.join("linked"),
    );
    for folder in [&plain, &unborn, &repo] {
        fs::create_dir_all(folder.join("docs/wiki")).unwrap();
        fs::write(folder.join("docs/wiki/index.md"), "# Wiki\n").unwrap();
    }
    machine.git(&unborn, &["init", "-q"]);
    machine.git(&repo, &["init", "-q"]);
    machine.git(&repo, &["add", "."]);
    machine.git(&repo, &["commit", "-q", "-m", "first"]);
    machine.git(&repo, &["worktree", "add", "-q", linked.to_str().unwrap()]);
    let before = tree(&machine.root);

    assert_eq!(
        machine.states_without_any_program(&plain),
        ["found", "no-git", "off"]
    );
    assert_eq!(
        machine.states_without_any_program(&unborn),
        ["found", "no-commit", "off"]
    );
    assert_eq!(
        machine.states_without_any_program(&repo),
        ["found", "ready", "off"]
    );
    assert_eq!(
        machine.states_without_any_program(&linked),
        ["found", "ready", "off"]
    );
    // From a folder inside the work tree the project is still the work tree.
    assert_eq!(
        machine.states_without_any_program(&repo.join("docs")),
        ["found", "ready", "off"]
    );

    assert_eq!(tree(&machine.root), before, "status wrote something");
    for agent in ["claude", "codex"] {
        assert!(
            !machine
                .root
                .join("home")
                .join(format!("{agent}-was-run"))
                .exists()
        );
    }
}
