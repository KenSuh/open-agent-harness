// 관리 링크 시험: 기본 링크(macOS 심볼릭 링크·Windows 정션)와 보고되는 복사 대체, 남의 항목 보존, 링크 삭제가 대상을 지우지 않는지 확인한다.
use oah_core::fsx::links::{
    CopiedFile, Entry, EntryIdentity, LinkKind, LinkRequest, ManagedLink, NativeLinker, Removal,
    SystemLinker, TargetStyle, create, create_with, inspect, remove,
};
use oah_core::fsx::{Fingerprint, FsxErrorKind};
use std::cell::Cell;
use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

#[cfg(windows)]
#[path = "support/windows_context.rs"]
mod windows_context;

struct Stores {
    _dir: tempfile::TempDir,
    store: PathBuf,
    agents: PathBuf,
}

impl Stores {
    fn new() -> Stores {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let store = root.join("store");
        let agents = root.join("agents");
        fs::create_dir_all(store.join("skill-a").join("scripts")).unwrap();
        fs::write(store.join("skill-a").join("SKILL.md"), "# Skill A\n").unwrap();
        fs::write(
            store.join("skill-a").join("scripts").join("run.sh"),
            "echo run\n",
        )
        .unwrap();
        fs::create_dir_all(&agents).unwrap();
        Stores {
            _dir: dir,
            store,
            agents,
        }
    }

    fn source(&self) -> PathBuf {
        self.store.join("skill-a")
    }

    fn request<'a>(
        &self,
        destination: &'a Path,
        style: TargetStyle,
        allow_copy: bool,
        source: &'a Path,
    ) -> LinkRequest<'a> {
        LinkRequest {
            source,
            destination,
            style,
            allow_copy,
        }
    }
}

struct StagingProbe {
    kind: LinkKind,
    classified: Cell<bool>,
}

impl StagingProbe {
    fn new(kind: LinkKind) -> Self {
        Self {
            kind,
            classified: Cell::new(false),
        }
    }
}

impl NativeLinker for StagingProbe {
    fn kind(&self) -> LinkKind {
        self.classified.set(true);
        self.kind
    }

    fn link(&self, _source: &Path, _destination: &Path, _target: &Path) -> io::Result<()> {
        panic!("a path-only request reached the custom linker");
    }
}

#[cfg(unix)]
fn legacy_identity(path: &Path) -> EntryIdentity {
    use std::os::unix::fs::MetadataExt;
    let metadata = fs::symlink_metadata(path).unwrap();
    EntryIdentity::Unix {
        device: metadata.dev(),
        inode: metadata.ino(),
    }
}

#[cfg(windows)]
fn legacy_identity(path: &Path) -> EntryIdentity {
    use std::os::windows::fs::OpenOptionsExt;
    let file = fs::OpenOptions::new()
        .access_mode(0)
        .custom_flags(0x0200_0000 | 0x0020_0000)
        .open(path)
        .unwrap();
    let info = winapi_util::file::information(file).unwrap();
    EntryIdentity::Windows {
        volume_serial: info.volume_serial_number(),
        file_index: info.file_index(),
    }
}

fn legacy_native(source: &Path, destination: &Path, target: &Path) -> ManagedLink {
    SystemLinker.link(source, destination, target).unwrap();
    ManagedLink {
        destination: destination.to_path_buf(),
        source: source.to_path_buf(),
        kind: native_kind(),
        identity: Some(legacy_identity(destination)),
        target: Some(target.to_path_buf()),
        folders: Vec::new(),
        files: Vec::new(),
    }
}

fn legacy_copy(source: &Path, destination: &Path) -> ManagedLink {
    let scripts = PathBuf::from("scripts");
    fs::create_dir_all(destination.join(&scripts)).unwrap();
    let files = [PathBuf::from("SKILL.md"), scripts.join("run.sh")]
        .into_iter()
        .map(|relative| {
            let bytes = fs::read(source.join(&relative)).unwrap();
            fs::write(destination.join(&relative), &bytes).unwrap();
            CopiedFile {
                relative,
                fingerprint: Fingerprint::of(&bytes),
                executable: false,
            }
        })
        .collect();
    ManagedLink {
        destination: destination.to_path_buf(),
        source: source.to_path_buf(),
        kind: LinkKind::Copy,
        identity: Some(legacy_identity(destination)),
        target: None,
        folders: vec![scripts],
        files,
    }
}

fn native_kind() -> LinkKind {
    if cfg!(windows) {
        LinkKind::Junction
    } else {
        LinkKind::Symlink
    }
}

