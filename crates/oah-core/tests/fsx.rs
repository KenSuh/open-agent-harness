// 파일 기본 동작 시험: 원자적 교체와 되읽기, 덮어쓰지 않는 게시, 짧은 쓰기 잠금의 경합·대기·해제, 자기 쓰기 기록의 일치·불일치를 확인한다.
use oah_core::fsx::{
    FileLock, Fingerprint, FsxErrorKind, LockOptions, SelfWriteMatch, SelfWrites, publish_new,
    read_optional, write_atomic,
};
use std::fs;
use std::path::Path;
use std::sync::mpsc;
use std::time::{Duration, Instant};

fn names(folder: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(folder)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

fn short(wait_ms: u64) -> LockOptions {
    LockOptions {
        wait: Duration::from_millis(wait_ms),
        poll: Duration::from_millis(10),
    }
}

#[test]
fn an_atomic_write_creates_then_replaces_with_verified_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("data.txt");
    let receipt = write_atomic(&path, b"first").unwrap();
    assert_eq!(receipt.path(), path);
    assert_eq!(receipt.len(), 5);
    assert_eq!(*receipt.fingerprint(), Fingerprint::of(b"first"));
    let receipt = write_atomic(&path, b"second, longer").unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"second, longer");
    assert_eq!(*receipt.fingerprint(), Fingerprint::of(b"second, longer"));
    let receipt = write_atomic(&path, b"").unwrap();
    assert!(receipt.is_empty());
    assert_eq!(
        names(dir.path()),
        ["data.txt"],
        "temporary files were left behind"
    );
}

#[test]
fn a_failed_replacement_keeps_the_destination_and_removes_its_temporary_file() {
    let dir = tempfile::tempdir().unwrap();
    let occupied = dir.path().join("occupied");
    fs::create_dir(&occupied).unwrap();
    fs::write(occupied.join("inside.txt"), "kept").unwrap();
    let error = write_atomic(&occupied, b"new").unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::Io);
    assert_eq!(error.code(), "OAH_FILE_IO");
    assert!(occupied.is_dir());
    assert_eq!(
        fs::read_to_string(occupied.join("inside.txt")).unwrap(),
        "kept"
    );
    assert_eq!(
        names(dir.path()),
        ["occupied"],
        "the temporary file was not cleaned up"
    );
}

#[test]
fn an_atomic_write_never_creates_its_folder() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("missing").join("data.txt");
    let error = write_atomic(&path, b"x").unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::Io);
    assert!(!dir.path().join("missing").exists());
    assert_eq!(
        write_atomic(Path::new("relative.txt"), b"x")
            .unwrap_err()
            .kind(),
        FsxErrorKind::Io
    );
}

#[test]
fn publishing_a_new_file_never_overwrites() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("backup.bak");
    let receipt = publish_new(&path, b"original").unwrap();
    assert_eq!(*receipt.fingerprint(), Fingerprint::of(b"original"));
    let error = publish_new(&path, b"other").unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::Exists);
    assert_eq!(error.code(), "OAH_FILE_EXISTS");
    assert_eq!(fs::read(&path).unwrap(), b"original");
    assert_eq!(names(dir.path()), ["backup.bak"]);
}

#[test]
fn reading_an_absent_file_is_none_and_creates_nothing() {
    let dir = tempfile::tempdir().unwrap();
    assert_eq!(read_optional(&dir.path().join("absent.txt")).unwrap(), None);
    assert!(names(dir.path()).is_empty());
    let error = read_optional(dir.path()).unwrap_err();
    assert_eq!(
        error.kind(),
        FsxErrorKind::Io,
        "a folder is not a readable file"
    );
}

