// 계획 적용 시나리오: 사전 확인의 낡음은 한 바이트도 쓰지 않고, 첫 게시 뒤 낡음은 항목별로 정직하게 보고하며, 잠금은 대상 하나씩만 잡는다.
use oah_core::config::{
    Edit, ProjectConfig, UserSettings, codes as config_codes, save_project, save_settings,
};
use oah_core::error::codes;
use oah_core::fsx::{FileLock, LockOptions};
use oah_core::plan::{
    self, Action, Basis, ChangePlan, ItemStatus, Outcome, Owner, PlanItem, Target,
};
use oah_core::tasks::CancelToken;
use oah_core::{Coded, Error};
use std::cell::RefCell;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

fn quick() -> LockOptions {
    LockOptions {
        wait: Duration::from_millis(200),
        poll: Duration::from_millis(10),
    }
}

fn item(id: &str, target: &Path) -> PlanItem {
    PlanItem {
        id: id.to_string(),
        target: target.to_path_buf(),
        owner: Owner::Project,
        action: Action::Update,
        summary: format!("update {id}"),
        detail: None,
    }
}

fn plan_of(items: Vec<PlanItem>, basis: &[&Path]) -> ChangePlan {
    ChangePlan {
        id: String::from("plan-1"),
        items,
        warnings: Vec::new(),
        basis: basis
            .iter()
            .map(|path| Basis::observe(path).unwrap())
            .collect(),
    }
}

struct Abc {
    _dir: tempfile::TempDir,
    a: PathBuf,
    b: PathBuf,
    c: PathBuf,
}

fn abc() -> Abc {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let [a, b, c] = ["a.txt", "b.txt", "c.txt"].map(|name| root.join(name));
    for (path, text) in [(&a, "a0"), (&b, "b0"), (&c, "c0")] {
        fs::write(path, text).unwrap();
    }
    Abc { _dir: dir, a, b, c }
}

fn three_item_plan(files: &Abc) -> ChangePlan {
    plan_of(
        vec![
            item("a", &files.a),
            item("b", &files.b),
            item("c", &files.c),
        ],
        &[&files.a, &files.b, &files.c],
    )
}

#[test]
fn a_missing_basis_prevents_every_accepted_write() {
    let files = abc();
    let plan = plan_of(vec![item("a", &files.a), item("b", &files.b)], &[&files.a]);
    let accepted = plan.accept(&["a", "b"]);

    let error = plan::apply(&accepted, &CancelToken::new(), |_| {
        panic!("preflight must stop before calling a writer")
    })
    .unwrap_err();

    assert_eq!(error.code(), codes::PLAN_STALE);
    assert!(!lock_file(&files.a).exists());
    assert!(!lock_file(&files.b).exists());
}

fn lock_file(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap().to_os_string();
    name.push(".lock");
    path.with_file_name(name)
}

#[test]
fn a_basis_that_moved_before_apply_is_stale_and_nothing_is_written() {
    let files = abc();
    let plan = three_item_plan(&files);
    fs::write(&files.b, "b changed by someone else").unwrap();
    let calls = RefCell::new(0);
    let result = plan::apply(
        &plan.accept(&["a", "b", "c"]),
        &CancelToken::new(),
        |target| {
            *calls.borrow_mut() += 1;
            plan::write_target(target, b"new", quick())
        },
    );
    let Err(error) = result else {
        panic!("an initial preflight that finds a moved basis must refuse the whole plan");
    };
    assert_eq!(error.code(), codes::PLAN_STALE);
    assert_eq!(*calls.borrow(), 0, "no writer may run");
    assert_eq!(fs::read(&files.a).unwrap(), b"a0");
    assert_eq!(fs::read(&files.b).unwrap(), b"b changed by someone else");
    assert_eq!(fs::read(&files.c).unwrap(), b"c0");
}

