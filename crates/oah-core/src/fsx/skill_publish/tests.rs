use super::*;
use std::fs;
use tempfile::TempDir;

pub(super) fn binding(revision: u8) -> ReviewBinding {
    ReviewBinding {
        repo: "fixture/cabinet".into(),
        path: "skills/notes".into(),
        review_id: format!("sha256:{}", Fingerprint::of(&[revision, 1]).to_hex()),
        snapshot_id: format!("sha256:{}", Fingerprint::of(&[revision, 2]).to_hex()),
    }
}

pub(super) fn files(revision: u8) -> BTreeMap<String, Vec<u8>> {
    BTreeMap::from([
        (
            "SKILL.md".into(),
            b"---\nname: notes\ndescription: A fixture.\n---\n".to_vec(),
        ),
        ("references/nested/note.txt".into(), vec![revision, 0, 255]),
    ])
}

pub(super) struct Fixture {
    pub root: ProjectRoot,
    pub leaf: SkillLeaf,
    pub path: PathBuf,
    pub home: PathBuf,
    _temp: TempDir,
}

impl Fixture {
    pub fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let base = temp.path().canonicalize().unwrap();
        let path = base.join("project");
        let home = base.join("home");
        fs::create_dir(&path).unwrap();
        fs::create_dir(&home).unwrap();
        Self {
            root: ProjectRoot::open(&path, &home).unwrap(),
            leaf: SkillLeaf::new("notes").unwrap(),
            path,
            home,
            _temp: temp,
        }
    }
    pub fn apply(
        &self,
        revision: u8,
        expected: Option<&str>,
    ) -> Result<PublishStatus, PublishError> {
        self.root.apply(
            Engine::Codex,
            &self.leaf,
            &binding(revision),
            &files(revision),
            expected,
        )
    }
    pub fn live(&self) -> PathBuf {
        self.path.join(".agents/skills/notes")
    }
}

#[test]
fn publishes_exact_nested_bytes_for_both_engines_and_reopens_after_restart() {
    let f = Fixture::new();
    for (engine, parent) in [(Engine::Claude, ".claude"), (Engine::Codex, ".agents")] {
        assert!(f.root.installed(engine, &f.leaf).unwrap().is_none());
        assert!(!f.path.join(parent).exists());
        assert_eq!(
            f.root
                .apply(engine, &f.leaf, &binding(1), &files(1), None)
                .unwrap(),
            PublishStatus::Installed
        );
        let reopened =
            ProjectRoot::open(&crate::paths::canonical_folder(&f.path).unwrap(), &f.home).unwrap();
        let acq = reopened.installed(engine, &f.leaf).unwrap().unwrap();
        assert_eq!(acq.binding, binding(1));
        assert_eq!(
            acq.files.keys().collect::<Vec<_>>(),
            files(1).keys().collect::<Vec<_>>()
        );
        let live = f.path.join(parent).join("skills/notes");
        for (path, data) in files(1) {
            assert_eq!(fs::read(live.join(path)).unwrap(), data);
        }
        assert_eq!(fs::read_dir(&live).unwrap().count(), 2);
        assert_eq!(
            reopened
                .apply(engine, &f.leaf, &binding(1), &files(1), None)
                .unwrap(),
            PublishStatus::Unchanged
        );
        assert_eq!(reopened.installed(engine, &f.leaf).unwrap().unwrap(), acq);
    }
}

#[test]
fn update_preserves_neighbors_and_retries_only_the_expected_revisions() {
    let f = Fixture::new();
    f.apply(1, None).unwrap();
    let prior = f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap();
    let neighbor = f.path.join(".agents/skills/unmanaged");
    fs::create_dir(&neighbor).unwrap();
    fs::write(neighbor.join("note"), b"keep").unwrap();
    let mut different = binding(2);
    different.path = "other/notes".into();
    assert!(matches!(
        f.root.apply(
            Engine::Codex,
            &f.leaf,
            &different,
            &files(2),
            Some(&prior.binding.review_id)
        ),
        Err(PublishError::SourceMismatch)
    ));
    assert!(matches!(
        f.apply(2, Some(&binding(9).review_id)),
        Err(PublishError::StaleRevision)
    ));
    assert!(matches!(f.apply(2, None), Err(PublishError::Occupied)));
    assert_eq!(
        f.apply(2, Some(&prior.binding.review_id)).unwrap(),
        PublishStatus::Updated
    );
    assert_eq!(
        f.apply(2, Some(&prior.binding.review_id)).unwrap(),
        PublishStatus::Unchanged
    );
    assert!(matches!(
        f.apply(3, Some(&prior.binding.review_id)),
        Err(PublishError::StaleRevision)
    ));
    let now = f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap();
    assert_eq!(now.binding, binding(2));
    assert_ne!(now.directory, prior.directory);
    assert_eq!(
        fs::read(f.live().join("references/nested/note.txt")).unwrap(),
        [2, 0, 255]
    );
    assert_eq!(fs::read(neighbor.join("note")).unwrap(), b"keep");
}

