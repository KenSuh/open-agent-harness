// 코어 C1 수정 검사: 실제 게시 전 취소는 아무것도 쓰지 않고, 같은 바이트라도 못 쓰는 CLI 는 알리고, 낡은 읽기는 새 선택을 덮지 않는다.
use oah_core::app::{self, Session};
use oah_core::config::{Edit, UserSettings, save_settings};
use oah_core::dto::{CliState, Empty, ProjectDto, RefreshDto};
use oah_core::fsx::LockOptions;
use oah_core::paths::{self, Env};
use oah_core::plan::{self, Action, Basis, ChangePlan, ItemStatus, Outcome, Owner, PlanItem};
use oah_core::tasks::{CancelToken, NoProgress, Progress, ProgressSink};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Barrier;

const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };

/// A canonical temporary root; the folder is removed when the value drops.
fn root() -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let root = paths::canonical_folder(&fs::canonicalize(dir.path()).unwrap()).unwrap();
    (dir, root)
}

/// A plan of `(id, target, action)` items whose basis is every target as it is now.
fn plan_of(items: &[(&str, &Path, Action)]) -> ChangePlan {
    let items: Vec<PlanItem> = items
        .iter()
        .map(|&(id, target, action)| PlanItem {
            id: id.to_string(),
            target: target.to_path_buf(),
            owner: Owner::User,
            action,
            summary: id.to_string(),
            detail: None,
        })
        .collect();
    let basis = items
        .iter()
        .map(|item| Basis::observe(&item.target).unwrap())
        .collect();
    ChangePlan {
        id: String::from("core-c1"),
        items,
        warnings: Vec::new(),
        basis,
    }
}

// CORE-C1: a writer that returned `done` with no receipt published nothing, so a cancellation after it still
// stops the apply before the next target; after a real publish the apply finishes (CONTRACTS C4).

/// The writers that finish a target without publishing anything.
#[derive(Debug, Clone, Copy)]
enum NoOp {
    /// An accepted `keep` item: its writer has nothing to write.
    Keep,
    /// A `config` writer whose edit sets the value the file already holds.
    UnchangedSettings,
}

const SETTINGS: &str = "[ui]\ntheme = \"dark\"\n";

/// Applies `[noop, write]`; the no-op writer asks for cancellation, the second writer really writes.
fn apply_after_noop(noop: NoOp) {
    let (_dir, root) = root();
    let [settings, later] = ["settings.toml", "later.txt"].map(|name| root.join(name));
    fs::write(&settings, SETTINGS).unwrap();
    fs::write(&later, "before").unwrap();
    let snapshot = UserSettings::read(&settings).unwrap();
    let planned = plan_of(&[
        ("noop", &settings, Action::Keep),
        ("write", &later, Action::Update),
    ]);
    let cancel = CancelToken::new();
    let lock = LockOptions::default();
    let result = plan::apply(&planned.accept(&["noop", "write"]), &cancel, |target| {
        if target.path != settings {
            return plan::write_target(target, b"after", lock);
        }
        cancel.cancel();
        match noop {
            NoOp::Keep => Outcome::Done(Vec::new()),
            NoOp::UnchangedSettings => {
                let edits = [Edit::set(&["ui", "theme"], "dark")];
                plan::config_outcome(save_settings(&snapshot, &edits, lock))
            }
        }
    });
    assert_eq!(fs::read_to_string(&settings).unwrap(), SETTINGS, "{noop:?}");
    assert_eq!(fs::read(&later).unwrap(), b"before", "{noop:?}: {result:?}");
    assert!(result.is_err_and(|error| error.is_cancelled()), "{noop:?}");
}

#[test]
fn a_cancellation_after_a_writer_that_published_nothing_stops_before_the_next_target() {
    for noop in [NoOp::Keep, NoOp::UnchangedSettings] {
        apply_after_noop(noop);
    }
}

