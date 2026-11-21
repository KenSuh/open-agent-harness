// 특수 파일 시험: 쓰는 쪽이 없는 실제 FIFO 를 읽기 함수가 막히지 않고 바로 거절하는지, 제한 시간이 있는 하위 프로세스로 확인한다.
use crate::fsx::FsxErrorKind;
use crate::rules::{AGENTS_MD, ProjectFile, RulesFiles};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const SCENARIO: &str = "OAH_RULES_SPECIAL_SCENARIO";
const ROOT: &str = "OAH_RULES_SPECIAL_ROOT";
const READY: &str = "SPECIAL_CHILD_RAN";
const DEADLINE: Duration = Duration::from_secs(20);

#[cfg(unix)]
fn make_fifo(path: &Path) {
    std::fs::create_dir_all(path.parent().expect("a parent")).expect("create folders");
    let status = Command::new("mkfifo")
        .arg(path)
        .status()
        .expect("run mkfifo");
    assert!(status.success(), "mkfifo failed: {status}");
}

/// The child side: runs one scenario against the project in `ROOT`. It only acts when the parent test starts it.
#[test]
fn special_file_child() {
    let (Ok(scenario), Ok(root)) = (std::env::var(SCENARIO), std::env::var(ROOT)) else {
        return;
    };
    let root = Path::new(&root);
    let error = match scenario.as_str() {
        "agents" => ProjectFile::read(root, AGENTS_MD).expect_err("a FIFO is refused"),
        "local" => RulesFiles::read(root).expect_err("a FIFO is refused"),
        other => panic!("unknown scenario {other}"),
    };
    assert_eq!(error.code(), "OAH_FILE_IO", "{error}");
    println!("{READY}");
}

/// Runs the child scenario in its own process, kills it at the deadline and always reaps it.
#[cfg(unix)]
fn run_child(scenario: &str, root: &Path) {
    let mut child = Command::new(std::env::current_exe().expect("this test binary"))
        .args([
            "special::special_file_child",
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(SCENARIO, scenario)
        .env(ROOT, root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("start the child");
    let start = Instant::now();
    let finished = loop {
        if let Some(status) = child.try_wait().expect("poll the child") {
            break Some(status);
        }
        if start.elapsed() >= DEADLINE {
            break None;
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    if finished.is_none() {
        child.kill().expect("kill the blocked child");
    }
    let output = child.wait_with_output().expect("reap the child");
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        finished.is_some(),
        "{scenario}: the read blocked on the FIFO for {DEADLINE:?}\n{stdout}{stderr}"
    );
    assert!(
        output.status.success(),
        "{scenario}: {}\n{stdout}{stderr}",
        output.status
    );
    assert!(
        stdout.contains(READY),
        "{scenario}: the child test did not run\n{stdout}{stderr}"
    );
}

#[cfg(unix)]
#[test]
fn a_fifo_project_file_is_refused_without_blocking() {
    let dir = tempfile::tempdir().expect("a project");
    make_fifo(&dir.path().join(AGENTS_MD));
    run_child("agents", dir.path());
}

#[cfg(unix)]
#[test]
fn a_fifo_local_rules_file_is_refused_without_blocking() {
    let dir = tempfile::tempdir().expect("a project");
    make_fifo(&dir.path().join(".oah/rules.local.md"));
    run_child("local", dir.path());
}

#[cfg(unix)]
#[test]
fn a_file_or_folder_where_a_folder_or_file_is_needed_is_an_error_not_a_hang() {
    let dir = tempfile::tempdir().expect("a project");
    std::fs::write(dir.path().join(".oah"), b"a file, not a folder").expect("write");
    let error = ProjectFile::read(dir.path(), ".oah/rules.local.md").expect_err("not a folder");
    assert_eq!(error.code(), "OAH_FILE_IO", "{error}");
    std::fs::create_dir(dir.path().join(AGENTS_MD)).expect("a folder named like the file");
    let error = ProjectFile::read(dir.path(), AGENTS_MD).expect_err("not a file");
    assert_eq!(error.code(), "OAH_FILE_IO", "{error}");
}

/// An unsupported entry is refused from the path alone: the typed error carries the stable kind and the entry's
/// own path and no operating-system error, so no other read was made to produce it.
#[cfg(unix)]
#[test]
fn an_unsupported_entry_is_refused_with_a_typed_error_built_from_its_path() {
    let dir = tempfile::tempdir().expect("a project");
    let agents = dir.path().join(AGENTS_MD);
    std::fs::create_dir(&agents).expect("a folder named like the file");
    let error = ProjectFile::read(dir.path(), AGENTS_MD).expect_err("not a file");
    assert_eq!(error.kind(), FsxErrorKind::Io, "{error}");
    assert_eq!(error.path(), agents, "{error}");
    assert!(error.io_error().is_none(), "built from the path: {error}");

    std::fs::remove_dir(&agents).expect("remove the folder so the local rules are read");
    let local = dir.path().join(".oah/rules.local.md");
    std::fs::create_dir_all(&local).expect("a folder named like the local rules");
    let error = RulesFiles::read(dir.path()).expect_err("not a file");
    assert_eq!(error.kind(), FsxErrorKind::Io, "{error}");
    assert_eq!(error.path(), local, "{error}");
    assert!(error.io_error().is_none(), "built from the path: {error}");
}
