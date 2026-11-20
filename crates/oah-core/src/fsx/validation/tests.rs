// 비공개 확인 단위 시험: 격리된 위치와 실제 사용자별 위치에서 사본·정리·거부를, 그리고 순수 판정 규칙을 확인한다.
use super::rules::{
    ALLOW_ACE_TYPE, Entry, Security, Who, location_rule, private_folder_rule, trusted_location_rule,
};
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

/// The operating system's per-user location, as a desktop start uses it.
#[test]
fn the_per_user_location_holds_a_private_copy_only_during_the_check() {
    let (_dir, _staging, home) = roots();
    let mut seen = None;
    let result = validate_private_copy(&home, OsStr::new("oah"), b"inert bytes", |copy| {
        let base = fs::canonicalize(platform::location().unwrap()).unwrap();
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

#[test]
fn the_location_rule_admits_only_root_or_this_user_and_sticky_shared_folders() {
    assert_eq!(location_rule(true, 0, 501, false, false), Ok(()));
    assert_eq!(location_rule(true, 501, 501, false, false), Ok(()));
    assert_eq!(location_rule(true, 0, 501, true, true), Ok(()));
    assert_eq!(location_rule(true, 501, 501, true, true), Ok(()));
    assert!(location_rule(false, 501, 501, false, false).is_err());
    assert!(location_rule(true, 502, 501, false, false).is_err());
    assert!(location_rule(true, 502, 501, true, true).is_err());
    assert!(location_rule(true, 0, 501, true, false).is_err());
    assert!(location_rule(true, 501, 501, true, false).is_err());
}

fn security(owner: Who, protected: bool, entries: &[(u8, bool, Who)]) -> Security {
    let entries = entries.iter().map(|&(kind, inherit_only, who)| Entry {
        kind,
        inherit_only,
        who,
    });
    Security {
        owner,
        dacl_present: true,
        protected,
        entries: entries.collect(),
    }
}

#[test]
fn the_private_folder_rule_admits_only_an_owned_protected_list_for_this_user() {
    let mine = [(ALLOW_ACE_TYPE, false, Who::User)];
    assert_eq!(
        private_folder_rule(&security(Who::User, true, &mine)),
        Ok(())
    );
    let with_deny = [
        (0x01, false, Who::Other),
        (ALLOW_ACE_TYPE, false, Who::User),
    ];
    assert_eq!(
        private_folder_rule(&security(Who::User, true, &with_deny)),
        Ok(())
    );
    assert!(private_folder_rule(&security(Who::Trusted, true, &mine)).is_err());
    assert!(private_folder_rule(&security(Who::User, false, &mine)).is_err());
    let mut missing = security(Who::User, true, &mine);
    missing.dacl_present = false;
    assert!(private_folder_rule(&missing).is_err());
    for who in [Who::Trusted, Who::Other] {
        for inherit_only in [false, true] {
            let entry = [(ALLOW_ACE_TYPE, inherit_only, who)];
            assert!(private_folder_rule(&security(Who::User, true, &entry)).is_err());
        }
    }
    for other_allowing_type in [0x04, 0x05, 0x09, 0x0B] {
        let entry = [(other_allowing_type, false, Who::Other)];
        assert!(private_folder_rule(&security(Who::User, true, &entry)).is_err());
    }
}

#[test]
fn the_location_rule_on_windows_admits_this_user_system_and_administrators_only() {
    let standard = [
        (ALLOW_ACE_TYPE, false, Who::Trusted),
        (ALLOW_ACE_TYPE, false, Who::User),
    ];
    for owner in [Who::User, Who::Trusted] {
        assert_eq!(
            trusted_location_rule(&security(owner, false, &standard)),
            Ok(())
        );
    }
    assert!(trusted_location_rule(&security(Who::Other, false, &standard)).is_err());
    let for_children = [
        (ALLOW_ACE_TYPE, true, Who::Other),
        (ALLOW_ACE_TYPE, false, Who::User),
    ];
    assert_eq!(
        trusted_location_rule(&security(Who::User, false, &for_children)),
        Ok(())
    );
    let other = [(ALLOW_ACE_TYPE, false, Who::Other)];
    assert!(trusted_location_rule(&security(Who::User, false, &other)).is_err());
    let object = [(0x05, false, Who::Other)];
    assert!(trusted_location_rule(&security(Who::User, false, &object)).is_err());
    let mut missing = security(Who::User, false, &standard);
    missing.dacl_present = false;
    assert!(trusted_location_rule(&missing).is_err());
}