#[test]
fn a_cancellation_before_the_first_target_writes_nothing() {
    let (_dir, root) = root();
    let target = root.join("a.txt");
    fs::write(&target, "before").unwrap();
    let planned = plan_of(&[("a", &target, Action::Update)]);
    let cancel = CancelToken::new();
    cancel.cancel();
    let result = plan::apply(&planned.accept(&["a"]), &cancel, |target| {
        plan::write_target(target, b"after", LockOptions::default())
    });
    assert!(result.is_err_and(|error| error.is_cancelled()));
    assert_eq!(fs::read(&target).unwrap(), b"before");
}

#[test]
fn a_cancellation_after_a_real_publish_lets_the_apply_finish_as_done() {
    let (_dir, root) = root();
    let [first, second] = ["first.txt", "second.txt"].map(|name| root.join(name));
    fs::write(&first, "f0").unwrap();
    fs::write(&second, "s0").unwrap();
    let planned = plan_of(&[
        ("first", &first, Action::Update),
        ("second", &second, Action::Update),
    ]);
    let cancel = CancelToken::new();
    let report = plan::apply(&planned.accept(&["first", "second"]), &cancel, |target| {
        let outcome = plan::write_target(target, b"new", LockOptions::default());
        cancel.cancel();
        outcome
    })
    .unwrap();
    assert!(report.stop.is_none(), "{:?}", report.stop);
    assert_eq!(report.status_of("first"), Some(ItemStatus::Done));
    assert_eq!(report.status_of("second"), Some(ItemStatus::Done));
    assert_eq!(report.receipts.len(), 2);
    assert_eq!(fs::read(&second).unwrap(), b"new");
}

// CORE-C2: bytes already in place pass the same `--version` validation as a new candidate; an installed copy that
// fails it is reported, kept byte for byte and never repaired behind the user's back (CONTRACTS C7).

#[cfg(unix)]
mod cli_copy {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// A session, a bundled CLI printing `printed`, and the same bytes already installed with `mode`.
    fn installed(printed: &str, mode: u32) -> (tempfile::TempDir, Session, PathBuf, PathBuf) {
        let (dir, root) = root();
        let home = root.join("home");
        let mut env = Env::new(&root).unwrap();
        env.set(HOME_VAR, &home);
        let bundled = root.join("bundle").join("oah");
        let dest = home.join(".oah").join("bin").join("oah");
        let bytes = format!("#!/bin/sh\necho '{printed}'\n");
        for (path, mode) in [(&bundled, 0o755), (&dest, mode)] {
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, &bytes).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
        }
        (dir, Session::new(env), bundled, dest)
    }

    #[test]
    fn identical_bytes_that_fail_the_version_check_keep_the_copy_and_raise_a_notice() {
        let current = format!("oah {}", oah_core::VERSION);
        for (printed, mode) in [(current.as_str(), 0o644), ("oah 9.9.9", 0o755)] {
            let (_dir, session, bundled, dest) = installed(printed, mode);
            let before = fs::read(&dest).unwrap();
            let modified = fs::metadata(&dest).unwrap().modified().unwrap();
            let cancel = CancelToken::new();
            let ctx = session.ctx(&cancel, &NoProgress);
            let copy = app::cli_copy(&ctx, &bundled);
            assert_eq!(copy.state, CliState::KeptPrevious, "{printed}: {copy:?}");
            assert_eq!(copy.version, None);
            let notice = copy.notice.clone().unwrap();
            assert_eq!(notice.code, "OAH_CLI_COPY_FAILED");
            let step = "(checking the installed copy)";
            assert!(notice.message.contains(step), "{}", notice.message);
            assert_eq!(fs::read(&dest).unwrap(), before);
            let metadata = fs::metadata(&dest).unwrap();
            assert_eq!(metadata.permissions().mode() & 0o777, mode, "never chmod");
            assert_eq!(metadata.modified().unwrap(), modified);
            let info = app::app_info(&ctx, Empty {}).unwrap();
            assert_eq!(info.notices, [notice]);
        }
    }
}