#[test]
fn changed_bytes_extra_entries_and_identical_foreign_copies_never_grant_authority() {
    for variant in ["edit", "extra", "copy", "untracked"] {
        let f = Fixture::new();
        if variant != "untracked" {
            f.apply(1, None).unwrap();
        }
        let live = f.live();
        match variant {
            "edit" => fs::write(live.join("references/nested/note.txt"), b"local").unwrap(),
            "extra" => fs::create_dir(live.join("local-empty")).unwrap(),
            "copy" | "untracked" => {
                if variant == "copy" {
                    fs::rename(&live, f.path.join("original")).unwrap();
                }
                fs::create_dir_all(live.join("references/nested")).unwrap();
                for (name, data) in files(1) {
                    fs::write(live.join(name), data).unwrap();
                }
            }
            _ => unreachable!(),
        }
        let before = fs::read(live.join("references/nested/note.txt")).unwrap();
        assert!(
            f.apply(2, Some(&binding(1).review_id)).is_err(),
            "{variant}"
        );
        assert!(f.apply(1, None).is_err(), "{variant}");
        assert!(
            !matches!(f.root.installed(Engine::Codex, &f.leaf), Ok(Some(_))),
            "{variant}"
        );
        assert_eq!(
            fs::read(live.join("references/nested/note.txt")).unwrap(),
            before
        );
        if variant == "extra" {
            assert!(live.join("local-empty").is_dir());
        }
    }
}

fn directory_link(to: &Path, at: &Path) {
    #[cfg(target_os = "macos")]
    std::os::unix::fs::symlink(to, at).unwrap();
    #[cfg(windows)]
    junction::create(to, at).unwrap();
}

#[test]
fn refuses_linked_project_ancestors_state_engine_and_nested_acquisitions() {
    for name in [".agents", ".oah", "live-nested", "selected"] {
        let f = Fixture::new();
        let outside = f.path.join("elsewhere");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("keep"), b"foreign").unwrap();
        match name {
            "live-nested" => {
                f.apply(1, None).unwrap();
                let nested = f.live().join("references/nested");
                fs::rename(&nested, f.path.join("user-saved")).unwrap();
                directory_link(&outside, &nested);
                assert!(f.apply(2, Some(&binding(1).review_id)).is_err());
            }
            "selected" => {
                let alias = f.path.join("alias");
                directory_link(&outside, &alias);
                assert!(ProjectRoot::open(&alias, &f.home).is_err());
            }
            _ => {
                directory_link(&outside, &f.path.join(name));
                assert!(f.apply(1, None).is_err());
            }
        }
        assert_eq!(fs::read_dir(&outside).unwrap().count(), 1);
        assert_eq!(fs::read(outside.join("keep")).unwrap(), b"foreign");
    }
}

#[test]
fn refuses_invalid_input_before_creating_project_state() {
    let f = Fixture::new();
    for name in ["../notes", "a/b", "CON", "bad:stream", ".git"] {
        assert!(SkillLeaf::new(name).is_err());
    }
    for path in ["../outside", "references/NESTED/note.txt", "SKILL.md/more"] {
        let mut payload = files(1);
        payload.insert(path.into(), vec![1]);
        assert!(matches!(
            f.root
                .apply(Engine::Codex, &f.leaf, &binding(1), &payload, None),
            Err(PublishError::InvalidPayload)
        ));
    }
    assert_eq!(fs::read_dir(&f.path).unwrap().count(), 0);
}

#[test]
fn native_nonreplacing_move_preserves_an_occupied_directory() {
    let f = Fixture::new();
    let root = f.root.project.pin().unwrap();
    let source = root.create_dir("source").unwrap();
    let id = source.id().clone();
    source.write_new("note", b"original").unwrap();
    drop(source);
    let empty = root.create_dir("empty").unwrap();
    let empty_id = empty.id().clone();
    drop(empty);
    assert!(os::rename_new(&root, "source", &root, "empty", &id).is_err());
    assert_eq!(root.child("empty").unwrap().id(), &empty_id);
    assert_eq!(fs::read_dir(f.path.join("empty")).unwrap().count(), 0);
    fs::create_dir(f.path.join("occupied")).unwrap();
    fs::write(f.path.join("occupied/note"), b"neighbor").unwrap();
    assert!(os::rename_new(&root, "source", &root, "occupied", &id).is_err());
    assert_eq!(fs::read(f.path.join("source/note")).unwrap(), b"original");
    assert_eq!(fs::read(f.path.join("occupied/note")).unwrap(), b"neighbor");
}