#[test]
fn a_basis_that_moves_after_the_first_publish_stops_with_honest_item_results() {
    let files = abc();
    let plan = three_item_plan(&files);
    let report = plan::apply(
        &plan.accept(&["a", "b", "c"]),
        &CancelToken::new(),
        |target| {
            let outcome = plan::write_target(target, b"new", quick());
            if target.path == files.a {
                fs::write(&files.b, "b changed after a").unwrap();
            }
            outcome
        },
    )
    .unwrap();
    assert_eq!(report.status_of("a"), Some(ItemStatus::Done));
    assert_eq!(report.status_of("b"), Some(ItemStatus::Stale));
    assert_eq!(report.status_of("c"), Some(ItemStatus::NotRun));
    assert_eq!(
        report.stop.as_ref().map(Error::code),
        Some(codes::PLAN_STALE)
    );
    assert_eq!(report.receipts.len(), 1);
    assert_eq!(report.receipts[0].path(), files.a.as_path());
    assert_eq!(fs::read(&files.a).unwrap(), b"new");
    assert_eq!(fs::read(&files.b).unwrap(), b"b changed after a");
    assert_eq!(fs::read(&files.c).unwrap(), b"c0");
}

#[test]
fn no_lock_of_another_target_is_held_while_a_target_is_written() {
    let files = abc();
    let plan = three_item_plan(&files);
    let report = plan::apply(
        &plan.accept(&["a", "b", "c"]),
        &CancelToken::new(),
        |target| {
            for other in [&files.a, &files.b, &files.c] {
                if other != target.path {
                    assert!(!lock_file(other).exists(), "{} is locked", other.display());
                }
            }
            plan::write_target(target, b"new", quick())
        },
    )
    .unwrap();
    assert!(report.stop.is_none());
    for path in [&files.a, &files.b, &files.c] {
        assert!(!lock_file(path).exists());
    }
}

#[test]
fn a_project_config_and_settings_written_by_their_own_locking_writers_both_complete() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let config = root.join("app").join(".oah").join("config.toml");
    let settings = root.join("home").join(".oah").join("settings.toml");
    fs::create_dir_all(config.parent().unwrap()).unwrap();
    fs::create_dir_all(settings.parent().unwrap()).unwrap();
    fs::write(&config, "# team file\n[stages]\nplan = \"claude\"\n").unwrap();
    fs::write(
        &settings,
        "# mine\n[automation]\nlevel = \"recommend-only\"\n",
    )
    .unwrap();
    let project_snapshot = ProjectConfig::read(&config).unwrap();
    let settings_snapshot = UserSettings::read(&settings).unwrap();
    let mut user_item = item("theme", &settings);
    user_item.owner = Owner::User;
    let plan = plan_of(
        vec![item("stage", &config), user_item],
        &[&config, &settings],
    );
    let report = plan::apply(
        &plan.accept(&["stage", "theme"]),
        &CancelToken::new(),
        |target| {
            if target.path == config {
                let edits = [Edit::set(&["stages", "build"], "codex")];
                plan::config_outcome(save_project(&project_snapshot, &edits, None, quick()))
            } else {
                let edits = [Edit::set(&["ui", "theme"], "dark")];
                plan::config_outcome(save_settings(&settings_snapshot, &edits, quick()))
            }
        },
    )
    .unwrap();
    assert!(report.stop.is_none(), "{:?}", report.stop);
    assert_eq!(report.status_of("stage"), Some(ItemStatus::Done));
    assert_eq!(report.status_of("theme"), Some(ItemStatus::Done));
    assert_eq!(report.receipts.len(), 2);
    let saved = ProjectConfig::read(&config).unwrap();
    assert!(
        saved
            .file()
            .text()
            .starts_with("# team file\n[stages]\nplan = \"claude\"\n")
    );
    assert_eq!(
        UserSettings::read(&settings).unwrap().values().ui.theme,
        "dark"
    );
}

