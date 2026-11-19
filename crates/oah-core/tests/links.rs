// 관리 링크 시험: 기본 링크(macOS 심볼릭 링크·Windows 정션)와 보고되는 복사 대체, 남의 항목 보존, 링크 삭제가 대상을 지우지 않는지 확인한다.
use oah_core::fsx::links::{
    CopiedFile, Entry, EntryIdentity, LinkKind, LinkRequest, ManagedLink, NativeLinker, Removal,
    SystemLinker, TargetStyle, create, create_with, inspect, remove,
};
use oah_core::fsx::{Fingerprint, FsxErrorKind};
use std::cell::RefCell;
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

/// A native linker that always fails, to drive the copy fallback deterministically.
struct Unavailable;

impl NativeLinker for Unavailable {
    fn kind(&self) -> LinkKind {
        LinkKind::Symlink
    }

    fn link(&self, _source: &Path, _destination: &Path, _target: &Path) -> io::Result<()> {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "links are switched off here",
        ))
    }
}

struct Misdirected {
    elsewhere: PathBuf,
}

impl NativeLinker for Misdirected {
    fn kind(&self) -> LinkKind {
        SystemLinker.kind()
    }

    fn link(&self, source: &Path, destination: &Path, target: &Path) -> io::Result<()> {
        SystemLinker.link(source, destination, target)?;
        #[cfg(windows)]
        fs::remove_dir(destination)?;
        #[cfg(not(windows))]
        fs::remove_file(destination)?;
        SystemLinker.link(&self.elsewhere, destination, &self.elsewhere)
    }
}

/// Another writer that takes the public destination while OAH's native attempt runs.
struct DestinationTaken {
    destination: PathBuf,
    elsewhere: PathBuf,
}

impl NativeLinker for DestinationTaken {
    fn kind(&self) -> LinkKind {
        SystemLinker.kind()
    }

    fn link(&self, source: &Path, destination: &Path, target: &Path) -> io::Result<()> {
        SystemLinker.link(source, destination, target)?;
        SystemLinker.link(&self.elsewhere, &self.destination, &self.elsewhere)
    }
}

/// What a failing native linker leaves at the path it was given before it reports the failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Leaves {
    Nothing,
    /// junction 2.1.0 makes the folder first and then sets the reparse data, which a volume without reparse
    /// points refuses; it does not remove the folder again.
    Folder,
    Link,
    FolderWithFile,
}

struct FailsAfter {
    leaves: Leaves,
    kind: LinkKind,
}

impl NativeLinker for FailsAfter {
    fn kind(&self) -> LinkKind {
        self.kind
    }

    fn link(&self, source: &Path, destination: &Path, target: &Path) -> io::Result<()> {
        match self.leaves {
            Leaves::Nothing => {}
            Leaves::Folder => fs::create_dir(destination)?,
            Leaves::Link => SystemLinker.link(source, destination, target)?,
            Leaves::FolderWithFile => {
                fs::create_dir(destination)?;
                fs::write(destination.join("partial.txt"), "partial")?;
            }
        }
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "synthetic: this volume has no reparse points",
        ))
    }
}

/// The system linker, noting every path and target it was handed.
#[derive(Default)]
struct Recording {
    seen: RefCell<Vec<(PathBuf, PathBuf)>>,
}

impl NativeLinker for Recording {
    fn kind(&self) -> LinkKind {
        SystemLinker.kind()
    }

