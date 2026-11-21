// 규칙 쓰기 시험: 계획대로 쓰고 다시 읽으며, 되읽기 실패는 OAH_RULES_VERIFY, 근거가 바뀌면 아무것도 쓰지 않고, 점검은 쓰지 않는다.
use crate::config::{AgentId, ProjectConfig};
use crate::fsx::LockOptions;
use crate::rules::{RulesSnapshot, SyncInput, apply_sync, apply_sync_with, check_sync, plan_sync};
use crate::support::write_files;
use oah_core::Coded;
use oah_core::plan::{ItemStatus, Outcome};
use oah_core::tasks::CancelToken;
use std::fs;
use std::path::Path;

fn tree(root: &Path) -> Vec<(String, Vec<u8>)> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
            } else {
                out.push((path.display().to_string(), fs::read(&path).unwrap()));
            }
        }
    }
    out.sort();
    out
}

fn input_for<'a>(config: &'a ProjectConfig, detected: &'a [AgentId]) -> SyncInput<'a> {
    SyncInput {
        effective: config.effective(),
        always: &[],
        detected,
        lang: None,
    }
}

fn defaults() -> ProjectConfig {
    ProjectConfig::read(Path::new("/nonexistent-oah-config/config.toml")).unwrap()
}

#[test]
fn a_sync_writes_every_planned_file_reads_it_back_and_is_then_a_fixed_point() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_files(root, &[("GEMINI.md", b"# Gemini\n")]);
    let config = defaults();
    let detected = [AgentId::Cursor];
    let snapshot = RulesSnapshot::read(root).unwrap();
    let plan = plan_sync(&snapshot, &input_for(&config, &detected)).unwrap();
    let paths: Vec<&str> = plan.writes().map(|file| file.path).collect();
    assert_eq!(
        paths,
        [
            "AGENTS.md",
            "CLAUDE.md",
            "GEMINI.md",
            ".cursor/rules/oah.mdc"
        ]
    );
    let result = apply_sync(root, &plan, &CancelToken::new(), LockOptions::default());
    assert!(result.synced(), "{:?}", result.error);
    assert_eq!(result.receipts.len(), 4);
    assert!(
        result
            .items
            .iter()
            .all(|(_, _, item)| item.status == ItemStatus::Done)
    );
    assert_eq!(
        fs::read(root.join("GEMINI.md")).unwrap(),
        b"# Gemini\n@AGENTS.md\n"
    );
    let (again, drift) = check_sync(
        &RulesSnapshot::read(root).unwrap(),
        &input_for(&config, &detected),
    )
    .unwrap();
    assert!(drift.is_empty(), "{drift:?}");
    assert_eq!(again.writes().count(), 0);
}

#[test]
fn a_write_that_does_not_read_back_is_rules_verify_and_stops_the_sync() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let config = defaults();
    let plan = plan_sync(
        &RulesSnapshot::read(root).unwrap(),
        &input_for(&config, &[]),
    )
    .unwrap();
    let result = apply_sync_with(root, &plan, &CancelToken::new(), |target, _bytes| {
        fs::write(target.path, b"something else\n").unwrap();
        Outcome::Done(Vec::new())
    });
    let error = result.error.expect("the read-back failed");
    assert_eq!(error.code(), "OAH_RULES_VERIFY");
    let statuses: Vec<ItemStatus> = result
        .items
        .iter()
        .map(|(_, _, item)| item.status)
        .collect();
    assert_eq!(statuses, [ItemStatus::Unknown, ItemStatus::NotRun]);
    assert!(
        !root.join("CLAUDE.md").exists(),
        "the sync stopped at the first unverified file"
    );
}

#[test]
fn a_file_changed_after_planning_is_stale_and_nothing_is_written() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_files(root, &[("CLAUDE.md", b"one\n")]);
    let config = defaults();
    let plan = plan_sync(
        &RulesSnapshot::read(root).unwrap(),
        &input_for(&config, &[]),
    )
    .unwrap();
    fs::write(root.join("CLAUDE.md"), b"two\n").unwrap();
    let result = apply_sync(root, &plan, &CancelToken::new(), LockOptions::default());
    assert_eq!(result.error.unwrap().code(), "OAH_PLAN_STALE");
    assert!(!root.join("AGENTS.md").exists());
    assert_eq!(fs::read(root.join("CLAUDE.md")).unwrap(), b"two\n");
}

#[test]
fn a_drift_check_reports_hand_edits_and_thin_files_and_writes_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let config = defaults();
    let plan = plan_sync(
        &RulesSnapshot::read(root).unwrap(),
        &input_for(&config, &[]),
    )
    .unwrap();
    assert!(apply_sync(root, &plan, &CancelToken::new(), LockOptions::default()).synced());
    let agents = fs::read_to_string(root.join("AGENTS.md")).unwrap();
    fs::write(
        root.join("AGENTS.md"),
        agents.replace("| Codex |", "| Someone |"),
    )
    .unwrap();
    let before = tree(root);
    let (_, drift) = check_sync(
        &RulesSnapshot::read(root).unwrap(),
        &input_for(&config, &[AgentId::Gemini]),
    )
    .unwrap();
    let lines: Vec<(&str, &str)> = drift.iter().map(|line| (line.path, line.code)).collect();
    assert_eq!(
        lines,
        [
            ("AGENTS.md", "OAH_RULES_DRIFT"),
            ("GEMINI.md", "OAH_RULES_DRIFT")
        ]
    );
    assert_eq!(tree(root), before, "a check writes nothing");
}

#[cfg(unix)]
#[test]
fn a_linked_folder_on_the_way_to_a_thin_file_is_refused_before_any_write() {
    let dir = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    let root = dir.path();
    std::os::unix::fs::symlink(outside.path(), root.join(".cursor")).unwrap();
    let config = defaults();
    let error = plan_sync(
        &RulesSnapshot::read(root).unwrap(),
        &input_for(&config, &[AgentId::Cursor]),
    )
    .unwrap_err();
    assert_eq!(error.code(), "OAH_RULES_LINK");
    assert!(fs::read_dir(outside.path()).unwrap().next().is_none());
    assert!(!root.join("AGENTS.md").exists());
}
