// oah sections·rules 시험: 가짜 PATH 의 가짜 에이전트로 실제 oah 를 돌려 JSON 모양, 점검이 아무것도 쓰지 않음, 동기화 뒤 점검이 깨끗함을 본다.
#![cfg(unix)]

use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

/// A temporary root with `home`, `bin` (the only `PATH` folder, with Git linked in) and `work` (the project).
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
        let machine = Machine { _dir: dir, root };
        machine.script(
            "claude",
            "case \"$1\" in\n--version) echo '2.1.318 (Claude Code)' ;;\nauth) echo '{\"loggedIn\": true}' ;;\nesac\n",
        );
        machine.script(
            "codex",
            "case \"$1\" in\n--version) echo 'codex-cli 0.163.0' ;;\nlogin) echo 'Not logged in'; exit 1 ;;\nesac\n",
        );
        let git = std::env::split_paths(&std::env::var_os("PATH").unwrap())
            .map(|folder| folder.join("git"))
            .find(|path| path.is_file())
            .expect("git is on PATH");
        std::os::unix::fs::symlink(git, machine.root.join("bin").join("git")).unwrap();
        machine
    }

    fn script(&self, name: &str, body: &str) {
        let path = self.root.join("bin").join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    fn work(&self) -> PathBuf {
        self.root.join("work")
    }

    fn oah(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_oah"))
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
}

/// Every file below `root` with its bytes.
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

fn json(out: &Output) -> serde_json::Value {
    serde_json::from_slice(&out.stdout)
        .unwrap_or_else(|error| panic!("{error}: {}", String::from_utf8_lossy(&out.stdout)))
}

#[test]
fn sections_show_json_is_the_app_page_and_writes_nothing() {
    let machine = Machine::new();
    fs::create_dir_all(machine.work().join(".oah")).unwrap();
    fs::write(
        machine.work().join(".oah/config.toml"),
        "[stages]\ninitial = \"claude\"\nreview = \"codex\"\ncorrection = \"codex\"\n",
    )
    .unwrap();
    let before = tree(&machine.work());
    let out = machine.oah(&["sections", "show", "--json"]);
    assert_eq!(
        out.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(
        tree(&machine.work()),
        before,
        "sections show writes nothing"
    );
    let page = json(&out);
    let sections: Vec<(&str, &str, &str)> = page["sections"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| {
            (
                s["id"].as_str().unwrap(),
                s["agent"].as_str().unwrap(),
                s["source"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        sections,
        [
            ("plan", "claude", "legacy:initial"),
            ("build", "claude", "legacy:initial"),
            ("review", "codex", "file"),
            ("fix", "codex", "legacy:correction"),
        ]
    );
    assert_eq!(page["config_format"], "legacy");
    assert_eq!(page["banner"]["kind"], "not-signed-in");
    assert_eq!(page["banner"]["agents"][0]["id"], "codex");
    assert!(
        page["agents"]
            .as_array()
            .unwrap()
            .iter()
            .all(|a| a["id"] != "cursor")
    );
    assert_eq!(page["rules"]["synced"], false);

    let text = machine.oah(&["sections", "show"]);
    let text = String::from_utf8(text.stdout).unwrap();
    assert!(
        text.contains("Code Review [review]: codex (file)"),
        "{text}"
    );
    assert!(text.contains("Status: Not signed in: Codex"), "{text}");
}

#[test]
fn rules_check_writes_nothing_and_rules_sync_makes_it_clean() {
    let machine = Machine::new();
    fs::write(
        machine.work().join("AGENTS.md"),
        "# Ours\n\nKeep this text.\n",
    )
    .unwrap();
    let before = tree(&machine.work());
    let check = machine.oah(&["rules", "check"]);
    assert_eq!(check.status.code(), Some(1), "drift exits 1");
    assert_eq!(tree(&machine.work()), before, "rules check writes nothing");
    let text = String::from_utf8(check.stdout).unwrap();
    assert!(
        text.contains("OAH_RULES_DRIFT: the managed block in AGENTS.md is missing or differs"),
        "{text}"
    );
    assert!(
        text.contains("OAH_RULES_DRIFT: CLAUDE.md is missing"),
        "{text}"
    );

    let state = json(&machine.oah(&["rules", "check", "--json"]));
    assert_eq!(state["synced"], false);
    let writes: Vec<&str> = state["writes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|w| w["path"].as_str().unwrap())
        .collect();
    assert_eq!(writes, ["AGENTS.md", "CLAUDE.md"]);
    assert_eq!(tree(&machine.work()), before);

    let sync = machine.oah(&["rules", "sync", "--json"]);
    assert_eq!(
        sync.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&sync.stdout)
    );
    let result = json(&sync);
    assert_eq!(result["synced"], true);
    let agents = fs::read_to_string(machine.work().join("AGENTS.md")).unwrap();
    assert!(
        agents.starts_with("# Ours\n\nKeep this text.\n\n<!-- OAH:RULES:BEGIN v1 -->"),
        "{agents}"
    );
    assert_eq!(
        fs::read(machine.work().join("CLAUDE.md")).unwrap(),
        b"@AGENTS.md\n"
    );

    let clean = machine.oah(&["rules", "check"]);
    assert_eq!(clean.status.code(), Some(0));
    assert_eq!(String::from_utf8(clean.stdout).unwrap(), "Rules: in sync\n");
    let again = machine.oah(&["rules", "sync"]);
    assert_eq!(
        String::from_utf8(again.stdout).unwrap(),
        "Rules: already in sync\n"
    );

    // A hand edit inside the block is drift again, and the check still writes nothing.
    fs::write(
        machine.work().join("AGENTS.md"),
        agents.replace("| Codex |", "| Someone |"),
    )
    .unwrap();
    let edited = tree(&machine.work());
    assert_eq!(machine.oah(&["rules", "check"]).status.code(), Some(1));
    assert_eq!(tree(&machine.work()), edited);
}

#[test]
fn a_rules_refusal_is_the_c5_error_shape_and_writes_nothing() {
    let machine = Machine::new();
    fs::write(
        machine.work().join("AGENTS.md"),
        "<!-- OAH:RULES:BEGIN v1 -->\nno end\n",
    )
    .unwrap();
    let before = tree(&machine.work());
    let out = machine.oah(&["rules", "sync", "--json"]);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(json(&out)["error"]["code"], "OAH_RULES_MARKER_BROKEN");
    assert_eq!(tree(&machine.work()), before);
    let check = json(&machine.oah(&["rules", "check", "--json"]));
    assert_eq!(check["error"]["code"], "OAH_RULES_MARKER_BROKEN");
}