#[test]
fn two_edits_to_one_target_are_published_once() {
    let files = abc();
    let plan = plan_of(
        vec![
            item("a1", &files.a),
            item("b", &files.b),
            item("a2", &files.a),
        ],
        &[&files.a, &files.b],
    );
    let mut calls: Vec<(PathBuf, Vec<String>)> = Vec::new();
    let report = plan::apply(
        &plan.accept(&["a1", "a2", "b"]),
        &CancelToken::new(),
        |target| {
            let ids: Vec<String> = target.items.iter().map(|item| item.id.clone()).collect();
            calls.push((target.path.to_path_buf(), ids.clone()));
            plan::write_target(target, ids.join("+").as_bytes(), quick())
        },
    )
    .unwrap();
    assert_eq!(
        calls,
        [
            (
                files.a.clone(),
                vec![String::from("a1"), String::from("a2")]
            ),
            (files.b.clone(), vec![String::from("b")]),
        ]
    );
    assert_eq!(fs::read(&files.a).unwrap(), b"a1+a2");
    assert_eq!(report.receipts.len(), 2);
    let ids: Vec<&str> = report.items.iter().map(|item| item.id.as_str()).collect();
    assert_eq!(ids, ["a1", "b", "a2"], "results follow the plan order");
}

#[test]
fn only_accepted_items_are_written() {
    let files = abc();
    let plan = three_item_plan(&files);
    let report = plan::apply(
        &plan.accept(&["c", "a", "not-in-plan"]),
        &CancelToken::new(),
        |target| plan::write_target(target, b"new", quick()),
    )
    .unwrap();
    let ids: Vec<&str> = report.items.iter().map(|item| item.id.as_str()).collect();
    assert_eq!(ids, ["a", "c"]);
    assert_eq!(fs::read(&files.b).unwrap(), b"b0");
    assert_eq!(fs::read(&files.c).unwrap(), b"new");
}

#[test]
fn a_target_lock_that_stays_busy_fails_that_item_and_stops_the_rest() {
    let files = abc();
    let plan = three_item_plan(&files);
    let held = FileLock::acquire(&files.b, LockOptions::default()).unwrap();
    let report = plan::apply(
        &plan.accept(&["a", "b", "c"]),
        &CancelToken::new(),
        |target| plan::write_target(target, b"new", quick()),
    )
    .unwrap();
    held.release().unwrap();
    assert_eq!(report.status_of("a"), Some(ItemStatus::Done));
    assert_eq!(report.status_of("b"), Some(ItemStatus::Failed));
    assert_eq!(report.status_of("c"), Some(ItemStatus::NotRun));
    assert_eq!(report.stop.as_ref().map(Error::code), Some("OAH_FILE_BUSY"));
    assert_eq!(fs::read(&files.b).unwrap(), b"b0");
}

#[test]
fn a_config_writer_that_finds_its_file_changed_is_stale() {
    let dir = tempfile::tempdir().unwrap();
    let settings = dir.path().canonicalize().unwrap().join("settings.toml");
    fs::write(&settings, "[ui]\ntheme = \"light\"\n").unwrap();
    let snapshot = UserSettings::read(&settings).unwrap();
    fs::write(&settings, "[ui]\ntheme = \"auto\"\n").unwrap();
    let result = save_settings(&snapshot, &[Edit::set(&["ui", "theme"], "dark")], quick());
    assert!(matches!(&result, Err(error) if error.code() == config_codes::CHANGED));
    assert!(matches!(plan::config_outcome(result), Outcome::Stale));
}

#[test]
fn a_writer_outcome_after_its_publish_is_unknown_and_stops_the_rest() {
    let files = abc();
    let plan = three_item_plan(&files);
    let report = plan::apply(
        &plan.accept(&["a", "b", "c"]),
        &CancelToken::new(),
        |target: &Target<'_>| {
            if target.path == files.b {
                let error: Error = oah_core::project::ProjectError::NotFolder {
                    path: files.b.clone(),
                }
                .into();
                Outcome::Unknown(error)
            } else {
                plan::write_target(target, b"new", quick())
            }
        },
    )
    .unwrap();
    assert_eq!(report.status_of("a"), Some(ItemStatus::Done));
    assert_eq!(report.status_of("b"), Some(ItemStatus::Unknown));
    assert_eq!(report.status_of("c"), Some(ItemStatus::NotRun));
    assert_eq!(fs::read(&files.c).unwrap(), b"c0");
}
