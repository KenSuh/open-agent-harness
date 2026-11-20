// 경로 시험: 임시 홈과 임시 저장소로 OAH_HOME·Git 공통 폴더·작업 트리 공유·Git 밖 폴더 격리·오류 유형을 확인하며 아무 폴더도 만들지 않는지 본다.
use oah_core::paths::{
    Env, PathsErrorKind, ProjectKind, canonical_folder, folder_id, locate_project, oah_home,
    settings_file, user_state_dir,
};
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };

/// A temporary folder with its own home; nothing here reads or writes the real home.
struct Sandbox {
    _dir: tempfile::TempDir,
    root: PathBuf,
    home: PathBuf,
}

impl Sandbox {
    fn new() -> Sandbox {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let root = canonical_folder(&root).unwrap();
        let home = root.join("home");
        fs::create_dir(&home).unwrap();
        Sandbox {
            _dir: dir,
            root,
            home,
        }
    }

    /// An environment built from nothing: the process's `PATH` (to find Git), the sandbox home, no system Git
    /// configuration, and a ceiling so Git never looks above the sandbox.
    fn env(&self) -> Env {
        let mut env = Env::new(&self.root).unwrap();
        if let Some(path) = std::env::var_os("PATH") {
            env.set("PATH", path);
        }
        if cfg!(windows) {
            for name in ["SystemRoot", "windir", "ComSpec", "PATHEXT", "TEMP", "TMP"] {
                if let Some(value) = std::env::var_os(name) {
                    env.set(name, value);
                }
            }
            env.set("HOME", &self.home);
        }
        env.set(HOME_VAR, &self.home);
        env.set("GIT_CONFIG_NOSYSTEM", "1");
        env.set("GIT_CEILING_DIRECTORIES", &self.root);
        env
    }

    fn folder(&self, name: &str) -> PathBuf {
        let path = self.root.join(name);
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn git(&self, folder: &Path, args: &[&str]) {
        let env = self.env();
        let output = Command::new("git")
            .args(["-c", "maintenance.auto=false", "-c", "gc.auto=0"])
            .arg("-C")
            .arg(folder)
            .args(args)
            .env_clear()
            .envs(env.vars())
            .env("GIT_AUTHOR_NAME", "Test")
            .env("GIT_AUTHOR_EMAIL", "test@example.com")
            .env("GIT_COMMITTER_NAME", "Test")
            .env("GIT_COMMITTER_EMAIL", "test@example.com")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    fn repo(&self, name: &str) -> PathBuf {
        let repo = self.folder(name);
        self.git(&repo, &["init", "-q"]);
        self.git(&repo, &["commit", "-q", "--allow-empty", "-m", "start"]);
        repo
    }

    /// Every path below the sandbox root.
    fn tree(&self) -> Vec<PathBuf> {
        let mut out = Vec::new();
        let mut pending = vec![self.root.clone()];
        while let Some(folder) = pending.pop() {
            for entry in fs::read_dir(&folder).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    pending.push(path.clone());
                }
                out.push(path);
            }
        }
        out.sort();
        out
    }
}

fn same_folder(a: &Path, b: &Path) -> bool {
    fs::canonicalize(a).unwrap() == fs::canonicalize(b).unwrap()
}

#[test]
fn oah_home_is_a_non_empty_override_or_the_home_dot_oah() {
    let sandbox = Sandbox::new();
    let env = sandbox.env();
    assert_eq!(oah_home(&env).unwrap(), sandbox.home.join(".oah"));
    assert_eq!(
        settings_file(&env).unwrap(),
        sandbox.home.join(".oah").join("settings.toml")
    );
    assert_eq!(
        user_state_dir(&env).unwrap(),
        sandbox.home.join(".oah").join("state")
    );
    assert_eq!(
        oah_home(&env.clone().with("OAH_HOME", "")).unwrap(),
        sandbox.home.join(".oah")
    );

    let elsewhere = sandbox.root.join("elsewhere");
    assert_eq!(
        oah_home(&env.clone().with("OAH_HOME", &elsewhere)).unwrap(),
        elsewhere
    );
    let relative = env.clone().with("OAH_HOME", "relative-home");
    assert_eq!(
        oah_home(&relative).unwrap(),
        sandbox.root.join("relative-home")
    );
    assert!(
        !sandbox.home.join(".oah").exists() && !elsewhere.exists(),
        "resolving created a folder"
    );
}