#[test]
fn a_new_review_of_unchanged_files_keeps_the_exact_prior_acquisition() {
    let f = Fixture::new();
    f.apply(1, None).unwrap();
    let prior = f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap();
    let same_files = |expected| {
        f.root
            .apply(Engine::Codex, &f.leaf, &binding(2), &files(1), expected)
    };
    assert_eq!(
        same_files(Some(&prior.binding.review_id)).unwrap(),
        PublishStatus::Unchanged
    );
    assert!(matches!(same_files(None), Err(PublishError::Occupied)));
    assert_eq!(
        f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap(),
        prior
    );
}

#[test]
fn a_new_install_can_follow_removal_of_the_managed_leaf() {
    let f = Fixture::new();
    f.apply(1, None).unwrap();
    f.apply(2, Some(&binding(1).review_id)).unwrap();
    fs::remove_dir_all(f.live()).unwrap();
    assert!(f.apply(3, Some(&binding(2).review_id)).is_err());
    assert!(!f.live().exists());

    let mut new_source = binding(3);
    new_source.repo = "fixture/replacement".into();
    let install = || {
        f.root
            .apply(Engine::Codex, &f.leaf, &new_source, &files(3), None)
    };
    assert_eq!(install().unwrap(), PublishStatus::Installed);
    assert_eq!(install().unwrap(), PublishStatus::Unchanged);
    let live = f.root.installed(Engine::Codex, &f.leaf).unwrap().unwrap();
    assert_eq!(live.binding, new_source);
    assert_eq!(
        fs::read(f.live().join("references/nested/note.txt")).unwrap(),
        [3, 0, 255]
    );
}

#[test]
fn user_added_attached_data_survives_a_refused_update() {
    let f = Fixture::new();
    f.apply(1, None).unwrap();
    let skill = f.live().join("SKILL.md");
    #[cfg(target_os = "macos")]
    {
        use rustix::fs::{XattrFlags, fsetxattr};
        fsetxattr(
            fs::File::open(&skill).unwrap(),
            "com.fixture.note",
            b"local",
            XattrFlags::CREATE,
        )
        .unwrap();
    }
    #[cfg(windows)]
    fs::write(f.live().join("SKILL.md:local-note"), b"local").unwrap();
    assert!(f.apply(2, Some(&binding(1).review_id)).is_err());
    assert_eq!(fs::read(skill).unwrap(), files(1)["SKILL.md"]);
    #[cfg(windows)]
    assert_eq!(
        fs::read(f.live().join("SKILL.md:local-note")).unwrap(),
        b"local"
    );
}

#[test]
fn a_user_ignore_file_is_preserved_and_private_state_is_ignored_by_git() {
    let f = Fixture::new();
    let state = f.path.join(".oah/project-skills-v1");
    fs::create_dir_all(&state).unwrap();
    fs::write(state.join(".gitignore"), b"custom rule\n").unwrap();
    assert!(matches!(f.apply(1, None), Err(PublishError::Occupied)));
    assert_eq!(
        fs::read(state.join(".gitignore")).unwrap(),
        b"custom rule\n"
    );
    fs::rename(state.join(".gitignore"), f.path.join("custom-rule")).unwrap();
    f.apply(1, None).unwrap();
    let git = |args: &[&str]| {
        std::process::Command::new("git")
            .current_dir(&f.path)
            .args(args)
            .output()
            .unwrap()
    };
    assert!(git(&["init", "-q"]).status.success());
    assert!(
        git(&[
            "check-ignore",
            "--quiet",
            ".oah/project-skills-v1/writer.lock"
        ])
        .status
        .success()
    );
    assert!(
        !git(&["check-ignore", "--quiet", ".agents/skills/notes/SKILL.md"])
            .status
            .success()
    );
}