    fn link(&self, source: &Path, destination: &Path, target: &Path) -> io::Result<()> {
        self.seen
            .borrow_mut()
            .push((destination.to_path_buf(), target.to_path_buf()));
        SystemLinker.link(source, destination, target)
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
fn a_native_link_points_at_the_store_and_is_reported_once() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let request = stores.request(&destination, TargetStyle::Absolute, false, &source);
    let outcome = create(&request).unwrap();
    assert!(outcome.created);
    assert!(outcome.fallback.is_none());
    assert_eq!(outcome.link.kind, native_kind());
    assert_eq!(outcome.link.target.as_deref(), Some(source.as_path()));
    match inspect(&destination).unwrap() {
        Entry::Symlink { target } | Entry::Junction { target } => {
            assert_eq!(fs::canonicalize(target).unwrap(), source);
        }
        other => panic!("not a link: {other:?}"),
    }
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert_eq!(
        create(&request).unwrap_err().kind(),
        FsxErrorKind::LinkExists
    );
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
}

#[cfg(unix)]
#[test]
fn a_project_link_stores_a_relative_target() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome =
        create(&stores.request(&destination, TargetStyle::Relative, false, &source)).unwrap();
    let target = outcome.link.target.clone().unwrap();
    assert!(target.is_relative(), "{target:?}");
    assert_eq!(fs::read_link(&destination).unwrap(), target);
    assert_eq!(fs::canonicalize(&destination).unwrap(), source);
}

#[test]
fn an_occupied_destination_is_left_alone() {
    let stores = Stores::new();
    let source = stores.source();
    let folder = stores.agents.join("mine");
    fs::create_dir(&folder).unwrap();
    fs::write(folder.join("notes.md"), "user text").unwrap();
    let error = create(&stores.request(&folder, TargetStyle::Absolute, true, &source)).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkExists);
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
    let outcome =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &source)).unwrap();
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap_err().kind(),
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
        remove(&outcome.link, &[&stores.store]).unwrap(),
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
    let outcome =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &source)).unwrap();

    let elsewhere = stores.agents.join("unrelated-store");
    let error = remove(&outcome.link, &[&elsewhere]).unwrap_err();
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
    let replaced =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &other)).unwrap();
    let error = remove(&outcome.link, &[&stores.store]).unwrap_err();
    assert_eq!(
        error.code(),
        "OAH_LINK_NOT_OWNED",
        "the link now points somewhere else"
    );
    assert_eq!(
        remove(&replaced.link, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(fs::canonicalize(&destination).unwrap(), other);
}

#[test]
fn a_failed_native_link_falls_back_to_a_reported_copy() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome = create_with(
        &stores.request(&destination, TargetStyle::Absolute, true, &source),
        &Unavailable,
    )
    .unwrap();
    assert_eq!(outcome.link.kind, LinkKind::Copy);
    let reason = outcome
        .fallback
        .expect("a copy reports why the native link was not made");
    assert!(
        reason.contains("unavailable") && reason.contains("links are switched off here"),
        "{reason}"
    );
    assert!(outcome.link.target.is_none());
    assert_eq!(inspect(&destination).unwrap(), Entry::Directory);
    let files: Vec<&Path> = outcome
        .link
        .files
        .iter()
        .map(|file| file.relative.as_path())
        .collect();
    assert_eq!(
        files,
        [Path::new("SKILL.md"), &Path::new("scripts").join("run.sh")]
    );
    assert_eq!(outcome.link.folders, [PathBuf::from("scripts")]);
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );

    fs::write(source.join("SKILL.md"), "# Changed\n").unwrap();
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Skill A\n",
        "a copy does not follow the source"
    );
}

#[test]
fn without_permission_to_copy_a_failed_link_is_an_error() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let error = create_with(
        &stores.request(&destination, TargetStyle::Absolute, false, &source),
        &Unavailable,
    )
    .unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported);
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
}