/// Every name in `folder`, sorted; private attempt or copy folders would show up here.
fn names(folder: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(folder)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

/// Removes a link the test made itself (a junction is removed as a folder).
fn unlink(path: &Path) {
    if cfg!(windows) {
        fs::remove_dir(path).unwrap();
    } else {
        fs::remove_file(path).unwrap();
    }
}

#[test]
fn general_staging_refuses_before_any_change() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("도구");
    let sibling = stores.agents.join(".existing-stage");
    fs::create_dir(&sibling).unwrap();
    fs::write(sibling.join("keep"), b"pre-existing stage bytes").unwrap();

    #[cfg(unix)]
    let stamps = [&stores.agents, &sibling, &source].map(|folder| {
        let past = std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_234_567_890);
        fs::File::open(folder).unwrap().set_modified(past).unwrap();
        (folder, fs::metadata(folder).unwrap().modified().unwrap())
    });

    for style in [TargetStyle::Absolute, TargetStyle::Relative] {
        for allow_copy in [false, true] {
            let request = stores.request(&destination, style, allow_copy, &source);
            let assert_refused = |error: oah_core::fsx::FsxError| {
                assert_eq!(error.code(), "OAH_LINK_UNSUPPORTED", "{error}");
                assert_eq!(error.path(), destination);
                assert!(error.io_error().is_none(), "{error}");
                assert!(error.cleanup_error().is_none(), "{error}");
            };
            assert_refused(create(&request).unwrap_err());
            for kind in [LinkKind::Symlink, LinkKind::Junction, LinkKind::Copy] {
                let linker = StagingProbe::new(kind);
                assert_refused(create_with(&request, &linker).unwrap_err());
                assert!(!linker.classified.get(), "{kind:?}");
            }

            assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
            assert_eq!(names(&stores.agents), [".existing-stage"]);
            assert_eq!(names(&sibling), ["keep"]);
            assert_eq!(
                fs::read(sibling.join("keep")).unwrap(),
                b"pre-existing stage bytes"
            );
            assert_eq!(fs::read(source.join("SKILL.md")).unwrap(), b"# Skill A\n");
            #[cfg(unix)]
            for (folder, modified) in stamps {
                assert_eq!(fs::metadata(folder).unwrap().modified().unwrap(), modified);
            }
        }
    }
}

#[test]
fn invalid_requests_are_rejected_before_the_staging_decision() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let relative = Path::new("relative");
    let source_file = source.join("SKILL.md");
    let missing_source = source.join("missing");
    let missing_parent = destination.join("missing-parent");

    let cases: [(&Path, &Path, &Path); 5] = [
        (relative, &destination, relative),
        (&source, relative, relative),
        (&source_file, &destination, &source_file),
        (&missing_source, &destination, &missing_source),
        (&source, &missing_parent, &missing_parent),
    ];
    for (source, destination, error_path) in cases {
        let request = stores.request(destination, TargetStyle::Absolute, true, source);
        let error = create(&request).unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::Io, "{error}");
        assert_eq!(error.path(), error_path);
    }
    assert!(names(&stores.agents).is_empty());
}

#[test]
fn an_occupied_destination_is_left_alone() {
    let stores = Stores::new();
    let source = stores.source();
    let folder = stores.agents.join("mine");
    fs::create_dir(&folder).unwrap();
    fs::write(folder.join("notes.md"), "user text").unwrap();
    let request = stores.request(&folder, TargetStyle::Absolute, true, &source);
    for error in [
        create(&request).unwrap_err(),
        create_with(&request, &StagingProbe::new(native_kind())).unwrap_err(),
        create_with(&request, &StagingProbe::new(LinkKind::Copy)).unwrap_err(),
    ] {
        assert_eq!(error.kind(), FsxErrorKind::LinkExists);
        assert_eq!(error.path(), folder);
        assert!(error.cleanup_error().is_none());
    }
    assert_eq!(
        fs::read_to_string(folder.join("notes.md")).unwrap(),
        "user text"
    );
    let file = stores.agents.join("file");
    fs::write(&file, "kept").unwrap();
    let error = create(&stores.request(&file, TargetStyle::Absolute, true, &source)).unwrap_err();
    assert_eq!(error.code(), "OAH_LINK_EXISTS");
    assert_eq!(fs::read_to_string(&file).unwrap(), "kept");
}

