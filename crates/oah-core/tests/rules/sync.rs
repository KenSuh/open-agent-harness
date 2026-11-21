// 규칙 쓰기 시험: 계획대로 쓰고 다시 읽으며, 되읽기 실패는 OAH_RULES_VERIFY, 근거가 바뀌면 아무것도 쓰지 않고, 점검은 쓰지 않는다.
use crate::config::{AgentId, ProjectConfig};
use crate::fsx::LockOptions;
use crate::rules::{
    RulesFiles, RulesSnapshot, SyncInput, apply_sync, apply_sync_with, check_sync, plan_sync,
};
use crate::support::write_files;
use oah_core::Coded;
use oah_core::plan::{ItemStatus, Outcome, write_target};
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

fn input_for(config: &ProjectConfig) -> SyncInput<'_> {
    SyncInput {
        config,
        always: &[],
        lang: None,
    }
}

fn config_at(root: &Path) -> ProjectConfig {
    ProjectConfig::read(&root.join(".oah/config.toml")).unwrap()
}

#[test]
fn a_sync_writes_every_planned_file_reads_it_back_and_is_then_a_fixed_point() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_files(root, &[("GEMINI.md", b"# Gemini\n")]);
    let config = config_at(root);
    let detected = [AgentId::Cursor];
    let snapshot = RulesSnapshot::read(root, &detected).unwrap();
    let plan = plan_sync(&snapshot, &input_for(&config)).unwrap();
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
    let result = apply_sync(&plan, &CancelToken::new(), LockOptions::default());
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
        &RulesSnapshot::read(root, &detected).unwrap(),
        &input_for(&config),
    )
    .unwrap();
    assert!(drift.is_empty(), "{drift:?}");
    assert_eq!(again.writes().count(), 0);
}

#[test]
fn a_write_that_does_not_read_back_is_rules_verify_and_stops_the_sync() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let config = config_at(root);
    let plan = plan_sync(
        &RulesSnapshot::read(root, &[]).unwrap(),
        &input_for(&config),
    )
    .unwrap();
    let result = apply_sync_with(&plan, &CancelToken::new(), |target, _bytes| {
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
    let config = config_at(root);
    let plan = plan_sync(
        &RulesSnapshot::read(root, &[]).unwrap(),
        &input_for(&config),
    )
    .unwrap();
    fs::write(root.join("CLAUDE.md"), b"two\n").unwrap();
    let result = apply_sync(&plan, &CancelToken::new(), LockOptions::default());
    assert_eq!(result.error.unwrap().code(), "OAH_PLAN_STALE");
    assert!(!root.join("AGENTS.md").exists());
    assert_eq!(fs::read(root.join("CLAUDE.md")).unwrap(), b"two\n");
}

#[test]
fn an_immediate_rules_apply_checks_the_source_bytes_it_rendered() {
    for relative in [".oah/config.toml", ".oah/rules.local.md"] {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        fs::create_dir(root.join(".oah")).unwrap();
        let config = config_at(root);
        let plan = plan_sync(
            &RulesSnapshot::read(root, &[]).unwrap(),
            &input_for(&config),
        )
        .unwrap();
        fs::write(root.join(relative), b"# added after the read\n").unwrap();
        let before = tree(root);
        let result = apply_sync(&plan, &CancelToken::new(), LockOptions::default());
        assert_eq!(result.error.unwrap().code(), "OAH_PLAN_STALE", "{relative}");
        assert_eq!(tree(root), before, "{relative}");
    }
}

#[test]
fn regular_local_rules_are_rendered_and_rechecked_before_apply() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_files(
        root,
        &[(".oah/rules.local.md", b"Follow local guidance.\n")],
    );
    let config = config_at(root);
    let read_plan = || {
        plan_sync(
            &RulesSnapshot::read(root, &[]).unwrap(),
            &input_for(&config),
        )
        .unwrap()
    };
    let plan = read_plan();
    let cancel = CancelToken::new();
    let lock = LockOptions::default();
    fs::write(root.join(".oah/rules.local.md"), b"Use the new guidance.\n").unwrap();
    assert_eq!(
        apply_sync(&plan, &cancel, lock).error.unwrap().code(),
        "OAH_PLAN_STALE"
    );
    assert!(!root.join("AGENTS.md").exists());
    assert!(apply_sync(&read_plan(), &cancel, lock).synced());
    assert!(
        fs::read_to_string(root.join("AGENTS.md"))
            .unwrap()
            .contains("Use the new guidance.")
    );
}