#[test]
fn a_missing_empty_or_relative_home_is_a_typed_error() {
    let sandbox = Sandbox::new();
    for env in [
        sandbox.env().without(HOME_VAR),
        sandbox.env().with(HOME_VAR, ""),
        sandbox.env().with(HOME_VAR, "not-absolute"),
    ] {
        let error = oah_home(&env).unwrap_err();
        assert_eq!(error.kind(), PathsErrorKind::HomeUnavailable);
        assert_eq!(error.code(), "OAH_HOME_UNAVAILABLE");
    }
    let env = sandbox
        .env()
        .without(HOME_VAR)
        .with("OAH_HOME", &sandbox.home);
    assert_eq!(
        oah_home(&env).unwrap(),
        sandbox.home,
        "OAH_HOME does not need a home folder"
    );
}

#[test]
fn a_git_work_tree_keeps_state_under_its_common_dir() {
    let sandbox = Sandbox::new();
    let repo = sandbox.repo("repo");
    let sub = sandbox.folder("repo/deep/sub");
    let before = sandbox.tree();
    let project = locate_project(&sandbox.env(), &sub).unwrap();
    assert!(same_folder(&project.root, &repo));
    let ProjectKind::Git { common_dir } = &project.kind else {
        panic!("not a Git project: {project:?}");
    };
    assert!(same_folder(common_dir, &repo.join(".git")));
    assert!(common_dir.is_absolute());
    assert_eq!(project.state_dir, common_dir.join("oah").join("v1"));
    assert_eq!(
        project.config_file(),
        project.root.join(".oah").join("config.toml")
    );
    assert_eq!(sandbox.tree(), before, "locating created something");
}

#[test]
fn linked_worktrees_share_one_state_folder() {
    let sandbox = Sandbox::new();
    let repo = sandbox.repo("main");
    sandbox.git(&repo, &["worktree", "add", "-q", "../linked"]);
    let linked = sandbox.root.join("linked");
    let first = locate_project(&sandbox.env(), &repo).unwrap();
    let second = locate_project(&sandbox.env(), &linked).unwrap();
    assert!(same_folder(&second.root, &linked));
    assert!(!same_folder(&first.root, &second.root));
    assert_eq!(
        first.kind, second.kind,
        "both worktrees report one common Git folder"
    );
    assert_eq!(first.state_dir, second.state_dir);
}

#[test]
fn oah_state_dir_replaces_the_state_base_of_git_projects() {
    let sandbox = Sandbox::new();
    let repo = sandbox.repo("repo");
    let base = sandbox.root.join("state-base");
    let project = locate_project(&sandbox.env().with("OAH_STATE_DIR", &base), &repo).unwrap();
    assert_eq!(project.state_dir, base.join("v1"));
    let project = locate_project(&sandbox.env().with("OAH_STATE_DIR", "rel"), &repo).unwrap();
    assert_eq!(project.state_dir, sandbox.root.join("rel").join("v1"));
    let project = locate_project(&sandbox.env().with("OAH_STATE_DIR", ""), &repo).unwrap();
    let ProjectKind::Git { common_dir } = &project.kind else {
        panic!("not a Git project");
    };
    assert_eq!(project.state_dir, common_dir.join("oah").join("v1"));
    assert!(!base.exists());
}

#[test]
fn folders_outside_git_are_isolated_by_their_canonical_path() {
    let sandbox = Sandbox::new();
    let one = sandbox.folder("one");
    let two = sandbox.folder("two");
    let before = sandbox.tree();
    let first = locate_project(&sandbox.env(), &one).unwrap();
    let second = locate_project(&sandbox.env(), &two).unwrap();
    let (ProjectKind::Folder { id: first_id }, ProjectKind::Folder { id: second_id }) =
        (&first.kind, &second.kind)
    else {
        panic!("not folder projects: {first:?} {second:?}");
    };
    assert_ne!(first_id, second_id);
    assert_eq!(first.root, canonical_folder(&one).unwrap());
    assert_eq!(first_id, &folder_id(&first.root));
    assert_eq!(first_id.len(), 16);
    assert!(
        first_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    );
    assert_eq!(
        first.state_dir,
        sandbox
            .home
            .join(".oah")
            .join("state")
            .join("projects")
            .join(first_id)
    );
    let relative = locate_project(&sandbox.env(), Path::new("one")).unwrap();
    assert_eq!(
        relative, first,
        "a relative folder is resolved against the invocation folder"
    );
    assert_eq!(sandbox.tree(), before, "locating created something");
}