#[test]
fn legacy_native_record_preserves_the_link_and_reports_ordinary_absence() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let record = legacy_native(&source, &destination, &source);
    assert_eq!(
        remove(&record, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(fs::canonicalize(&destination).unwrap(), source);
    assert_eq!(
        fs::read_to_string(source.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert!(source.join("scripts").join("run.sh").is_file());
    unlink(&destination);
    assert_eq!(
        remove(&record, &[&stores.store]).unwrap(),
        Removal::AlreadyGone
    );
}

#[cfg(any(unix, windows))]
#[test]
fn legacy_absence_requires_ordinary_destination_ancestors() {
    let stores = Stores::new();
    let source = stores.source();
    let alias = stores.agents.join("alias");
    #[cfg(unix)]
    std::os::unix::fs::symlink(&source, &alias).unwrap();
    #[cfg(windows)]
    junction::create(&source, &alias).unwrap();

    let remove_at = |parent: &Path| {
        remove(
            &ManagedLink {
                destination: parent.join("absent"),
                source: source.clone(),
                kind: native_kind(),
                identity: None,
                target: None,
                files: Vec::new(),
                folders: Vec::new(),
            },
            &[&stores.store],
        )
    };
    for parent in [source.join("scripts"), source.join("missing")] {
        assert_eq!(remove_at(&parent).unwrap(), Removal::AlreadyGone);
    }
    for parent in [
        alias.join("scripts"),
        alias.join("missing"),
        alias,
        source.join("SKILL.md"),
    ] {
        let error = remove_at(&parent).expect_err("unverified ancestor reported ordinary absence");
        assert_eq!(error.kind(), FsxErrorKind::NotOwned, "{parent:?}: {error}");
    }
}

#[test]
fn a_link_that_is_no_longer_ours_is_not_removed() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let record = legacy_native(&source, &destination, &source);

    let elsewhere = stores.agents.join("unrelated-store");
    let error = remove(&record, &[&elsewhere]).unwrap_err();
    assert_eq!(
        error.kind(),
        FsxErrorKind::NotOwned,
        "the source is outside every store"
    );
    assert!(matches!(
        inspect(&destination).unwrap(),
        Entry::Symlink { .. } | Entry::Junction { .. }
    ));

    let other = stores.store.join("skill-b");
    fs::create_dir(&other).unwrap();
    unlink(&destination);
    let replaced = legacy_native(&other, &destination, &other);
    let error = remove(&record, &[&stores.store]).unwrap_err();
    assert_eq!(
        error.code(),
        "OAH_LINK_NOT_OWNED",
        "the link now points somewhere else"
    );
    assert_eq!(
        remove(&replaced, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(fs::canonicalize(&destination).unwrap(), other);
}

#[test]
fn unowned_matching_link_cannot_be_adopted_and_removed() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("user-link");
    SystemLinker.link(&source, &destination, &source).unwrap();
    let error =
        create(&stores.request(&destination, TargetStyle::Absolute, true, &source)).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkExists);
    assert_eq!(fs::canonicalize(&destination).unwrap(), source);
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
}

#[test]
fn cleanup_ownership_record_refuses_mismatching_replacements() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let owned = legacy_native(&source, &destination, &source);
    let saved = stores.agents.join("original-link");
    fs::rename(&destination, &saved).unwrap();

    let other = stores.store.join("skill-b");
    fs::create_dir(&other).unwrap();
    fs::write(other.join("SKILL.md"), "# Skill B\n").unwrap();
    SystemLinker.link(&other, &destination, &other).unwrap();
    let error = remove(&owned, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(fs::canonicalize(&destination).unwrap(), other);
    unlink(&destination);

    fs::create_dir(&destination).unwrap();
    fs::write(destination.join("notes.md"), "user text").unwrap();
    let error = remove(&owned, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(
        fs::read_to_string(destination.join("notes.md")).unwrap(),
        "user text"
    );
    fs::remove_dir_all(&destination).unwrap();

    fs::write(&destination, "a user file").unwrap();
    let error = remove(&owned, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(fs::read_to_string(&destination).unwrap(), "a user file");
    fs::remove_file(&destination).unwrap();

    SystemLinker.link(&source, &destination, &source).unwrap();
    let error = remove(&owned, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(fs::canonicalize(&destination).unwrap(), source);
    assert_eq!(
        fs::read_to_string(source.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert_eq!(fs::canonicalize(&saved).unwrap(), source);
}

#[test]
fn legacy_copy_records_preserve_matching_copies_and_ordinary_children() {
    let stores = Stores::new();
    let source = stores.source();
    let name = if cfg!(unix) { r"skill-a\." } else { "skill-a" };
    let destination = stores.agents.join(name);
    let record = legacy_copy(&source, &destination);
    fs::write(destination.join("SKILL.md"), "# Edited by the user\n").unwrap();
    let error = remove(&record, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Edited by the user\n"
    );

    fs::write(destination.join("SKILL.md"), "# Skill A\n").unwrap();
    fs::write(destination.join("added.txt"), "new").unwrap();
    assert_eq!(
        remove(&record, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );

    fs::remove_file(destination.join("added.txt")).unwrap();
    assert_eq!(
        remove(&record, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(
        fs::read(destination.join("SKILL.md")).unwrap(),
        b"# Skill A\n"
    );
    assert_eq!(fs::read(source.join("SKILL.md")).unwrap(), b"# Skill A\n");
}

#[test]
fn ambiguous_copy_destinations_are_refused_even_when_absent() {
    let separator = std::path::MAIN_SEPARATOR_STR;
    let terminal_dot = format!("{separator}.");
    for (suffix, reason) in [
        (separator, "trailing separator"),
        (terminal_dot.as_str(), "dot component"),
    ] {
        let stores = Stores::new();
        let source = stores.source();
        let destination = stores.agents.join("skill.v1");
        let canonical = legacy_copy(&source, &destination);
        assert_eq!(canonical.kind, LinkKind::Copy);

        let mut legacy = canonical.clone();
        legacy.destination.as_mut_os_string().push(suffix);
        let record_path = stores._dir.path().join("managed-record");
        let saved_record = record_text(&legacy);
        fs::write(&record_path, &saved_record).unwrap();
        let restored = parse_record(&fs::read_to_string(&record_path).unwrap());
        let assert_refused = || {
            let error = remove(&restored, &[&stores.store]).unwrap_err();
            assert_eq!(error.code(), "OAH_LINK_NOT_OWNED");
            assert_eq!(error.path().as_os_str(), legacy.destination.as_os_str());
            assert!(error.to_string().contains(reason), "{error}");
            assert_eq!(fs::read(&record_path).unwrap(), saved_record.as_bytes());
        };

        assert_refused();
        assert_eq!(names(&stores.agents), ["skill.v1"]);
        assert_eq!(inspect(&destination).unwrap(), Entry::Directory);
        assert_eq!(names(&destination), ["SKILL.md", "scripts"]);
        assert_eq!(names(&destination.join("scripts")), ["run.sh"]);
        assert_eq!(
            fs::read(destination.join("SKILL.md")).unwrap(),
            b"# Skill A\n"
        );
        assert_eq!(
            fs::read(destination.join("scripts").join("run.sh")).unwrap(),
            b"echo run\n"
        );

        assert_eq!(
            remove(&canonical, &[&stores.store]).unwrap_err().kind(),
            FsxErrorKind::NotOwned
        );
        fs::remove_dir_all(&destination).unwrap();
        assert!(names(&stores.agents).is_empty());
        assert_refused();
        assert_eq!(
            remove(&canonical, &[&stores.store]).unwrap(),
            Removal::AlreadyGone
        );
        assert!(names(&stores.agents).is_empty());
        assert_eq!(fs::read(source.join("SKILL.md")).unwrap(), b"# Skill A\n");
        assert_eq!(
            fs::read(source.join("scripts").join("run.sh")).unwrap(),
            b"echo run\n"
        );
    }
}

#[cfg(windows)]
#[test]
fn an_existing_junction_keeps_its_absolute_target_and_is_preserved() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let record = legacy_native(&source, &destination, &source);
    assert_eq!(record.kind, LinkKind::Junction);
    assert!(junction::exists(&destination).unwrap());
    let target = junction::get_target(&destination).unwrap();
    assert!(target.is_absolute());
    assert_eq!(fs::canonicalize(target).unwrap(), source);
    assert_eq!(
        remove(&record, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(
        fs::read(destination.join("SKILL.md")).unwrap(),
        b"# Skill A\n"
    );
}

#[cfg(windows)]
#[test]
fn a_long_junction_target_is_refused_before_starting_the_attempt() {
    let stores = Stores::new();
    let mut source = stores.store.clone();
    while source.as_os_str().len() <= 4200 {
        source.push("d".repeat(120));
    }
    fs::create_dir_all(&source).unwrap();
    fs::write(source.join("SKILL.md"), "# Long\n").unwrap();
    let destination = stores.agents.join("skill-long");
    for allow_copy in [false, true] {
        let error =
            create(&stores.request(&destination, TargetStyle::Absolute, allow_copy, &source))
                .unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported);
        assert_eq!(error.path(), destination);
        assert!(error.io_error().is_none(), "{error}");
        assert!(error.cleanup_error().is_none(), "{error}");
        assert!(names(&stores.agents).is_empty());
        assert_eq!(fs::read(source.join("SKILL.md")).unwrap(), b"# Long\n");
    }
}

#[cfg(windows)]
fn require_windows_context(guard: u32) {
    let context =
        windows_context::ProcessContext::read().expect("read the current Windows process context");
    context
        .require_medium(guard)
        .expect("required Windows process context");
    println!(
        "WINDOWS_CONTEXT pid={} integrity={} elevated={} redirection_guard={}",
        context.pid, context.integrity, context.elevated, context.redirection_guard
    );
}

#[cfg(windows)]
#[test]
#[ignore = "requires ordinary medium integrity with Redirection Guard disabled; run by the native evidence gate"]
fn ordinary_medium_integrity_native_junction_contract() {
    require_windows_context(0);
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome =
        create(&stores.request(&destination, TargetStyle::Relative, false, &source)).unwrap();
    assert_eq!(outcome.link.kind, LinkKind::Junction);
    assert_eq!(outcome.link.target.as_deref(), Some(source.as_path()));
    assert!(junction::exists(&destination).unwrap());
    assert_eq!(names(&stores.agents), ["skill-a"]);
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert!(source.join("SKILL.md").is_file());
    assert!(junction::exists(&destination).unwrap());
}

#[cfg(windows)]
#[test]
#[ignore = "requires medium integrity and actual Redirection Guard traversal refusal; run by the native evidence gate"]
fn system_linker_copy_under_actual_traversal_refusal() {
    require_windows_context(1);
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let error =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &source)).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported);
    assert_eq!(
        error.io_error().and_then(io::Error::raw_os_error),
        Some(448)
    );
    assert!(error.cleanup_error().is_none(), "{error}");
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
    assert_eq!(names(&stores.agents), Vec::<String>::new());
    let outcome =
        create(&stores.request(&destination, TargetStyle::Absolute, true, &source)).unwrap();
    assert_eq!(outcome.link.kind, LinkKind::Copy);
    assert_eq!(names(&stores.agents), ["skill-a"]);
    assert!(
        outcome
            .fallback
            .as_deref()
            .unwrap()
            .contains("os error 448")
    );
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(
        fs::read_to_string(source.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert_eq!(inspect(&destination).unwrap(), Entry::Directory);
    assert_eq!(names(&stores.agents), ["skill-a"]);
}

/// The role a separate process of this test binary plays, and the folder it works in.
fn child_role() -> Option<(String, PathBuf)> {
    let role = std::env::var("OAH_LINK_TEST_ROLE").ok()?;
    let root = std::env::var_os("OAH_LINK_TEST_ROOT")?;
    Some((role, PathBuf::from(root)))
}

/// Runs `test` again in a separate process of this test binary as `role`, working in `root`, and returns its
/// output after checking that it ran just that test and finished. On Unix, `limit` caps the open descriptors of
/// that process with `ulimit -n`.
fn rerun(test: &str, role: &str, root: &Path, limit: Option<u32>) -> String {
    let exe = std::env::current_exe().unwrap();
    let args = ["--exact", test, "--nocapture", "--test-threads=1"];
    let mut command = match limit {
        Some(limit) if cfg!(unix) => {
            let mut command = Command::new("/bin/sh");
            command
                .arg("-c")
                .arg(format!("ulimit -n {limit} && exec \"$0\" \"$@\""))
                .arg(&exe)
                .args(args);
            command
        }
        _ => {
            let mut command = Command::new(&exe);
            command.args(args);
            command
        }
    };
    let output = command
        .env("OAH_LINK_TEST_ROLE", role)
        .env("OAH_LINK_TEST_ROOT", root)
        .output()
        .unwrap();
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        output.status.success(),
        "the {role} process failed:\n{text}"
    );
    assert!(text.contains(&format!("ROLE_OK {role}")), "{text}");
    assert!(text.contains("test result: ok. 1 passed"), "{text}");
    // libtest prints the test's own output after the test's name, on the same line.
    for line in text
        .lines()
        .filter_map(|line| line.find("ROLE_OK").map(|at| &line[at..]))
    {
        println!("{line} limit={limit:?}");
    }
    text
}

/// The process id a role reported.
fn reported_process(output: &str) -> &str {
    output
        .split("process=")
        .nth(1)
        .and_then(|rest| rest.split_whitespace().next())
        .unwrap()
}

/// Opens files until this process has no descriptor left (failing if 300 open without a limit), then frees
/// `free` of them. The others stay open until the returned files are dropped.
#[cfg(unix)]
fn hold_all_but(free: usize) -> Vec<fs::File> {
    let mut held = Vec::new();
    let error = loop {
        match fs::File::open("/dev/null") {
            Ok(_) if held.len() == 300 => panic!("opened 300 files; no descriptor limit"),
            Ok(file) => held.push(file),
            Err(error) => break error,
        }
    };
    assert_eq!(error.raw_os_error(), Some(24), "{error}");
    held.truncate(held.len() - free);
    held
}

/// Writes a record as plain text, the way a caller keeps it in its own state between processes.
fn record_text(link: &ManagedLink) -> String {
    let kind = match link.kind {
        LinkKind::Symlink => "symlink",
        LinkKind::Junction => "junction",
        LinkKind::Copy => "copy",
    };
    let text = |path: &Path| path.to_str().unwrap().to_string();
    let mut lines = vec![
        format!("kind={kind}"),
        format!("destination={}", text(&link.destination)),
        format!("source={}", text(&link.source)),
    ];
    if let Some(identity) = link.identity {
        let value = match identity {
            EntryIdentity::Unix { device, inode } => format!("unix {device} {inode}"),
            EntryIdentity::Windows {
                volume_serial,
                file_index,
            } => format!("windows {volume_serial} {file_index}"),
            EntryIdentity::WindowsFatDirectory {
                volume_serial,
                file_index,
                first_cluster,
            } => format!("fat-directory {volume_serial} {file_index} {first_cluster}"),
        };
        lines.push(format!("identity={value}"));
    }
    if let Some(target) = &link.target {
        lines.push(format!("target={}", text(target)));
    }
    for folder in &link.folders {
        lines.push(format!("folder={}", text(folder)));
    }
    for file in &link.files {
        lines.push(format!(
            "file={} {} {}",
            file.fingerprint.to_hex(),
            u8::from(file.executable),
            text(&file.relative)
        ));
    }
    lines.join("\n")
}

/// Rebuilds a record from the text [`record_text`] wrote, as a later process does.
fn parse_record(text: &str) -> ManagedLink {
    let mut fields = BTreeMap::new();
    let mut folders = Vec::new();
    let mut files = Vec::new();
    for line in text.lines() {
        let (key, value) = line.split_once('=').unwrap();
        match key {
            "folder" => folders.push(PathBuf::from(value)),
            "file" => {
                let mut parts = value.splitn(3, ' ');
                let fingerprint = Fingerprint::from_hex(parts.next().unwrap()).unwrap();
                let executable = parts.next().unwrap() == "1";
                let relative = PathBuf::from(parts.next().unwrap());
                files.push(CopiedFile {
                    relative,
                    fingerprint,
                    executable,
                });
            }
            _ => {
                fields.insert(key, value);
            }
        }
    }
    let kind = match fields["kind"] {
        "symlink" => LinkKind::Symlink,
        "junction" => LinkKind::Junction,
        "copy" => LinkKind::Copy,
        other => panic!("unknown kind {other}"),
    };
    let identity = fields.get("identity").map(|text| {
        let fields: Vec<_> = text.split(' ').collect();
        assert_eq!(
            fields.len(),
            if fields[0] == "fat-directory" { 4 } else { 3 }
        );
        let first = fields[1].parse().unwrap();
        let second = fields[2].parse().unwrap();
        match fields[0] {
            "unix" => EntryIdentity::Unix {
                device: first,
                inode: second,
            },
            "windows" => EntryIdentity::Windows {
                volume_serial: first,
                file_index: second,
            },
            "fat-directory" => EntryIdentity::WindowsFatDirectory {
                volume_serial: first,
                file_index: second,
                first_cluster: fields[3].parse().unwrap(),
            },
            other => panic!("unsupported identity tag {other}"),
        }
    });
    ManagedLink {
        destination: PathBuf::from(fields["destination"]),
        source: PathBuf::from(fields["source"]),
        kind,
        identity,
        target: fields.get("target").map(|target| PathBuf::from(*target)),
        folders,
        files,
    }
}

#[test]
fn durable_record_legacy_records_round_trip_across_processes_without_removal_authority() {
    const TEST: &str =
        "durable_record_legacy_records_round_trip_across_processes_without_removal_authority";
    if let Some((role, root)) = child_role() {
        let store = root.join("store");
        let agents = root.join("agents");
        let records = root.join("records");
        match role.as_str() {
            "create" => {
                fs::create_dir_all(&records).unwrap();
                let source = store.join("skill-a");
                for (name, copy) in [("linked", false), ("copied", true)] {
                    let destination = agents.join(name);
                    let record = if copy {
                        legacy_copy(&source, &destination)
                    } else {
                        legacy_native(&source, &destination, &source)
                    };
                    let text = record_text(&record);
                    assert_eq!(parse_record(&text), record, "the text is the whole record");
                    fs::write(records.join(name), text).unwrap();
                }
            }
            "remove" => {
                let stores = [store.as_path()];
                for name in ["linked", "copied"] {
                    let path = records.join(name);
                    let saved = fs::read_to_string(&path).unwrap();
                    let record = parse_record(&saved);
                    assert!(record.identity.is_some());
                    let error = remove(&record, &stores).unwrap_err();
                    assert_eq!(error.kind(), FsxErrorKind::NotOwned, "{name}: {error}");
                    assert_eq!(fs::read(&path).unwrap(), saved.as_bytes());
                }
            }
            other => panic!("unknown role {other}"),
        }
        println!("ROLE_OK {role} process={}", std::process::id());
        return;
    }
    let stores = Stores::new();
    let root = stores.store.parent().unwrap().to_path_buf();
    let created = rerun(TEST, "create", &root, None);
    let refused = rerun(TEST, "remove", &root, None);
    assert_ne!(reported_process(&created), reported_process(&refused));
    assert_eq!(names(&stores.agents), ["copied", "linked"]);
    assert_eq!(
        fs::canonicalize(stores.agents.join("linked")).unwrap(),
        stores.source()
    );
    assert_eq!(
        fs::read(stores.agents.join("copied/SKILL.md")).unwrap(),
        b"# Skill A\n"
    );
    assert_eq!(
        fs::read_to_string(stores.source().join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
}

#[cfg(windows)]
#[test]
fn operation_identity_real_fat32_fallback_record() {
    const TEST: &str = "operation_identity_real_fat32_fallback_record";
    if let Some((role, root)) = child_role() {
        let store = root.join("store");
        let source = store.join("skill");
        let agents = root.join("agents");
        let records = root.join("records");
        let read = |name: &str| parse_record(&fs::read_to_string(records.join(name)).unwrap());
        match role.as_str() {
            "create" => {
                let destination = agents.join("copy");
                let mut request = LinkRequest {
                    source: &source,
                    destination: &destination,
                    style: TargetStyle::Absolute,
                    allow_copy: false,
                };
                let error = create(&request).unwrap_err();
                assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported);
                assert_eq!(error.io_error().and_then(io::Error::raw_os_error), Some(1));
                assert!(error.cleanup_error().is_none());
                assert_eq!(names(&agents), ["unowned.txt"]);
                request.allow_copy = true;
                for name in ["copy", "replaced", "malformed"] {
                    let destination = agents.join(name);
                    let outcome = create(&LinkRequest {
                        destination: &destination,
                        ..request
                    })
                    .unwrap();
                    assert_eq!(outcome.link.kind, LinkKind::Copy);
                    assert!(outcome.created);
                    assert!(outcome.fallback.as_deref().unwrap().contains("os error 1"));
                    assert_eq!(
                        fs::read(destination.join("스킬.txt")).unwrap(),
                        b"synthetic bytes\r\n"
                    );
                    let text = record_text(&outcome.link);
                    assert_eq!(parse_record(&text), outcome.link);
                    fs::write(records.join(name), text).unwrap();
                    println!("RETURNED_PROOF {name} {:?}", outcome.link.identity);
                }
            }
            "replace" => {
                let destination = agents.join("replaced");
                fs::rename(
                    &destination,
                    root.join("retained-original-with-a-longer-name"),
                )
                .unwrap();
                let original = read("replaced");
                let index = |link: &ManagedLink| match link.identity.unwrap() {
                    EntryIdentity::WindowsFatDirectory { file_index, .. } => file_index,
                    other => {
                        panic!("the actual returned record lacks FAT directory proof: {other:?}")
                    }
                };
                let mut retained = vec![original];
                let mut reused = false;
                for number in 0..128 {
                    let replacement = create(&LinkRequest {
                        source: &source,
                        destination: &destination,
                        style: TargetStyle::Absolute,
                        allow_copy: true,
                    })
                    .unwrap();
                    println!(
                        "REPLACEMENT_PROOF attempt={number} {:?}",
                        replacement.link.identity
                    );
                    if let Some(original) = retained
                        .iter()
                        .find(|prior| index(prior) == index(&replacement.link))
                    {
                        assert_ne!(original.identity, replacement.link.identity);
                        fs::write(records.join("reused-original"), record_text(original)).unwrap();
                        fs::write(records.join("replacement"), record_text(&replacement.link))
                            .unwrap();
                        println!(
                            "ACTUAL_FAT32_SLOT_REUSED_WITH_ORIGINAL_ALLOCATED original={:?} replacement={:?}",
                            original.identity, replacement.link.identity
                        );
                        reused = true;
                        break;
                    }
                    fs::rename(
                        &destination,
                        root.join(format!("retained-replacement-{number}")),
                    )
                    .unwrap();
                    retained.push(replacement.link);
                }
                assert!(
                    reused,
                    "the bounded actual FAT32 control did not exercise directory-slot reuse; no preservation pass"
                );
            }
            "remove" => {
                for name in ["copy", "replaced", "reused-original"] {
                    let error = remove(&read(name), &[&store]).unwrap_err();
                    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
                }
                let original = read("malformed");
                let EntryIdentity::WindowsFatDirectory {
                    volume_serial,
                    file_index,
                    ..
                } = original.identity.unwrap()
                else {
                    panic!(
                        "the explicitly supplied native test filesystem is not FAT with directory allocation proof"
                    );
                };
                for identity in [
                    None,
                    Some(EntryIdentity::Windows {
                        volume_serial,
                        file_index,
                    }),
                    Some(EntryIdentity::WindowsFatDirectory {
                        volume_serial,
                        file_index,
                        first_cluster: u64::MAX,
                    }),
                    Some(EntryIdentity::WindowsFatDirectory {
                        volume_serial,
                        file_index: 0,
                        first_cluster: 0,
                    }),
                ] {
                    let restored = parse_record(&record_text(&ManagedLink {
                        identity,
                        ..original.clone()
                    }));
                    assert_eq!(
                        remove(&restored, &[&store]).unwrap_err().kind(),
                        FsxErrorKind::NotOwned
                    );
                    assert_eq!(
                        fs::read(original.destination.join("스킬.txt")).unwrap(),
                        b"synthetic bytes\r\n"
                    );
                }
                assert_eq!(
                    remove(&original, &[&store]).unwrap_err().kind(),
                    FsxErrorKind::NotOwned
                );
            }
            other => panic!("unknown role {other}"),
        }
        println!("ROLE_OK {role} process={}", std::process::id());
        return;
    }
    let Some(root) = std::env::var_os("OAH_FAT32_TEST_ROOT").map(PathBuf::from) else {
        println!(
            "SKIP actual FAT32 prerequisite: set OAH_FAT32_TEST_ROOT to a new owned path on a verified disposable FAT32 filesystem"
        );
        return;
    };
    assert!(root.is_absolute());
    fs::create_dir(&root).unwrap();
    for name in ["store", "store/skill", "agents", "records"] {
        fs::create_dir(root.join(name)).unwrap();
    }
    fs::write(root.join("store/skill/스킬.txt"), b"synthetic bytes\r\n").unwrap();
    fs::write(root.join("agents/unowned.txt"), b"unowned sibling").unwrap();
    let created = rerun(TEST, "create", &root, None);
    let replaced = rerun(TEST, "replace", &root, None);
    let removed = rerun(TEST, "remove", &root, None);
    assert_ne!(reported_process(&created), reported_process(&replaced));
    assert_ne!(reported_process(&replaced), reported_process(&removed));
    println!("{created}{replaced}{removed}");
    assert_eq!(
        names(&root.join("agents")),
        ["copy", "malformed", "replaced", "unowned.txt"]
    );
    for folder in [
        "store/skill",
        "agents/copy",
        "agents/malformed",
        "agents/replaced",
        "retained-original-with-a-longer-name",
    ] {
        assert_eq!(
            fs::read(root.join(folder).join("스킬.txt")).unwrap(),
            b"synthetic bytes\r\n"
        );
    }
    for entry in fs::read_dir(&root).unwrap() {
        let entry = entry.unwrap();
        if entry
            .file_name()
            .to_string_lossy()
            .starts_with("retained-replacement-")
        {
            assert_eq!(
                fs::read(entry.path().join("스킬.txt")).unwrap(),
                b"synthetic bytes\r\n"
            );
        }
    }
    assert_eq!(
        fs::read(root.join("agents/unowned.txt")).unwrap(),
        b"unowned sibling"
    );
    println!("LEGACY_FAT32_CREATION_AND_REFUSAL_OK");
}

#[test]
fn durable_record_legacy_native_records_preserve_absolute_and_relative_links() {
    let stores = Stores::new();
    let source = stores.source();
    let absolute = stores.agents.join("absolute");
    let relative = stores.agents.join("relative");
    let target = if cfg!(unix) {
        Path::new("../store/skill-a")
    } else {
        source.as_path()
    };
    let made = [
        legacy_native(&source, &absolute, &source),
        legacy_native(&source, &relative, target),
    ];
    if cfg!(unix) {
        assert_eq!(fs::read_link(&relative).unwrap(), target);
        assert_eq!(fs::canonicalize(&relative).unwrap(), source);
    }
    if cfg!(unix) {
        // A symlink's stored text still names its source once the source is gone.
        fs::rename(&source, stores.store.join("moved-away")).unwrap();
    }
    for record in &made {
        assert_eq!(
            remove(record, &[&stores.store]).unwrap_err().kind(),
            FsxErrorKind::NotOwned
        );
    }
    assert_eq!(names(&stores.agents), ["absolute", "relative"]);
}

#[test]
fn many_existing_native_records_remain_readable_under_descriptor_limit() {
    const TEST: &str = "many_existing_native_records_remain_readable_under_descriptor_limit";
    const LINKS: usize = 600;
    if let Some((role, root)) = child_role() {
        assert_eq!(role, "many");
        let store = root.join("store");
        let agents = root.join("agents");
        let source = store.join("skill-a");
        let mut records = Vec::new();
        for index in 0..LINKS {
            let destination = agents.join(format!("link-{index:04}"));
            let record = legacy_native(&source, &destination, &source);
            let restored = parse_record(&record_text(&record));
            assert_eq!(restored, record);
            records.push(restored);
        }

        for record in &records {
            assert_eq!(
                remove(record, &[&store]).unwrap_err().kind(),
                FsxErrorKind::NotOwned
            );
        }
        assert_eq!(names(&agents).len(), LINKS);
        println!("ROLE_OK many legacy_links={LINKS}");
        return;
    }

    let stores = Stores::new();
    let root = stores.store.parent().unwrap();
    rerun(TEST, "many", root, Some(256));
    assert_eq!(names(&stores.agents).len(), LINKS);
    assert_eq!(
        fs::read(stores.source().join("SKILL.md")).unwrap(),
        b"# Skill A\n"
    );
}

#[cfg(unix)]
#[test]
fn staging_refusal_does_not_require_opening_a_descriptor() {
    const TEST: &str = "staging_refusal_does_not_require_opening_a_descriptor";
    if let Some((role, root)) = child_role() {
        assert_eq!(role, "exhaust");
        let source = root.join("store/skill-a");
        let agents = root.join("agents");
        let destination = agents.join("skill-a");
        let request = LinkRequest {
            source: &source,
            destination: &destination,
            style: TargetStyle::Absolute,
            allow_copy: true,
        };

        let held = hold_all_but(0);
        let native = create(&request);
        let copy = create_with(&request, &StagingProbe::new(LinkKind::Copy));
        drop(held);

        for result in [native, copy] {
            let error = result.unwrap_err();
            assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported, "{error}");
            assert!(error.io_error().is_none());
            assert!(error.cleanup_error().is_none());
        }
        assert!(names(&agents).is_empty());
        println!("ROLE_OK exhaust free=0");
        return;
    }

    let stores = Stores::new();
    let root = stores.store.parent().unwrap();
    rerun(TEST, "exhaust", root, Some(256));
    assert!(names(&stores.agents).is_empty());
}

#[test]
fn durable_record_missing_invalid_or_foreign_identity_refuses_without_adoption() {
    let stores = Stores::new();
    let source = stores.source();
    for copy in [false, true] {
        let destination = stores.agents.join(if copy { "copy" } else { "link" });
        let record = if copy {
            legacy_copy(&source, &destination)
        } else {
            legacy_native(&source, &destination, &source)
        };
        let identity = record.identity.unwrap();
        let (invalid, foreign) = match identity {
            EntryIdentity::Unix { device, .. } => (
                EntryIdentity::Unix { device, inode: 0 },
                EntryIdentity::Windows {
                    volume_serial: device,
                    file_index: 1,
                },
            ),
            EntryIdentity::Windows { volume_serial, .. }
            | EntryIdentity::WindowsFatDirectory { volume_serial, .. } => (
                EntryIdentity::Windows {
                    volume_serial,
                    file_index: 0,
                },
                EntryIdentity::Unix {
                    device: volume_serial,
                    inode: 1,
                },
            ),
        };
        for proof in [None, Some(invalid), Some(foreign)] {
            let mut restored = parse_record(&record_text(&record));
            restored.identity = proof;
            let restored = parse_record(&record_text(&restored));
            let error = remove(&restored, &[&stores.store]).unwrap_err();
            assert_eq!(error.kind(), FsxErrorKind::NotOwned, "{error}");
            assert_eq!(
                fs::read(destination.join("SKILL.md")).unwrap(),
                b"# Skill A\n"
            );
        }
        assert_eq!(
            remove(&parse_record(&record_text(&record)), &[&stores.store])
                .unwrap_err()
                .kind(),
            FsxErrorKind::NotOwned
        );
    }
}