#[test]
fn a_held_lock_makes_a_second_writer_busy_and_readers_still_read() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("config.toml");
    fs::write(&target, "content").unwrap();
    let held = FileLock::acquire(&target, short(100)).unwrap();
    assert_eq!(held.lock_path(), dir.path().join("config.toml.lock"));
    let started = Instant::now();
    let error = FileLock::acquire(&target, short(150)).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::Busy);
    assert_eq!(error.code(), "OAH_FILE_BUSY");
    assert!(
        started.elapsed() >= Duration::from_millis(150),
        "the retry was not bounded by the wait"
    );
    assert_eq!(
        read_optional(&target).unwrap().as_deref(),
        Some(b"content".as_slice())
    );
    held.release().unwrap();
    assert_eq!(
        names(dir.path()),
        ["config.toml"],
        "the lock file outlived its lock"
    );
}

#[test]
fn a_waiting_writer_gets_the_lock_once_it_is_released() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("settings.toml");
    let held = FileLock::acquire(&target, short(100)).unwrap();
    let (ready, waiting) = mpsc::channel();
    let path = target.clone();
    let worker = std::thread::spawn(move || {
        ready.send(()).unwrap();
        let started = Instant::now();
        let lock = FileLock::acquire(&path, short(5_000)).unwrap();
        let waited = started.elapsed();
        lock.release().unwrap();
        waited
    });
    waiting.recv().unwrap();
    std::thread::sleep(Duration::from_millis(200));
    held.release().unwrap();
    let waited = worker.join().unwrap();
    assert!(
        waited >= Duration::from_millis(100),
        "the second writer did not wait: {waited:?}"
    );
    assert!(!target.exists(), "locking created the target file");
}

#[test]
fn dropping_a_lock_releases_it() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("state.json");
    {
        let _held = FileLock::acquire(&target, short(100)).unwrap();
        assert!(dir.path().join("state.json.lock").exists());
    }
    assert!(names(dir.path()).is_empty());
    FileLock::acquire(&target, short(100))
        .unwrap()
        .release()
        .unwrap();
    let error = FileLock::acquire(&dir.path().join("missing").join("x"), short(50)).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::Io);
}

#[test]
fn only_bytes_this_process_wrote_are_its_own() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("watched.toml");
    let registry = SelfWrites::new();
    let first = write_atomic(&path, b"one").unwrap();
    registry.record(&first);
    let second = write_atomic(&path, b"two").unwrap();
    registry.record(&second);
    assert_eq!(registry.pending(&path), 2);
    assert_eq!(registry.observe(&path, b"two"), SelfWriteMatch::Own);
    assert_eq!(
        registry.pending(&path),
        0,
        "a matching event clears the writes up to it"
    );
    assert_eq!(registry.observe(&path, b"one"), SelfWriteMatch::External);
    assert_eq!(
        registry.observe(&dir.path().join("other.toml"), b"two"),
        SelfWriteMatch::External
    );
}

#[test]
fn different_bytes_are_never_suppressed() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("watched.toml");
    let registry = SelfWrites::new();
    let mine = write_atomic(&path, b"mine").unwrap();
    registry.record(&mine);
    fs::write(&path, b"theirs").unwrap();
    assert_eq!(registry.observe(&path, b"theirs"), SelfWriteMatch::External);
    assert_eq!(
        registry.pending(&path),
        0,
        "after an external change the pending writes are dropped"
    );
    assert_eq!(
        registry.observe(&path, b"mine"),
        SelfWriteMatch::External,
        "an old write must not hide a later external change back to the same bytes"
    );
}

#[test]
fn durable_record_fingerprints_read_back_only_their_hex_form() {
    // A caller keeps fingerprints as text between processes; reading one back is exact and strict.
    let fingerprint = Fingerprint::of(b"synthetic bytes");
    let text = fingerprint.to_hex();
    assert_eq!(Fingerprint::from_hex(&text), Some(fingerprint));
    assert_eq!(
        Fingerprint::from_hex(&Fingerprint::of(b"").to_hex()),
        Some(Fingerprint::of(b""))
    );
    let upper = text.to_uppercase();
    for bad in [
        "",
        &text[..62],
        &format!("{text}0"),
        &format!("{}g", &text[..63]),
        &format!(" {}", &text[1..]),
        upper.as_str(),
    ] {
        assert_eq!(Fingerprint::from_hex(bad), None, "{bad:?}");
    }
}