#[cfg(unix)]
#[test]
fn a_folder_reached_through_a_link_is_the_same_project() {
    let sandbox = Sandbox::new();
    let real = sandbox.folder("real");
    let link = sandbox.root.join("via-link");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    let direct = locate_project(&sandbox.env(), &real).unwrap();
    let linked = locate_project(&sandbox.env(), &link).unwrap();
    assert_eq!(direct, linked);
}

#[test]
fn utf8_folder_names_work_inside_and_outside_git() {
    let sandbox = Sandbox::new();
    let plain = sandbox.folder("프로젝트 폴더");
    let project = locate_project(&sandbox.env(), &plain).unwrap();
    assert!(project.root.ends_with("프로젝트 폴더"));
    let ProjectKind::Folder { id } = &project.kind else {
        panic!("not a folder project");
    };
    assert_eq!(id, &folder_id(&canonical_folder(&plain).unwrap()));

    let repo = sandbox.repo("저장소");
    let project = locate_project(&sandbox.env(), &repo).unwrap();
    assert!(same_folder(&project.root, &repo));
    assert!(project.root.ends_with("저장소"));
}

#[test]
fn a_missing_folder_or_a_file_is_not_a_project() {
    let sandbox = Sandbox::new();
    let error = locate_project(&sandbox.env(), &sandbox.root.join("absent")).unwrap_err();
    assert_eq!(error.kind(), PathsErrorKind::ProjectMissing);
    let file = sandbox.root.join("file.txt");
    fs::write(&file, "x").unwrap();
    let error = locate_project(&sandbox.env(), &file).unwrap_err();
    assert_eq!(error.code(), "OAH_PROJECT_MISSING");
}

#[test]
fn a_missing_git_is_an_error_not_a_folder_fallback() {
    let sandbox = Sandbox::new();
    let empty = sandbox.folder("no-tools");
    let folder = sandbox.folder("work");
    let env = sandbox
        .env()
        .with("PATH", OsString::from(empty.as_os_str()));
    let error = locate_project(&env, &folder).unwrap_err();
    assert_eq!(error.kind(), PathsErrorKind::GitMissing);
    assert_eq!(error.code(), "OAH_GIT_MISSING");
    let error = locate_project(&sandbox.env().without("PATH"), &folder).unwrap_err();
    assert_eq!(error.kind(), PathsErrorKind::GitMissing);
}

#[test]
fn a_failed_git_probe_is_an_error_not_a_folder_fallback() {
    let sandbox = Sandbox::new();
    let repo = sandbox.repo("future");
    sandbox.git(&repo, &["config", "core.repositoryformatversion", "99"]);
    let error = locate_project(&sandbox.env(), &repo).unwrap_err();
    assert_eq!(error.kind(), PathsErrorKind::GitProbeFailed);
    assert_eq!(error.code(), "OAH_GIT_PROBE_FAILED");
}

#[cfg(unix)]
#[test]
fn an_unexpected_git_answer_is_refused() {
    use std::os::unix::fs::PermissionsExt;
    let sandbox = Sandbox::new();
    let tools = sandbox.folder("fake-tools");
    let fake = tools.join("git");
    fs::write(&fake, "#!/bin/sh\necho only-one-line\n").unwrap();
    fs::set_permissions(&fake, fs::Permissions::from_mode(0o755)).unwrap();
    let env = sandbox
        .env()
        .with("PATH", OsString::from(tools.as_os_str()));
    let error = locate_project(&env, &sandbox.folder("work")).unwrap_err();
    assert_eq!(error.kind(), PathsErrorKind::GitProbeFailed);
}
