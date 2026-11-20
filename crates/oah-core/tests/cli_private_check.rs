// CLI 비공개 확인 시험: 시작은 OAH_HOME 의 설치본이나 임시 사본을 실행하지 않고, 확인에 실패하면 OAH_HOME 에 아무것도 쓰지 않으며, 비공개 사본을 지운다.
//! Desktop starts of the CLI copy and its private check (CONTRACTS C7), with an inert shell script as the bundled
//! CLI. The script records where it ran and prints its line only from a private `oah-validate.*` folder; anywhere
//! else it exits 3, so a start that ran the installed copy or its temporary sibling fails.
#![cfg(unix)]

use oah_core::app::{self, CliCopy, Session};
use oah_core::dto::CliState;
use oah_core::paths::Env;
use oah_core::tasks::{CancelToken, NoProgress};
use std::fs;
use std::path::{Path, PathBuf};

/// A canonical root holding `home` (so `OAH_HOME` is `home/.oah`) and `bundle/oah`, a script printing `printed`.
fn setup(printed: &str) -> (tempfile::TempDir, PathBuf, Session) {
    let dir = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(dir.path()).unwrap();
    let mut env = Env::new(&root).unwrap();
    env.set("HOME", root.join("home"));
    env.set("OAH_TEST_RAN", root.join("ran.log"));
    let script = format!(
        "#!/bin/sh\necho \"$0\" >> \"$OAH_TEST_RAN\"\nfolder=${{0%/*}}\n\
         case ${{folder##*/}} in\n  oah-validate.*) echo '{printed}' ;;\n  *) exit 3 ;;\nesac\n"
    );
    fs::create_dir_all(root.join("bundle")).unwrap();
    fs::write(root.join("bundle").join("oah"), script).unwrap();
    (dir, root, Session::new(env))
}

fn start(session: &Session, root: &Path) -> CliCopy {
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    app::cli_copy(&ctx, &root.join("bundle").join("oah"))
}

fn dest(root: &Path) -> PathBuf {
    root.join("home").join(".oah").join("bin").join("oah")
}

/// How many times the script ran since the last call. Each run must have been a private copy outside the root
/// whose folder is gone again.
fn private_runs(root: &Path) -> usize {
    let log = root.join("ran.log");
    let ran = fs::read_to_string(&log).unwrap_or_default();
    let _ = fs::remove_file(&log);
    for line in ran.lines() {
        let folder = Path::new(line).parent().unwrap();
        let name = folder.file_name().unwrap().to_string_lossy();
        assert!(
            name.starts_with("oah-validate."),
            "{line} is not a private copy"
        );
        assert!(!folder.starts_with(root), "{line} ran inside the test root");
        assert!(!folder.exists(), "{line}: the private folder is removed");
    }
    ran.lines().count()
}

#[test]
fn first_and_repeated_starts_run_only_private_copies() {
    let (_dir, root, session) = setup(&format!("oah {}", oah_core::VERSION));
    let first = start(&session, &root);
    assert_eq!(first.state, CliState::Copied, "{:?}", first.notice);
    assert_eq!(first.version.as_deref(), Some(oah_core::VERSION));
    assert_eq!(private_runs(&root), 1);
    let bundled = fs::read(root.join("bundle").join("oah")).unwrap();
    assert_eq!(fs::read(dest(&root)).unwrap(), bundled);
    let modified = fs::metadata(dest(&root)).unwrap().modified().unwrap();
    let again = start(&session, &root);
    assert_eq!(again.state, CliState::Unchanged, "{:?}", again.notice);
    assert_eq!(again.version.as_deref(), Some(oah_core::VERSION));
    assert_eq!(
        private_runs(&root),
        1,
        "only the private copy runs, never the installed one"
    );
    let after = fs::metadata(dest(&root)).unwrap().modified().unwrap();
    assert_eq!(after, modified);
}

#[test]
fn an_upgrade_publishes_the_checked_bytes_without_running_the_sibling() {
    let (_dir, root, session) = setup(&format!("oah {}", oah_core::VERSION));
    fs::create_dir_all(dest(&root).parent().unwrap()).unwrap();
    fs::write(dest(&root), "older inert copy").unwrap();
    let copy = start(&session, &root);
    assert_eq!(copy.state, CliState::Copied, "{:?}", copy.notice);
    assert_eq!(private_runs(&root), 1);
    let bundled = fs::read(root.join("bundle").join("oah")).unwrap();
    assert_eq!(fs::read(dest(&root)).unwrap(), bundled);
    let names: Vec<_> = fs::read_dir(dest(&root).parent().unwrap())
        .unwrap()
        .collect();
    assert_eq!(names.len(), 1, "no temporary sibling is left");
}

#[test]
fn a_failed_check_writes_nothing_under_oah_home() {
    let (_dir, root, session) = setup("oah 9.9.9");
    let copy = start(&session, &root);
    assert_eq!(copy.state, CliState::KeptPrevious);
    assert_eq!(copy.version, None);
    let notice = copy.notice.unwrap();
    assert_eq!(notice.code, "OAH_CLI_COPY_FAILED");
    assert!(
        notice.message.contains("(the --version check)"),
        "{}",
        notice.message
    );
    assert_eq!(private_runs(&root), 1);
    assert!(
        !root.join("home").join(".oah").exists(),
        "nothing is written under OAH_HOME"
    );
}