#[test]
fn cleanup_ownership_native_replacement_survives_with_or_without_copy() {
    // The attempt runs in OAH's private folder, so another writer can only take the public destination. A
    // destination taken before publication is refused by the non-replacing rename and is left exactly as it is.
    let stores = Stores::new();
    let source = stores.source();
    let elsewhere = stores.store.join("elsewhere");
    fs::create_dir_all(&elsewhere).unwrap();
    fs::write(elsewhere.join("keep.txt"), "kept\n").unwrap();
    for allow_copy in [false, true] {
        let destination = stores.agents.join(format!("skill-{allow_copy}"));
        let linker = DestinationTaken {
            destination: destination.clone(),
            elsewhere: elsewhere.clone(),
        };
        let error = create_with(
            &stores.request(&destination, TargetStyle::Absolute, allow_copy, &source),
            &linker,
        )
        .unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::LinkExists, "{error}");
        assert!(error.cleanup_error().is_none(), "{error}");
        assert!(matches!(
            inspect(&destination).unwrap(),
            Entry::Symlink { .. } | Entry::Junction { .. }
        ));
        assert_eq!(fs::canonicalize(&destination).unwrap(), elsewhere);
    }
    assert_eq!(names(&stores.agents), ["skill-false", "skill-true"]);
    assert_eq!(
        fs::read_to_string(elsewhere.join("keep.txt")).unwrap(),
        "kept\n",
        "removing the wrong link touched its target"
    );
}

