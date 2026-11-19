// 보호된 디렉터리 복사의 공개 API와 등록된 리눅스 작동 범위를 검증한다.
use oah_core::fsx::FsxErrorKind;
use oah_core::fsx::links::{ProtectedLinkRef, create_protected_copy, remove_protected};
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
    use super::{FsxErrorKind, ProtectedLinkRef, create_protected_copy, fs, remove_protected};
    use oah_core::fsx::links::{CopiedFile, EntryIdentity, LinkKind, ManagedLink, Removal, remove};
    use oah_core::fsx::{Fingerprint, FsxError};
    use rustix::io::Errno;
    use std::fs::File;
    use std::os::unix::fs::MetadataExt;
    use std::path::{Path, PathBuf};

    fn descriptor_count() -> usize {
        fs::read_dir("/proc/self/fd").unwrap().count() - 1
    }

    fn with_free_descriptors<T>(free: usize, run: impl FnOnce() -> T) -> T {
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
        assert_eq!(descriptor_count(), occupied);
        drop(held);
        assert_eq!(descriptor_count(), baseline);
        println!(
            "FD_BUDGET free={free} limit={limit} occupied={occupied} baseline={baseline} residue=0"
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

    fn legacy_record(source: &Path, destination: &Path) -> ManagedLink {
        let metadata = fs::symlink_metadata(destination).unwrap();
        let mut record = ManagedLink {
            destination: destination.to_path_buf(),
            source: source.to_path_buf(),
            kind: LinkKind::Copy,
            identity: Some(EntryIdentity::Unix {
                device: metadata.dev(),
                inode: metadata.ino(),
            }),
            target: None,
            folders: Vec::new(),
            files: Vec::new(),
        };
        let mut pending = vec![PathBuf::new()];
        while let Some(directory) = pending.pop() {
            for item in fs::read_dir(destination.join(&directory)).unwrap() {
                let item = item.unwrap();
                let relative = directory.join(item.file_name());
                if item.file_type().unwrap().is_dir() {
                    pending.push(relative.clone());
                    record.folders.push(relative);
                } else {
                    assert!(item.file_type().unwrap().is_file());
                    record.files.push(CopiedFile {
                        relative,
                        fingerprint: Fingerprint::of(&fs::read(item.path()).unwrap()),
                        executable: item.metadata().unwrap().mode() & 0o111 != 0,
                    });
                }
            }
        }
        record
    }

    #[test]
    #[ignore = "requires a provisioned ext4 namespace and ordinary enrolled Linux writer"]
    fn enrolled_copy_operation() {
        let anchor =
            PathBuf::from(std::env::var_os("OAH_COPY_ANCHOR").expect("enrolled fixture path"));
        let case = std::env::var("OAH_COPY_CASE").expect("fixture copy name");
        let source_name = std::env::var("OAH_COPY_SOURCE").expect("fixture source name");
        for name in [&case, &source_name] {
            assert_eq!(Path::new(name).file_name().unwrap(), name.as_str());
        }
        let stores = anchor.join("store");
        let source = stores.join(source_name);
        let destination = anchor.join("links").join(&case);
        let saved = anchor.join("caller").join(&case);
        let create = || create_protected_copy(&anchor, &source, &destination);
        let restore = || ProtectedLinkRef::restore(&fs::read_to_string(&saved).unwrap()).unwrap();
        let delete =
            |reference: &ProtectedLinkRef| remove_protected(&anchor, reference, &[&stores]);
        let operation = std::env::var("OAH_COPY_OPERATION").expect("fixture operation");
        match operation.as_str() {
            "create" => {
                let reference = with_free_descriptors(2, create).unwrap();
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
                    with_free_descriptors(2, || delete(&reference)).unwrap(),
                    expected
                );
            }
            "create-one" => error(
                with_free_descriptors(1, create).unwrap_err(),
                FsxErrorKind::Io,
                Some(Errno::MFILE),
            ),
            "remove-one" => {
                let reference = restore();
                error(
                    with_free_descriptors(1, || delete(&reference)).unwrap_err(),
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
