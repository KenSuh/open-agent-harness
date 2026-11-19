// 보호된 디렉터리 복사의 공개 API와 등록된 리눅스 작동 범위를 검증한다.
use oah_core::fsx::FsxErrorKind;
use oah_core::fsx::links::{
    LinkRequest, ProtectedLinkRef, TargetStyle, create_protected, create_protected_copy,
    remove_protected,
};
use std::fs;

#[test]
fn an_unenrolled_copy_request_preserves_existing_files() {
    let temp = tempfile::tempdir().unwrap();
    let anchor = fs::canonicalize(temp.path()).unwrap();
    let source = anchor.join("source");
    let destination = anchor.join("links/copy");
    for root in [&source, &destination] {
        fs::create_dir_all(root).unwrap();
        fs::write(root.join("file"), root.as_os_str().as_encoded_bytes()).unwrap();
    }
    let reference =
        ProtectedLinkRef::restore(&format!("oah-copy-v1:{0}:{0}", "1".repeat(64))).unwrap();
    assert_eq!(
        create_protected_copy(&anchor, &source, &destination)
            .unwrap_err()
            .kind(),
        FsxErrorKind::NotOwned
    );
    for allow_copy in [false, true] {
        let request = LinkRequest {
            source: &source,
            destination: &destination,
            style: TargetStyle::Absolute,
            allow_copy,
        };
        assert_eq!(
            create_protected(&anchor, &request).unwrap_err().kind(),
            FsxErrorKind::NotOwned
        );
    }
    assert_eq!(
        remove_protected(&anchor, &reference, &[&source])
            .unwrap_err()
            .kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(fs::read_dir(&anchor).unwrap().count(), 2);
    for root in [source, destination] {
        assert_eq!(fs::read_dir(&root).unwrap().count(), 1);
        assert_eq!(
            fs::read(root.join("file")).unwrap(),
            root.as_os_str().as_encoded_bytes()
        );
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::{
        FsxErrorKind, LinkRequest, ProtectedLinkRef, TargetStyle, create_protected,
        create_protected_copy, fs, remove_protected,
    };
    use oah_core::fsx::links::{CopiedFile, EntryIdentity, LinkKind, ManagedLink, Removal, remove};
    use oah_core::fsx::{Fingerprint, FsxError};
    use rustix::io::Errno;
    use std::fs::File;
    use std::io::{ErrorKind, Write};
    use std::os::unix::{ffi::OsStrExt, fs::MetadataExt};
    use std::path::{Component, Path, PathBuf};

    fn descriptor_count() -> usize {
        fs::read_dir("/proc/self/fd").unwrap().count() - 1
    }

    fn with_free_descriptors<T>(operation: &str, free: usize, run: impl FnOnce() -> T) -> T {
        let baseline = descriptor_count();
        let mut held = Vec::new();
        loop {
            match File::open("/dev/null") {
                Ok(file) => held.push(file),
                Err(error) => {
                    assert_eq!(error.raw_os_error(), Some(Errno::MFILE.raw_os_error()));
                    break;
                }
            }
        }
        let limit = baseline + held.len();
        assert_eq!(limit, 32);
        held.truncate(held.len() - free);
        let occupied = descriptor_count();
        assert_eq!(occupied, limit - free);
        let result = run();
        let after = descriptor_count();
        assert_eq!(after, occupied);
        drop(held);
        let restored = descriptor_count();
        assert_eq!(restored, baseline);
        println!(
            "FD_BUDGET free={free} limit={limit} occupied={occupied} baseline={baseline} residue=0 \
             operation={operation} pid={} after={after} restored={restored}",
            std::process::id()
        );
        result
    }

    fn error(found: FsxError, kind: FsxErrorKind, errno: Option<Errno>) {
        assert_eq!(found.kind(), kind, "{found}");
        assert_eq!(
            found.io_error().and_then(std::io::Error::raw_os_error),
            errno.map(Errno::raw_os_error),
            "{found}"
        );
        println!("FSX_ERROR {found}");
    }

    fn tree_entries(root: &Path) -> (Vec<PathBuf>, Vec<CopiedFile>) {
        let mut folders = Vec::new();
        let mut files = Vec::new();
        let mut pending = vec![PathBuf::new()];
        while let Some(directory) = pending.pop() {
            for item in fs::read_dir(root.join(&directory)).unwrap() {
                let item = item.unwrap();
                let relative = directory.join(item.file_name());
                if item.file_type().unwrap().is_dir() {
                    pending.push(relative.clone());
                    folders.push(relative);
                } else {
                    assert!(item.file_type().unwrap().is_file());
                    files.push(CopiedFile {
                        relative,
                        fingerprint: Fingerprint::of(&fs::read(item.path()).unwrap()),
                        executable: item.metadata().unwrap().mode() & 0o111 != 0,
                    });
                }
            }
        }
        (folders, files)
    }

    fn legacy_record(source: &Path, destination: &Path) -> ManagedLink {
        let metadata = fs::symlink_metadata(destination).unwrap();
        let (folders, files) = tree_entries(destination);
        ManagedLink {
            destination: destination.to_path_buf(),
            source: source.to_path_buf(),
            kind: LinkKind::Copy,
            identity: Some(EntryIdentity::Unix {
                device: metadata.dev(),
                inode: metadata.ino(),
            }),
            target: None,
            folders,
            files,
        }
    }

    fn copy_snapshot(root: &Path) -> (Vec<PathBuf>, Vec<CopiedFile>) {
        assert!(fs::symlink_metadata(root).unwrap().is_dir());
        let (mut folders, mut files) = tree_entries(root);
        assert_eq!(files.len(), 600, "expected 600 regular files");
        folders.sort_unstable();
        files.sort_unstable_by(|left, right| left.relative.cmp(&right.relative));
        (folders, files)
    }

    fn assert_absent(path: &Path) {
        assert_eq!(
            fs::symlink_metadata(path).unwrap_err().kind(),
            ErrorKind::NotFound
        );
    }

    fn reference_kind(reference: &ProtectedLinkRef, kind: &str) {
        assert!(
            reference
                .encode()
                .split_once(':')
                .map(|(version, _)| version)
                == Some(kind),
            "expected a {kind} reference"
        );
    }

    fn redacted_error(error: FsxError) -> (FsxErrorKind, Option<i32>) {
        (
            error.kind(),
            error.io_error().and_then(std::io::Error::raw_os_error),
        )
    }

    #[test]
    #[ignore = "requires a provisioned ext4 namespace and ordinary enrolled Linux writer"]
    fn enrolled_copy_operation() {
        let anchor =
            PathBuf::from(std::env::var_os("OAH_COPY_ANCHOR").expect("enrolled fixture path"));
        let case = std::env::var("OAH_COPY_CASE").expect("fixture copy name");
        let source_name = std::env::var("OAH_COPY_SOURCE").expect("fixture source name");
        let operation = std::env::var("OAH_COPY_OPERATION").expect("fixture operation");
        let automatic = operation.starts_with("auto-");
        assert_eq!(Path::new(&case).file_name().unwrap(), case.as_str());
        if automatic {
            assert!(!source_name.is_empty());
            assert!(
                Path::new(&source_name)
                    .components()
                    .all(|component| matches!(component, Component::Normal(_)))
            );
        } else {
            assert_eq!(
                Path::new(&source_name).file_name().unwrap(),
                source_name.as_str()
            );
        }
        let stores = anchor.join("store");
        let source = stores.join(source_name);
        let destination = anchor.join("links").join(&case);
        let saved = anchor.join("caller").join(&case);
        if automatic {
            for path in [
                anchor.as_path(),
                source.as_path(),
                destination.parent().unwrap(),
                saved.parent().unwrap(),
            ] {
                assert_eq!(
                    fs::canonicalize(path).unwrap().as_os_str(),
                    path.as_os_str()
                );
            }
        }
        let create = || create_protected_copy(&anchor, &source, &destination);
        let restore = || ProtectedLinkRef::restore(&fs::read_to_string(&saved).unwrap()).unwrap();
        let delete =
            |reference: &ProtectedLinkRef| remove_protected(&anchor, reference, &[&stores]);
        match operation.as_str() {
            "auto-copy" | "auto-native" | "auto-no-consent" => {
                assert_absent(&destination);
                assert_absent(&saved);
                let stat = rustix::fs::statfs(&anchor).unwrap();
                assert_eq!(stat.f_type, 0xef53, "expected ext4");
                let block_size = usize::try_from(stat.f_bsize).unwrap();
                let target_bytes = source.as_os_str().as_bytes().len() + 1;
                if operation == "auto-native" {
                    assert!(target_bytes <= block_size);
                } else {
                    assert!(matches!(block_size, 1024 | 2048) && target_bytes > block_size);
                }
                let snapshot = (operation == "auto-copy").then(|| copy_snapshot(&source));
                let request = LinkRequest {
                    source: &source,
                    destination: &destination,
                    style: TargetStyle::Absolute,
                    allow_copy: operation != "auto-no-consent",
                };
                let result =
                    with_free_descriptors(&operation, 2, || create_protected(&anchor, &request))
                        .map_err(redacted_error);
                if request.allow_copy {
                    let reference = result.unwrap();
                    if let Some(before) = snapshot {
                        assert_eq!(copy_snapshot(&source), before);
                        assert_eq!(copy_snapshot(&destination), before);
                        reference_kind(&reference, "oah-copy-v1");
                        println!(
                            "COPY_CONTENT source_files=600 destination_files=600 matched=true"
                        );
                    } else {
                        assert!(fs::symlink_metadata(&destination).unwrap().is_symlink());
                        assert_eq!(fs::read_link(&destination).unwrap(), source);
                        reference_kind(&reference, "oah-link-v1");
                    }
                    let mut caller_record = File::create_new(&saved).unwrap();
                    caller_record
                        .write_all(reference.encode().as_bytes())
                        .unwrap();
                    caller_record.sync_all().unwrap();
                    File::open(saved.parent().unwrap())
                        .unwrap()
                        .sync_all()
                        .unwrap();
                } else {
                    assert_eq!(
                        result.unwrap_err(),
                        (FsxErrorKind::Io, Some(Errno::NAMETOOLONG.raw_os_error()))
                    );
                    assert_absent(&destination);
                    assert_absent(&saved);
                    println!(
                        "COPY_CAPACITY_REFUSAL errno=ENAMETOOLONG destination=absent reference=absent"
                    );
                }
                println!("COPY_TARGET block_size={block_size} bytes_with_nul={target_bytes}");
            }
            "auto-restore" | "auto-copy-remove" | "auto-native-remove" => {
                let reference = restore();
                let copy = fs::symlink_metadata(&destination).unwrap().is_dir();
                if copy {
                    reference_kind(&reference, "oah-copy-v1");
                } else {
                    assert!(fs::symlink_metadata(&destination).unwrap().is_symlink());
                    assert_eq!(fs::read_link(&destination).unwrap(), source);
                    reference_kind(&reference, "oah-link-v1");
                }
                if operation != "auto-restore" {
                    assert_eq!(copy, operation == "auto-copy-remove");
                    let snapshot = copy.then(|| copy_snapshot(&source));
                    if let Some(before) = &snapshot {
                        assert_eq!(&copy_snapshot(&destination), before);
                    }
                    assert_eq!(
                        with_free_descriptors(&operation, 2, || delete(&reference))
                            .map_err(redacted_error)
                            .unwrap(),
                        Removal::Removed
                    );
                    assert_absent(&destination);
                    if let Some(before) = snapshot {
                        assert_eq!(copy_snapshot(&source), before);
                        println!("COPY_SOURCE_PRESERVED files=600 matched=true");
                    } else {
                        assert!(fs::symlink_metadata(&source).unwrap().is_dir());
                    }
                }
            }
            "create" => {
                let reference = with_free_descriptors(&operation, 2, create).unwrap();
                fs::write(&saved, reference.encode()).unwrap();
            }
            "remove" | "repeat" => {
                let reference = restore();
                let expected = if operation == "remove" {
                    Removal::Removed
                } else {
                    Removal::AlreadyGone
                };
                assert_eq!(
                    with_free_descriptors(&operation, 2, || delete(&reference)).unwrap(),
                    expected
                );
            }
            "create-one" => error(
                with_free_descriptors(&operation, 1, create).unwrap_err(),
                FsxErrorKind::Io,
                Some(Errno::MFILE),
            ),
            "remove-one" => {
                let reference = restore();
                error(
                    with_free_descriptors(&operation, 1, || delete(&reference)).unwrap_err(),
                    FsxErrorKind::Io,
                    Some(Errno::MFILE),
                );
            }
            "legacy" => error(
                remove(&legacy_record(&source, &destination), &[&stores]).unwrap_err(),
                FsxErrorKind::NotOwned,
                None,
            ),
            "occupied" => error(create().unwrap_err(), FsxErrorKind::LinkExists, None),
            "refuse-create" => error(create().unwrap_err(), FsxErrorKind::NotOwned, None),
            "refuse-remove" => error(
                delete(&restore()).unwrap_err(),
                FsxErrorKind::NotOwned,
                None,
            ),
            "wrong-store" => error(
                remove_protected(&anchor, &restore(), &[Path::new("/outside-store")]).unwrap_err(),
                FsxErrorKind::NotOwned,
                None,
            ),
            "acquisition-io" => error(
                create().unwrap_err(),
                FsxErrorKind::Unverified,
                Some(Errno::FBIG),
            ),
            "deletion-barrier-io" => error(
                delete(&restore()).unwrap_err(),
                FsxErrorKind::Io,
                Some(Errno::FBIG),
            ),
            "retirement-io" => error(
                delete(&restore()).unwrap_err(),
                FsxErrorKind::Unverified,
                Some(Errno::FBIG),
            ),
            other => panic!("unknown protected copy operation {other}"),
        }
        println!(
            "COPY_OPERATION_OK operation={operation} case={case} pid={}",
            std::process::id()
        );
    }
}