#[test]
fn rejects_the_session_home_and_its_metadata_trees() {
    let f = Fixture::new();
    assert!(matches!(
        ProjectRoot::open(&f.home, &f.home),
        Err(PublishError::UnsafePath)
    ));
    for subtree in [".claude", ".agents", ".oah"] {
        let top = f.home.join(subtree);
        let deep = top.join("nested/project");
        fs::create_dir_all(&deep).unwrap();
        for path in [&top, &deep] {
            assert!(matches!(
                ProjectRoot::open(path, &f.home),
                Err(PublishError::UnsafePath)
            ));
        }
        assert_eq!(fs::read_dir(deep).unwrap().count(), 0);
    }

    let nested_home = f.path.join(".oah/home");
    fs::create_dir_all(&nested_home).unwrap();
    assert!(matches!(
        ProjectRoot::open(&f.path, &nested_home),
        Err(PublishError::UnsafePath)
    ));

    let ordinary = f.home.join("projects/notes");
    fs::create_dir_all(&ordinary).unwrap();
    let project = ProjectRoot::open(&ordinary, &f.home).unwrap();
    assert_eq!(
        project
            .apply(Engine::Claude, &f.leaf, &binding(1), &files(1), None)
            .unwrap(),
        PublishStatus::Installed
    );
    assert_eq!(f.apply(1, None).unwrap(), PublishStatus::Installed);
}

#[test]
fn session_home_has_to_remain_a_physical_selection() {
    let f = Fixture::new();
    let alias = f.path.join("home-link");
    directory_link(&f.home, &alias);
    for unusable in [
        Path::new("relative-home"),
        &alias,
        &f.path.join("missing-home"),
    ] {
        assert!(ProjectRoot::open(&f.path, unusable).is_err());
    }
    fs::rename(&f.home, f.home.with_file_name("former-home")).unwrap();
    fs::create_dir(&f.home).unwrap();
    assert!(matches!(f.apply(1, None), Err(PublishError::UnsafePath)));
    assert!(!f.path.join(".oah").exists());
    assert!(!f.path.join(".agents").exists());
}

#[test]
fn a_replaced_selected_root_cannot_redirect_writes() {
    let f = Fixture::new();
    let selected = f.path.join("chosen");
    fs::create_dir(&selected).unwrap();
    let root = ProjectRoot::open(&selected, &f.home).unwrap();
    assert!(root.installed(Engine::Codex, &f.leaf).unwrap().is_none());
    let moved = f.path.join("moved");
    fs::rename(&selected, &moved).unwrap();
    fs::create_dir(&selected).unwrap();
    assert!(matches!(
        root.installed(Engine::Codex, &f.leaf),
        Err(PublishError::UnsafePath)
    ));
    assert!(matches!(
        root.apply(Engine::Codex, &f.leaf, &binding(1), &files(1), None),
        Err(PublishError::UnsafePath)
    ));
    assert_eq!(fs::read_dir(&selected).unwrap().count(), 0);
    assert_eq!(fs::read_dir(&moved).unwrap().count(), 0);
}

#[cfg(windows)]
pub(super) fn hold_without_delete_sharing(path: &Path) -> fs::File {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_BACKUP_SEMANTICS, FILE_LIST_DIRECTORY, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    fs::OpenOptions::new()
        .access_mode(FILE_LIST_DIRECTORY)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS)
        .open(path)
        .unwrap()
}

#[cfg(windows)]
pub(super) fn assert_sharing_violation<T: fmt::Debug>(result: Result<T, PublishError>) {
    use windows_sys::Win32::Foundation::ERROR_SHARING_VIOLATION;
    let Err(PublishError::Io(err)) = result else {
        panic!("expected a native sharing violation, got {result:?}");
    };
    assert_eq!(
        err.raw_os_error(),
        Some(i32::try_from(ERROR_SHARING_VIOLATION).unwrap()),
        "{err}"
    );
}

#[cfg(windows)]
#[test]
fn a_pinned_ancestor_cannot_be_moved_until_released() {
    let f = Fixture::new();
    let project = f.path.join("ancestor/project");
    fs::create_dir_all(&project).unwrap();
    let root = &f.root.project.pin().unwrap();
    let ancestor = root.child("ancestor").unwrap();
    let id = ancestor.id().clone();

    let rename = || root.move_new("ancestor", root, "renamed", &id);
    assert_sharing_violation(rename());
    assert!(project.is_dir());

    drop(ancestor);
    rename().unwrap();
    assert!(f.path.join("renamed/project").is_dir());
}

#[cfg(windows)]
#[test]
fn an_open_without_delete_sharing_preserves_live_then_retry_updates() {
    let f = Fixture::new();
    f.apply(1, None).unwrap();
    let hold = hold_without_delete_sharing(&f.live());
    assert_sharing_violation(f.apply(2, Some(&binding(1).review_id)));
    assert_eq!(
        fs::read(f.live().join("references/nested/note.txt")).unwrap(),
        [1, 0, 255]
    );
    drop(hold);
    assert_eq!(
        f.apply(2, Some(&binding(1).review_id)).unwrap(),
        PublishStatus::Updated
    );
}
