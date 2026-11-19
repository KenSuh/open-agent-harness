// 보호된 링크의 참조 형식과 플랫폼별 동작을 검증한다.
use oah_core::fsx::FsxErrorKind;
use oah_core::fsx::links::ProtectedLinkRef;

#[test]
fn protected_reference_has_a_strict_versioned_round_trip() {
    let text = format!("oah-link-v1:{}:{}", "a".repeat(64), "b".repeat(64));
    assert_eq!(ProtectedLinkRef::restore(&text).unwrap().encode(), text);
    for bad in [
        text.replace("link-v1", "link-v2"),
        text.to_uppercase(),
        format!("{text}:extra"),
        format!("{text}\n"),
        "legacy".to_owned(),
    ] {
        assert_eq!(
            ProtectedLinkRef::restore(&bad).unwrap_err().kind(),
            FsxErrorKind::NotOwned
        );
    }
}

#[cfg(not(target_os = "linux"))]
#[test]
fn protected_backend_refuses_on_unsupported_hosts() {
    use oah_core::fsx::links::{LinkRequest, TargetStyle, create_protected, remove_protected};
    use std::path::Path;
    let anchor = Path::new("unprovisioned");
    let source = anchor.join("source");
    let destination = anchor.join("links/example");
    let request = LinkRequest {
        source: &source,
        destination: &destination,
        style: TargetStyle::Absolute,
        allow_copy: false,
    };
    let reference =
        ProtectedLinkRef::restore(&format!("oah-link-v1:{0}:{0}", "1".repeat(64))).unwrap();
    assert_eq!(
        create_protected(anchor, &request).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(
        remove_protected(anchor, &reference, &[&source])
            .unwrap_err()
            .kind(),
        FsxErrorKind::NotOwned
    );
}

#[cfg(target_os = "linux")]
mod linux {
    use super::{FsxErrorKind, ProtectedLinkRef};
    use oah_core::fsx::FsxError;
    use oah_core::fsx::links::{
        EntryIdentity, LinkKind, LinkRequest, ManagedLink, Removal, TargetStyle, create_protected,
        remove, remove_protected,
    };
    use rustix::io::Errno;
    use std::fs::{self, File};
    use std::os::unix::fs::MetadataExt;
    use std::path::{Path, PathBuf};

    #[test]
    fn an_ordinary_temp_directory_cannot_supply_protected_authority() {
        let folder = tempfile::tempdir().unwrap();
        let anchor = fs::canonicalize(folder.path()).unwrap();
        let links = anchor.join("links");
        let source = anchor.join("source");
        fs::create_dir(&links).unwrap();
        fs::create_dir(&source).unwrap();
        let destination = links.join("entry");
        let result = create_protected(
            &anchor,
            &LinkRequest {
                source: &source,
                destination: &destination,
                style: TargetStyle::Absolute,
                allow_copy: false,
            },
        );
        assert_eq!(result.unwrap_err().kind(), FsxErrorKind::NotOwned);
        assert_eq!(fs::read_dir(&links).unwrap().count(), 0);
    }

    struct Fixture {
        anchor: PathBuf,
        case: String,
    }

    impl Fixture {
        fn store(&self) -> PathBuf {
            self.anchor.join("store")
        }

        fn destination(&self, leaf: &str) -> PathBuf {
            self.anchor.join("links").join(leaf)
        }

        fn create(&self, leaf: &str) -> Result<ProtectedLinkRef, FsxError> {
            create_protected(
                &self.anchor,
                &LinkRequest {
                    source: &self.store().join("source"),
                    destination: &self.destination(leaf),
                    style: if leaf.starts_with("many-") {
                        TargetStyle::Absolute
                    } else {
                        TargetStyle::Relative
                    },
                    allow_copy: false,
                },
            )
        }

        fn caller_record(&self) -> PathBuf {
            self.anchor.join("caller").join(&self.case)
        }

        fn read_reference(&self) -> ProtectedLinkRef {
            ProtectedLinkRef::restore(&fs::read_to_string(self.caller_record()).unwrap()).unwrap()
        }

        fn remove(&self, reference: &ProtectedLinkRef) -> Result<Removal, FsxError> {
            remove_protected(&self.anchor, reference, &[&self.store()])
        }
    }

    fn read_descriptor_count() -> usize {
        fs::read_dir("/proc/self/fd").unwrap().count() - 1
    }

    fn with_descriptor_budget<T>(free: usize, operation: impl FnOnce() -> T) -> T {
        let baseline = read_descriptor_count();
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
        let occupied = read_descriptor_count();
        assert_eq!(occupied, limit - free);
        let result = operation();
        assert_eq!(read_descriptor_count(), occupied);
        drop(held);
        assert_eq!(read_descriptor_count(), baseline);
        println!(
            "FD_BUDGET baseline={baseline} occupied={occupied} limit={limit} free={free} residue=0"
        );
        result
    }

    fn require_error(error: FsxError, kind: FsxErrorKind, errno: Option<Errno>) {
        assert_eq!(error.kind(), kind, "{error}");
        assert_eq!(
            error.io_error().and_then(std::io::Error::raw_os_error),
            errno.map(Errno::raw_os_error),
            "{error}"
        );
        println!("FSX_ERROR {error}");
    }

    fn require_legacy_refusal(fixture: &Fixture) {
        let destination = fixture.destination(&fixture.case);
        let stat = fs::symlink_metadata(&destination).unwrap();
        let key = ManagedLink {
            target: Some(fs::read_link(&destination).unwrap()),
            destination,
            source: fixture.store().join("source"),
            kind: LinkKind::Symlink,
            identity: Some(EntryIdentity::Unix {
                device: stat.dev(),
                inode: stat.ino(),
            }),
            folders: Vec::new(),
            files: Vec::new(),
        };
        require_error(
            remove(&key, &[&fixture.store()]).unwrap_err(),
            FsxErrorKind::NotOwned,
            None,
        );
    }

    const RECORD_COUNT: usize = 600;

    fn create_many(fixture: &Fixture) {
        let records = with_descriptor_budget(2, || {
            (0..RECORD_COUNT)
                .map(|index| fixture.create(&format!("many-{index:03}")).unwrap())
                .collect::<Vec<_>>()
        });
        let text: Vec<_> = records.iter().map(ProtectedLinkRef::encode).collect();
        fs::write(fixture.caller_record(), text.join("\n")).unwrap();
        println!("RECORDS_CREATED {}", text.len());
    }

    fn remove_many(fixture: &Fixture) {
        let text = fs::read_to_string(fixture.caller_record()).unwrap();
        let records: Vec<_> = text
            .lines()
            .map(|line| ProtectedLinkRef::restore(line).unwrap())
            .collect();
        assert_eq!(records.len(), RECORD_COUNT);
        with_descriptor_budget(2, || {
            for record in &records {
                assert_eq!(fixture.remove(record).unwrap(), Removal::Removed);
            }
        });
        println!("RECORDS_REMOVED {}", records.len());
    }

    #[test]
    #[ignore = "requires an externally enrolled ext4 fixture and separate provisioned Linux writer credentials"]
    fn enrolled_native_link_operation() {
        let fixture = Fixture {
            anchor: PathBuf::from(
                std::env::var_os("OAH_PROTECTED_FIXTURE").expect("enrolled fixture path"),
            ),
            case: std::env::var("OAH_PROTECTED_CASE").expect("synthetic fixture case"),
        };
        assert!(fixture.anchor.is_absolute());
        assert!(
            Path::new(&fixture.case)
                .file_name()
                .is_some_and(|name| name == fixture.case.as_str())
        );
        let operation = std::env::var("OAH_PROTECTED_OPERATION").expect("fixture operation");
        let create = || fixture.create(&fixture.case);
        let remove_saved = || fixture.remove(&fixture.read_reference());
        match operation.as_str() {
            "create" => {
                let reference = with_descriptor_budget(2, create).unwrap();
                fs::write(fixture.caller_record(), reference.encode()).unwrap();
            }
            "remove" | "repeat" => {
                let reference = fixture.read_reference();
                let expected = if operation == "remove" {
                    Removal::Removed
                } else {
                    Removal::AlreadyGone
                };
                assert_eq!(
                    with_descriptor_budget(2, || fixture.remove(&reference)).unwrap(),
                    expected
                );
            }
            "create-one" => require_error(
                with_descriptor_budget(1, create).unwrap_err(),
                FsxErrorKind::Io,
                Some(Errno::MFILE),
            ),
            "remove-one" => {
                let reference = fixture.read_reference();
                require_error(
                    with_descriptor_budget(1, || fixture.remove(&reference)).unwrap_err(),
                    FsxErrorKind::Io,
                    Some(Errno::MFILE),
                );
            }
            "legacy" => require_legacy_refusal(&fixture),
            "refuse-create" => require_error(create().unwrap_err(), FsxErrorKind::NotOwned, None),
            "occupied" => require_error(create().unwrap_err(), FsxErrorKind::LinkExists, None),
            "refuse-remove" => {
                require_error(remove_saved().unwrap_err(), FsxErrorKind::NotOwned, None)
            }
            "wrong-store" => require_error(
                remove_protected(
                    &fixture.anchor,
                    &fixture.read_reference(),
                    &[Path::new("/different-store")],
                )
                .unwrap_err(),
                FsxErrorKind::NotOwned,
                None,
            ),
            "busy" => require_error(
                create().unwrap_err(),
                FsxErrorKind::Busy,
                Some(Errno::WOULDBLOCK),
            ),
            "publication-failure" => require_error(
                create().unwrap_err(),
                FsxErrorKind::Unverified,
                Some(Errno::FBIG),
            ),
            "deletion-barrier-failure" => require_error(
                remove_saved().unwrap_err(),
                FsxErrorKind::Io,
                Some(Errno::FBIG),
            ),
            "retirement-failure" => require_error(
                remove_saved().unwrap_err(),
                FsxErrorKind::Unverified,
                Some(Errno::FBIG),
            ),
            "create-many" => create_many(&fixture),
            "remove-many" => remove_many(&fixture),
            other => panic!("unknown fixture operation {other}"),
        }
        println!(
            "PROTECTED_OPERATION_OK operation={operation} case={} pid={}",
            fixture.case,
            std::process::id()
        );
    }
}