#[test]
fn cleanup_ownership_misdirected_attempt_is_discarded_privately() {
    // A link that does not store the requested target is the attempt's own unusable result inside OAH's
    // private folder: it is removed there (never its target), never published, and a copy may stand in.
    let stores = Stores::new();
    let source = stores.source();
    let elsewhere = stores.store.join("elsewhere");
    fs::create_dir_all(&elsewhere).unwrap();
    fs::write(elsewhere.join("keep.txt"), "kept\n").unwrap();
    let linker = Misdirected {
        elsewhere: elsewhere.clone(),
    };
    let refused = stores.agents.join("refused");
    let error = create_with(
        &stores.request(&refused, TargetStyle::Absolute, false, &source),
        &linker,
    )
    .unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported);
    assert_eq!(inspect(&refused).unwrap(), Entry::Missing);
    let copied = stores.agents.join("copied");
    let outcome = create_with(
        &stores.request(&copied, TargetStyle::Absolute, true, &source),
        &linker,
    )
    .unwrap();
    assert_eq!(outcome.link.kind, LinkKind::Copy);
    assert!(
        outcome
            .fallback
            .as_deref()
            .unwrap()
            .contains("did not make a link storing the requested target")
    );
    assert_eq!(
        fs::read_to_string(copied.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
    assert_eq!(names(&stores.agents), ["copied"]);
    assert_eq!(
        fs::read_to_string(elsewhere.join("keep.txt")).unwrap(),
        "kept\n"
    );
}

#[test]
fn cleanup_ownership_residue_from_failed_native_attempts_is_cleaned() {
    // Whatever a failed attempt leaves is inside OAH's private attempt folder, so it is the attempt's own:
    // it is removed and the destination stays free for the designed copy fallback (never LinkExists).
    let stores = Stores::new();
    let source = stores.source();
    for (index, leaves) in [Leaves::Nothing, Leaves::Folder, Leaves::Link]
        .into_iter()
        .enumerate()
    {
        let kind = if leaves == Leaves::Folder {
            LinkKind::Junction
        } else {
            native_kind()
        };
        let linker = FailsAfter { leaves, kind };
        let destination = stores.agents.join(format!("skill-{index}"));
        let error = create_with(
            &stores.request(&destination, TargetStyle::Absolute, false, &source),
            &linker,
        )
        .unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported, "{leaves:?}");
        assert!(error.cleanup_error().is_none(), "{leaves:?}: {error}");
        assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
        assert_eq!(names(&stores.agents), Vec::<String>::new(), "{leaves:?}");

        let outcome = create_with(
            &stores.request(&destination, TargetStyle::Absolute, true, &source),
            &linker,
        )
        .unwrap();
        assert_eq!(outcome.link.kind, LinkKind::Copy, "{leaves:?}");
        let reason = outcome.fallback.as_deref().unwrap();
        assert!(
            reason.contains("unavailable")
                && reason.contains("synthetic: this volume has no reparse points"),
            "{reason}"
        );
        assert_eq!(
            fs::read_to_string(destination.join("SKILL.md")).unwrap(),
            "# Skill A\n"
        );
        assert_eq!(names(&stores.agents), [format!("skill-{index}")]);
        fs::remove_dir_all(&destination).unwrap();
    }

    // A residue the attempt cannot remove (something was put inside its folder) stays where it is and is
    // reported together with the original failure; no copy papers over it.
    let linker = FailsAfter {
        leaves: Leaves::FolderWithFile,
        kind: LinkKind::Junction,
    };
    let destination = stores.agents.join("kept");
    let error = create_with(
        &stores.request(&destination, TargetStyle::Absolute, true, &source),
        &linker,
    )
    .unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported, "{error}");
    let cleanup = error.cleanup_error().expect("the residue is reported");
    assert!(cleanup.path().starts_with(&stores.agents), "{cleanup}");
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
    let left = names(&stores.agents);
    assert_eq!(left.len(), 1, "{left:?}");
    assert!(
        left[0].starts_with(".kept.") && left[0].ends_with(".attempt.tmp"),
        "{left:?}"
    );
    assert_eq!(
        fs::read_to_string(
            stores
                .agents
                .join(&left[0])
                .join("kept")
                .join("partial.txt")
        )
        .unwrap(),
        "partial"
    );
    assert_eq!(
        fs::read_to_string(source.join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
}

#[test]
fn cleanup_ownership_native_attempt_never_touches_the_destination() {
    let stores = Stores::new();
    let source = stores.source();
    let linker = Recording::default();
    let destination = stores.agents.join("skill-a");
    let outcome = create_with(
        &stores.request(&destination, TargetStyle::Absolute, false, &source),
        &linker,
    )
    .unwrap();
    let (given, target) = linker.seen.borrow()[0].clone();
    assert_ne!(given, destination);
    assert_eq!(given.file_name(), destination.file_name());
    let folder = given.parent().unwrap();
    assert_eq!(folder.parent().unwrap(), stores.agents);
    let folder_name = folder.file_name().unwrap().to_string_lossy();
    assert!(
        folder_name.starts_with(".skill-a.") && folder_name.ends_with(".attempt.tmp"),
        "{folder_name}"
    );
    assert_eq!(outcome.link.target.as_deref(), Some(target.as_path()));
    assert_eq!(fs::canonicalize(&destination).unwrap(), source);
    assert_eq!(names(&stores.agents), ["skill-a"]);

    if cfg!(unix) {
        // A relative target is handed over exactly as it must read at the destination, relative to the
        // destination's folder; it is never rewritten for the private folder and resolves once published.
        let relative = stores.agents.join("skill-relative");
        let outcome = create_with(
            &stores.request(&relative, TargetStyle::Relative, false, &source),
            &linker,
        )
        .unwrap();
        let (_, target) = linker.seen.borrow()[1].clone();
        assert!(target.is_relative(), "{target:?}");
        assert_eq!(outcome.link.target.as_deref(), Some(target.as_path()));
        assert_eq!(fs::read_link(&relative).unwrap(), target);
        assert_eq!(fs::canonicalize(&relative).unwrap(), source);
        assert_eq!(names(&stores.agents), ["skill-a", "skill-relative"]);
    }
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
    let owned =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &source)).unwrap();
    let saved = stores.agents.join("original-link");
    fs::rename(&destination, &saved).unwrap();

    let other = stores.store.join("skill-b");
    fs::create_dir(&other).unwrap();
    fs::write(other.join("SKILL.md"), "# Skill B\n").unwrap();
    SystemLinker.link(&other, &destination, &other).unwrap();
    let error = remove(&owned.link, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(fs::canonicalize(&destination).unwrap(), other);
    unlink(&destination);

    fs::create_dir(&destination).unwrap();
    fs::write(destination.join("notes.md"), "user text").unwrap();
    let error = remove(&owned.link, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(
        fs::read_to_string(destination.join("notes.md")).unwrap(),
        "user text"
    );
    fs::remove_dir_all(&destination).unwrap();

    fs::write(&destination, "a user file").unwrap();
    let error = remove(&owned.link, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(fs::read_to_string(&destination).unwrap(), "a user file");
    fs::remove_file(&destination).unwrap();

    SystemLinker.link(&source, &destination, &source).unwrap();
    let error = remove(&owned.link, &[&stores.store]).unwrap_err();
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
    let outcome = create_with(
        &stores.request(&destination, TargetStyle::Absolute, true, &source),
        &Unavailable,
    )
    .unwrap();
    fs::write(destination.join("SKILL.md"), "# Edited by the user\n").unwrap();
    let error = remove(&outcome.link, &[&stores.store]).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Edited by the user\n"
    );

    fs::write(destination.join("SKILL.md"), "# Skill A\n").unwrap();
    fs::write(destination.join("added.txt"), "new").unwrap();
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );

    fs::remove_file(destination.join("added.txt")).unwrap();
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap_err().kind(),
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
        let canonical = create_with(
            &stores.request(&destination, TargetStyle::Absolute, true, &source),
            &Unavailable,
        )
        .unwrap()
        .link;
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

#[cfg(unix)]
#[test]
fn copying_never_follows_links() {
    let stores = Stores::new();
    let source = stores.source();
    let outside = stores.agents.join("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(outside.join("private.txt"), "not part of the skill").unwrap();
    std::os::unix::fs::symlink(&outside, source.join("escape")).unwrap();
    let destination = stores.agents.join("skill-a");
    let error = create_with(
        &stores.request(&destination, TargetStyle::Absolute, true, &source),
        &Unavailable,
    )
    .unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::TreeHasLink);
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
    let leftovers: Vec<_> = fs::read_dir(&stores.agents)
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    assert_eq!(
        leftovers,
        ["outside"],
        "the staging copy was not cleaned up"
    );
}

#[cfg(unix)]
#[test]
fn a_copy_keeps_the_executable_bit() {
    use std::os::unix::fs::PermissionsExt;
    let stores = Stores::new();
    let source = stores.source();
    let script = source.join("scripts").join("run.sh");
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
    let destination = stores.agents.join("skill-a");
    let outcome = create_with(
        &stores.request(&destination, TargetStyle::Absolute, true, &source),
        &Unavailable,
    )
    .unwrap();
    let recorded = outcome
        .link
        .files
        .iter()
        .find(|file| file.relative.ends_with("run.sh"))
        .unwrap();
    assert!(recorded.executable);
    let mode = fs::metadata(destination.join("scripts").join("run.sh"))
        .unwrap()
        .permissions()
        .mode();
    assert_ne!(mode & 0o111, 0);
}

#[cfg(windows)]
#[test]
fn a_real_junction_is_made_without_elevation() {
    let stores = Stores::new();
    let source = stores.source();
    let destination = stores.agents.join("skill-a");
    let outcome =
        create(&stores.request(&destination, TargetStyle::Relative, false, &source)).unwrap();
    assert_eq!(outcome.link.kind, LinkKind::Junction);
    assert_eq!(
        outcome.link.target.as_deref(),
        Some(source.as_path()),
        "junctions store an absolute target"
    );
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
fn a_junction_refused_after_its_folder_was_made_leaves_nothing_and_can_be_copied() {
    // junction 2.1.0 makes the folder before it checks that the target fits the reparse data, and leaves the
    // folder when it does not. That folder lands in OAH's private attempt folder, so it is removed again and the
    // destination stays free for the reported copy.
    let stores = Stores::new();
    let mut source = stores.store.clone();
    while source.as_os_str().len() <= 4200 {
        source.push("d".repeat(120));
    }
    fs::create_dir_all(&source).unwrap();
    fs::write(source.join("SKILL.md"), "# Long\n").unwrap();
    let destination = stores.agents.join("skill-long");
    let error =
        create(&stores.request(&destination, TargetStyle::Absolute, false, &source)).unwrap_err();
    assert_eq!(error.kind(), FsxErrorKind::LinkUnsupported, "{error}");
    assert!(
        error.to_string().contains("`target` is too long"),
        "{error}"
    );
    assert!(error.cleanup_error().is_none(), "{error}");
    assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
    assert_eq!(names(&stores.agents), Vec::<String>::new());
    let outcome =
        create(&stores.request(&destination, TargetStyle::Absolute, true, &source)).unwrap();
    assert_eq!(outcome.link.kind, LinkKind::Copy);
    let reason = outcome.fallback.as_deref().unwrap();
    assert!(
        reason.contains("junction unavailable") && reason.contains("`target` is too long"),
        "{reason}"
    );
    assert_eq!(
        fs::read_to_string(destination.join("SKILL.md")).unwrap(),
        "# Long\n"
    );
    assert_eq!(
        remove(&outcome.link, &[&stores.store]).unwrap_err().kind(),
        FsxErrorKind::NotOwned
    );
    assert_eq!(fs::read(destination.join("SKILL.md")).unwrap(), b"# Long\n");
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
    a_real_junction_is_made_without_elevation();
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
                    let request = LinkRequest {
                        source: &source,
                        destination: &destination,
                        style: TargetStyle::Absolute,
                        allow_copy: copy,
                    };
                    let outcome = if copy {
                        create_with(&request, &Unavailable)
                    } else {
                        create(&request)
                    }
                    .unwrap();
                    let text = record_text(&outcome.link);
                    assert_eq!(
                        parse_record(&text),
                        outcome.link,
                        "the text is the whole record"
                    );
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
    let made = [
        create(&stores.request(&absolute, TargetStyle::Absolute, false, &source)).unwrap(),
        create(&stores.request(&relative, TargetStyle::Relative, false, &source)).unwrap(),
    ];
    if cfg!(unix) {
        // A symlink's stored text still names its source once the source is gone.
        fs::rename(&source, stores.store.join("moved-away")).unwrap();
    }
    for outcome in &made {
        assert_eq!(
            remove(&outcome.link, &[&stores.store]).unwrap_err().kind(),
            FsxErrorKind::NotOwned
        );
    }
    assert_eq!(names(&stores.agents), ["absolute", "relative"]);
}

#[test]
fn durable_record_many_links_and_files_stay_within_256_descriptors() {
    // Records keep nothing open, so a run that holds hundreds of records, or copies hundreds of files, fits in a
    // process limited to 256 open descriptors.
    const TEST: &str = "durable_record_many_links_and_files_stay_within_256_descriptors";
    const LINKS: usize = 600;
    const FILES: usize = 600;
    if let Some((role, root)) = child_role() {
        assert_eq!(role, "many");
        if cfg!(unix) {
            drop(hold_all_but(0));
        }
        let store = root.join("store");
        let agents = root.join("agents");
        let source = store.join("skill-a");
        let mut records = Vec::new();
        for index in 0..LINKS {
            let destination = agents.join(format!("link-{index:04}"));
            let request = LinkRequest {
                source: &source,
                destination: &destination,
                style: TargetStyle::Absolute,
                allow_copy: false,
            };
            let link = create(&request).unwrap().link;
            let restored = parse_record(&record_text(&link));
            assert_eq!(restored, link);
            assert!(restored.identity.is_some());
            records.push(restored);
        }
        let big = store.join("big");
        for index in 0..FILES {
            let folder = big.join(format!("part-{:02}", index % 8));
            fs::create_dir_all(&folder).unwrap();
            fs::write(
                folder.join(format!("file-{index:04}.md")),
                format!("synthetic {index}\n"),
            )
            .unwrap();
        }
        let copied = agents.join("big");
        let request = LinkRequest {
            source: &big,
            destination: &copied,
            style: TargetStyle::Absolute,
            allow_copy: true,
        };
        let outcome = create_with(&request, &Unavailable).unwrap();
        assert_eq!(outcome.link.kind, LinkKind::Copy);
        assert_eq!(outcome.link.files.len(), FILES);
        let restored = parse_record(&record_text(&outcome.link));
        assert_eq!(restored, outcome.link);
        assert!(restored.identity.is_some());
        records.push(restored);
        for record in &records {
            assert_eq!(
                remove(record, &[&store]).unwrap_err().kind(),
                FsxErrorKind::NotOwned
            );
        }
        assert_eq!(names(&agents).len(), LINKS + 1);
        println!("ROLE_OK many links={LINKS} files={FILES}");
        return;
    }
    let stores = Stores::new();
    let root = stores.store.parent().unwrap().to_path_buf();
    rerun(TEST, "many", &root, Some(256));
    assert_eq!(names(&stores.agents).len(), LINKS + 1);
    assert_eq!(
        fs::read_to_string(stores.source().join("SKILL.md")).unwrap(),
        "# Skill A\n"
    );
}

#[cfg(unix)]
#[test]
fn durable_record_descriptor_exhaustion_is_an_io_error_without_residue() {
    // Running out of descriptors during a copy is reported as the IO error it is and leaves nothing behind, and a
    // copy needs no more than two free descriptors.
    const TEST: &str = "durable_record_descriptor_exhaustion_is_an_io_error_without_residue";
    if let Some((role, root)) = child_role() {
        assert_eq!(role, "exhaust");
        let store = root.join("store");
        let agents = root.join("agents");
        let source = store.join("skill-a");
        let destination = agents.join("skill-a");
        let request = LinkRequest {
            source: &source,
            destination: &destination,
            style: TargetStyle::Absolute,
            allow_copy: true,
        };
        // One free descriptor is too few: a folder stays open while one of its files is fingerprinted.
        let held = hold_all_but(1);
        let result = create_with(&request, &Unavailable);
        drop(held);
        let error = result.unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::Io, "{error}");
        assert_eq!(
            error.io_error().and_then(io::Error::raw_os_error),
            Some(24),
            "{error}"
        );
        assert!(error.cleanup_error().is_none(), "{error}");
        assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
        assert_eq!(names(&agents), Vec::<String>::new());
        println!("ROLE_OK exhaust free=1 error={error}");
        // Two are enough: copying keeps at most two files open, and neither the copy nor its record keeps any.
        let held = hold_all_but(2);
        let outcome = create_with(&request, &Unavailable).unwrap();
        assert_eq!(outcome.link.kind, LinkKind::Copy);
        assert_eq!(
            fs::read_to_string(destination.join("SKILL.md")).unwrap(),
            "# Skill A\n"
        );
        drop(held);
        assert_eq!(names(&agents), ["skill-a"]);
        println!("ROLE_OK exhaust free=2 copied={}", outcome.link.files.len());
        return;
    }
    let stores = Stores::new();
    let root = stores.store.parent().unwrap().to_path_buf();
    let output = rerun(TEST, "exhaust", &root, Some(256));
    assert!(output.contains("os error 24"), "{output}");
    assert_eq!(names(&stores.agents), ["skill-a"]);
}

#[test]
fn durable_record_missing_invalid_or_foreign_identity_refuses_without_adoption() {
    let stores = Stores::new();
    let source = stores.source();
    for copy in [false, true] {
        let destination = stores.agents.join(if copy { "copy" } else { "link" });
        let request = stores.request(&destination, TargetStyle::Absolute, copy, &source);
        let outcome = if copy {
            create_with(&request, &Unavailable)
        } else {
            create(&request)
        }
        .unwrap();
        let identity = outcome.link.identity.unwrap();
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
            let mut restored = parse_record(&record_text(&outcome.link));
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
            remove(&parse_record(&record_text(&outcome.link)), &[&stores.store])
                .unwrap_err()
                .kind(),
            FsxErrorKind::NotOwned
        );
    }
}
