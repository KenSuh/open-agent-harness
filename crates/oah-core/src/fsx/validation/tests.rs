// 비공개 확인 단위 시험: 격리된 위치와 실제 사용자별 위치에서 사본·정리·거부 동작을 확인한다.
use super::*;
use std::fs;
use std::path::PathBuf;

/// A canonical temporary folder holding an empty `staging` location, and a separate `home` (`OAH_HOME`).
fn roots() -> (tempfile::TempDir, PathBuf, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(dir.path()).unwrap();
    let staging = root.join("staging");
    fs::create_dir(&staging).unwrap();
    (dir, staging, root.join("home"))
}

fn entries(folder: &Path) -> Vec<PathBuf> {
    let mut names: Vec<PathBuf> = fs::read_dir(folder)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    names.sort();
    names
}

/// Checks what the check sees: its own copy of the bytes, alone in a new `oah-validate.*` folder of `base`, owned
/// by this user and closed to everyone else.
fn private_copy_seen(base: &Path, copy: &Path) -> PathBuf {
    assert_eq!(fs::read(copy).unwrap(), b"inert bytes");
    let folder = copy.parent().unwrap();
    assert_eq!(folder.parent().unwrap(), base);
    let name = folder.file_name().unwrap().to_string_lossy();
    assert!(name.starts_with("oah-validate."), "{name}");
    assert_eq!(entries(folder), [copy.to_path_buf()]);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let metadata = fs::symlink_metadata(folder).unwrap();
        assert_eq!(metadata.permissions().mode() & 0o7777, 0o700);
        assert_eq!(metadata.uid(), rustix::process::geteuid().as_raw());
        let mode = fs::symlink_metadata(copy).unwrap().permissions().mode();
        assert_eq!(mode & 0o7777, 0o700);
    }
    #[cfg(windows)]
    platform::assert_private_copy_owner(folder, copy);
    copy.to_path_buf()
}

#[test]
fn an_accepted_check_sees_only_a_private_copy_which_is_then_removed() {
    let (_dir, staging, home) = roots();
    let mut seen = None;
    let result = validate_in(&staging, &home, OsStr::new("oah"), b"inert bytes", |copy| {
        seen = Some(private_copy_seen(&staging, copy));
        Ok::<_, ()>(7)
    });
    assert_eq!(result.unwrap(), Ok(7));
    assert!(!seen.unwrap().exists());
    assert!(
        entries(&staging).is_empty(),
        "the private folder is removed"
    );
}

#[test]
fn a_rejected_check_is_returned_and_its_private_copy_removed() {
    let (_dir, staging, home) = roots();
    let result = validate_in(&staging, &home, OsStr::new("oah"), b"inert", |_| {
        Err::<(), _>("no")
    });
    assert_eq!(result.unwrap(), Err("no"));
    assert!(entries(&staging).is_empty());
}

#[test]
fn a_location_in_oah_home_is_refused_before_anything_is_written() {
    let (_dir, staging, _home) = roots();
    let inside = staging.join("tmp");
    fs::create_dir(&inside).unwrap();
    for home in [&staging, &inside] {
        let mut ran = false;
        let refused = validate_in(&inside, home, OsStr::new("oah"), b"inert", |_| {
            ran = true;
            Ok::<_, ()>(())
        });
        let error = refused.unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::NotOwned, "{error}");
        assert!(!ran);
        assert!(entries(&inside).is_empty());
    }
}

#[test]
fn a_relative_or_missing_location_is_refused() {
    let (_dir, staging, home) = roots();
    for path in [PathBuf::from("relative"), staging.join("missing")] {
        let result = validate_in(&path, &home, OsStr::new("oah"), b"inert", |_| {
            Ok::<_, ()>(())
        });
        assert!(result.is_err(), "{}", path.display());
    }
    assert!(entries(&staging).is_empty());
}

#[cfg(unix)]
#[test]
fn a_location_others_can_write_is_refused_unless_it_is_sticky() {
    use std::os::unix::fs::PermissionsExt;
    let (_dir, staging, home) = roots();
    fs::set_permissions(&staging, fs::Permissions::from_mode(0o777)).unwrap();
    let mut ran = false;
    let refused = validate_in(&staging, &home, OsStr::new("oah"), b"inert", |_| {
        ran = true;
        Ok::<_, ()>(())
    });
    let error = refused.unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned, "{error}");
    assert!(!ran);
    assert!(entries(&staging).is_empty());
    fs::set_permissions(&staging, fs::Permissions::from_mode(0o1777)).unwrap();
    let accepted = validate_in(&staging, &home, OsStr::new("oah"), b"inert", |_| {
        Ok::<_, ()>(1)
    });
    assert_eq!(accepted.unwrap(), Ok(1));
    assert!(entries(&staging).is_empty());
}

/// A location below a folder other users can write is refused, though the location itself is private.
#[cfg(unix)]
#[test]
fn a_location_below_a_folder_others_can_write_is_refused() {
    use std::os::unix::fs::PermissionsExt;
    let (_dir, staging, home) = roots();
    let inner = staging.join("inner");
    fs::create_dir(&inner).unwrap();
    fs::set_permissions(&staging, fs::Permissions::from_mode(0o777)).unwrap();
    let refused = validate_in(&inner, &home, OsStr::new("oah"), b"inert", |_| {
        Ok::<_, ()>(())
    });
    let error = refused.unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned, "{error}");
    assert!(error.to_string().contains("staging"), "{error}");
    assert!(entries(&inner).is_empty());
    fs::set_permissions(&staging, fs::Permissions::from_mode(0o700)).unwrap();
}

/// A benign failure during copy creation (invalid file name) still cleans up the verified private folder cleanly.
#[test]
fn a_copy_creation_failure_still_cleans_up_the_verified_private_folder() {
    let (_dir, staging, home) = roots();
    let mut ran = false;
    let bad_name = OsStr::new("nested/invalid");
    let refused = validate_in(&staging, &home, bad_name, b"inert", |_| {
        ran = true;
        Ok::<_, ()>(())
    });
    assert!(refused.is_err());
    assert!(!ran);
    assert!(entries(&staging).is_empty());
}

/// The operating system's per-user location, as a desktop start uses it.
#[test]
fn the_per_user_location_holds_a_private_copy_only_during_the_check() {
    let (_dir, _staging, home) = roots();
    let mut seen = None;
    let result = validate_private_copy(&home, OsStr::new("oah"), b"inert bytes", |copy| {
        let base = platform::location().unwrap().path().to_path_buf();
        seen = Some(private_copy_seen(&base, copy));
        Ok::<_, ()>(())
    });
    assert_eq!(result.unwrap(), Ok(()));
    let copy = seen.unwrap();
    assert!(!copy.exists());
    assert!(
        !copy.parent().unwrap().exists(),
        "the private folder is removed"
    );
}