// CORE-C3: a refresh re-reads the selection it started from and never publishes over a newer selection, even when
// the user went A, then B, then back to A meanwhile.

/// Holds the refresh at its first progress report, after it took its snapshot and before it reads the project.
struct PauseAtStart(Barrier);

impl ProgressSink for PauseAtStart {
    fn progress(&self, progress: Progress) {
        if progress.done == 0 {
            self.0.wait();
            self.0.wait();
        }
    }
}

/// What both refreshes report: the one project the session holds, which has no notice.
const HELD: RefreshDto = RefreshDto {
    projects: 1,
    notices: 0,
};

/// A session whose home folder holds the empty project folders `a` and `b`.
fn projects() -> (tempfile::TempDir, Session, PathBuf, PathBuf) {
    let (dir, root) = root();
    let home = root.join("home");
    let [a, b] = ["a", "b"].map(|name| home.join(name));
    for folder in [&a, &b] {
        fs::create_dir_all(folder).unwrap();
    }
    let mut env = Env::new(&root).unwrap();
    let mut names = vec!["PATH"];
    if cfg!(windows) {
        names.extend(["SystemRoot", "windir", "ComSpec", "PATHEXT", "TEMP", "TMP"]);
    }
    for name in names {
        if let Some(value) = std::env::var_os(name) {
            env.set(name, value);
        }
    }
    env.set(HOME_VAR, &home);
    env.set("GIT_CONFIG_NOSYSTEM", "1");
    env.set("GIT_CEILING_DIRECTORIES", &root);
    (dir, Session::new(env), a, b)
}

fn open(session: &Session, folder: &Path) -> ProjectDto {
    let cancel = CancelToken::new();
    app::project_open(&session.ctx(&cancel, &NoProgress), folder).unwrap()
}

fn current(session: &Session) -> ProjectDto {
    let cancel = CancelToken::new();
    let current = app::project_current(&session.ctx(&cancel, &NoProgress), Empty {});
    current.unwrap().unwrap()
}

/// Runs a refresh held at its start while `meanwhile` changes the selection, and returns its result.
fn refresh_around(session: &Session, meanwhile: impl FnOnce()) -> RefreshDto {
    let pause = PauseAtStart(Barrier::new(2));
    let cancel = CancelToken::new();
    std::thread::scope(|scope| {
        let refresh = scope.spawn(|| app::refresh(&session.ctx(&cancel, &pause), Empty {}));
        pause.0.wait();
        meanwhile();
        pause.0.wait();
        refresh.join().unwrap().unwrap()
    })
}

/// How a project folder below the home folder is shown.
fn shown(name: &str) -> String {
    Path::new("~").join(name).display().to_string()
}

#[test]
fn a_refresh_that_started_before_another_project_was_opened_keeps_the_newer_selection() {
    let (_dir, session, a, b) = projects();
    assert_eq!(open(&session, &a).root, shown("a"));
    let refreshed = refresh_around(&session, || {
        assert_eq!(open(&session, &b).root, shown("b"));
    });
    assert_eq!(current(&session).root, shown("b"));
    assert_eq!(refreshed, HELD);
}

#[test]
fn a_refresh_that_started_before_a_then_b_then_a_again_does_not_publish_its_read() {
    let (_dir, session, a, b) = projects();
    open(&session, &a);
    let refreshed = refresh_around(&session, || {
        open(&session, &b);
        assert!(open(&session, &a).notices.is_empty());
        // Only the paused refresh can now read this unknown key; the reopened A was read before it.
        fs::create_dir_all(a.join(".oah")).unwrap();
        let config = "[models]\nprofil = \"efficient\"\n";
        fs::write(a.join(".oah").join("config.toml"), config).unwrap();
    });
    let held = current(&session);
    assert_eq!(held.root, shown("a"));
    assert!(held.notices.is_empty(), "the old refresh won: {held:?}");
    assert_eq!(refreshed, HELD);
}