#[cfg(unix)]
#[test]
fn linked_local_rules_leaf_and_ancestor_are_refused_before_plan_and_apply() {
    use std::os::unix::fs::symlink;

    // 1. Leaf symlink at plan time.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let inert = root.join("inert.md");
    fs::write(&inert, b"inert fixture\n").unwrap();
    fs::create_dir(root.join(".oah")).unwrap();
    symlink(&inert, root.join(".oah/rules.local.md")).unwrap();
    let config = config_at(root);
    let error = plan_sync(
        &RulesSnapshot::read(root, &[]).unwrap(),
        &input_for(&config),
    )
    .unwrap_err();
    assert_eq!(error.code(), "OAH_RULES_LINK");
    assert!(error.message.contains(".oah/rules.local.md"));
    assert!(!error.message.contains("inert.md"));
    let io_error = RulesFiles::read(root).unwrap_err();
    assert_eq!(io_error.code(), "OAH_FILE_IO");
    assert!(!io_error.to_string().contains("inert.md"));
    assert!(!root.join("AGENTS.md").exists());
    assert!(!root.join("CLAUDE.md").exists());

    // 2. Traversed in-project ancestor `.oah` symlink at plan time.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let inert_dir = root.join("inert-dir");
    fs::create_dir(&inert_dir).unwrap();
    fs::write(inert_dir.join("rules.local.md"), b"inert fixture\n").unwrap();
    symlink(&inert_dir, root.join(".oah")).unwrap();
    let config = ProjectConfig::read(&root.join("missing.toml")).unwrap();
    let error = plan_sync(
        &RulesSnapshot::read(root, &[]).unwrap(),
        &input_for(&config),
    )
    .unwrap_err();
    assert_eq!(error.code(), "OAH_RULES_LINK");
    assert!(error.message.contains(".oah"));
    assert!(!error.message.contains("inert-dir"));
    let io_error = RulesFiles::read(root).unwrap_err();
    assert_eq!(io_error.code(), "OAH_FILE_IO");
    assert!(!io_error.to_string().contains("inert-dir"));
    assert!(!root.join("AGENTS.md").exists());

    // 3. Leaf replaced with an in-project symlink (even matching bytes) between plan and apply.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let same_bytes = b"Follow local guidance.\n";
    let inert = root.join("inert.md");
    fs::write(&inert, same_bytes).unwrap();
    write_files(root, &[(".oah/rules.local.md", same_bytes)]);
    let config = config_at(root);
    let plan = plan_sync(
        &RulesSnapshot::read(root, &[]).unwrap(),
        &input_for(&config),
    )
    .unwrap();
    fs::remove_file(root.join(".oah/rules.local.md")).unwrap();
    symlink(&inert, root.join(".oah/rules.local.md")).unwrap();
    let result = apply_sync(&plan, &CancelToken::new(), LockOptions::default());
    let err = result.error.unwrap();
    assert_eq!(err.code(), "OAH_RULES_LINK");
    assert!(!err.message().contains("inert.md"));
    assert!(!root.join("AGENTS.md").exists());
    assert!(!root.join("CLAUDE.md").exists());

    // 4. Absent `.oah` replaced with an in-project symlink between plan and apply.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let inert_dir = root.join("inert-dir");
    fs::create_dir(&inert_dir).unwrap();
    let config = config_at(root);
    let plan = plan_sync(
        &RulesSnapshot::read(root, &[]).unwrap(),
        &input_for(&config),
    )
    .unwrap();
    symlink(&inert_dir, root.join(".oah")).unwrap();
    let result = apply_sync(&plan, &CancelToken::new(), LockOptions::default());
    let err = result.error.unwrap();
    assert_eq!(err.code(), "OAH_RULES_LINK");
    assert!(!err.message().contains("inert-dir"));
    assert!(!root.join("AGENTS.md").exists());
}

#[test]
fn a_target_changed_during_apply_keeps_completed_writes_and_stops_at_that_target() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let config = config_at(root);
    let plan = plan_sync(
        &RulesSnapshot::read(root, &[AgentId::Gemini]).unwrap(),
        &input_for(&config),
    )
    .unwrap();
    let result = apply_sync_with(&plan, &CancelToken::new(), |target, bytes| {
        let outcome = write_target(target, bytes, LockOptions::default());
        if target.path == root.join("AGENTS.md") {
            fs::write(root.join("CLAUDE.md"), b"user edit\n").unwrap();
        }
        outcome
    });
    assert_eq!(result.error.unwrap().code(), "OAH_PLAN_STALE");
    assert_eq!(result.receipts.len(), 1);
    let statuses: Vec<_> = result
        .items
        .iter()
        .map(|(_, _, item)| item.status)
        .collect();
    assert_eq!(
        statuses,
        [ItemStatus::Done, ItemStatus::Stale, ItemStatus::NotRun]
    );
    assert_eq!(fs::read(root.join("CLAUDE.md")).unwrap(), b"user edit\n");
    assert!(!root.join("GEMINI.md").exists());
}

#[test]
fn a_drift_check_reports_hand_edits_and_thin_files_and_writes_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let config = config_at(root);
    let plan = plan_sync(
        &RulesSnapshot::read(root, &[]).unwrap(),
        &input_for(&config),
    )
    .unwrap();
    assert!(apply_sync(&plan, &CancelToken::new(), LockOptions::default()).synced());
    let agents = fs::read_to_string(root.join("AGENTS.md")).unwrap();
    fs::write(
        root.join("AGENTS.md"),
        agents.replace("| Codex |", "| Someone |"),
    )
    .unwrap();
    let before = tree(root);
    let (_, drift) = check_sync(
        &RulesSnapshot::read(root, &[AgentId::Gemini]).unwrap(),
        &input_for(&config),
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
    let config = config_at(root);
    let error = plan_sync(
        &RulesSnapshot::read(root, &[AgentId::Cursor]).unwrap(),
        &input_for(&config),
    )
    .unwrap_err();
    assert_eq!(error.code(), "OAH_RULES_LINK");
    assert!(fs::read_dir(outside.path()).unwrap().next().is_none());
    assert!(!root.join("AGENTS.md").exists());
}
