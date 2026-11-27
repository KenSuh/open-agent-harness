// 프로세스 없는 프로젝트 찾기: Git 을 실행하지 않고 .git 항목과 그 짧은 메타데이터만 읽어 일반·연결 작업 트리·일반 폴더를 가리고, 확인되지 않는 Git 자료는 읽지 않는다.
//! Finding a project and its Git folder without starting Git.
//!
//! [`discover`] walks up from a folder to the first one with a `.git` entry, as Git does, and [`git_at`] decides
//! what that entry is. A Git folder is read further only when it is qualified:
//!
//! - a real `.git` folder with `HEAD`, `objects` and `refs` and no `commondir` (an ordinary work tree), or
//! - a `.git` file naming `<main>/.git/worktrees/<leaf>` on the project's own disk, reached through real folders
//!   only, whose `gitdir` names this same `.git` file and whose `commondir` names that `.git` (a standard local
//!   linked work tree, checked from both ends). The shape is decided from the text before the path is touched.
//!
//! Everything else (a link, a submodule, a separate Git folder, a broken pointer) is [`Git::Unqualified`]: the
//! card says the state is unknown and nothing behind the pointer is read. Environment overrides such as
//! `GIT_DIR` and Git's own configuration are not consulted; `GIT_CEILING_DIRECTORIES` stops the walk.
//! Metadata is read through the link-refusing `fsx::open_regular`, a few kilobytes at most.

use crate::error::Error;
use crate::fsx::links::{self, Entry};
use crate::fsx::open_regular;
use crate::paths::{self, Env};
use crate::project::ProjectError;
use std::io::Read;
use std::path::{Component, Path, PathBuf, Prefix};

/// The most of a `.git` file, `gitdir`, `commondir` or `HEAD` that is read.
pub(super) const POINTER_LIMIT: u64 = 4096;

const UNVERIFIED_POINTER: &str = "The .git file here points to Git data OAH could not confirm belongs to this folder, so it was not read.";

/// What is known, without running Git, about the Git folder of a project root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Git {
    /// The root has no `.git` entry.
    None,
    /// A qualified work tree. `git_dir` holds `HEAD` and `index`; `common_dir` holds the branches.
    WorkTree {
        git_dir: PathBuf,
        common_dir: PathBuf,
    },
    /// There is a `.git` entry, and it is not read; the text says why.
    Unqualified(&'static str),
}

/// The first `limit` bytes of the regular file at `path` and whether it is longer. `None` when there is no regular
/// file there or it cannot be read; a link, a pipe or a device is never opened.
pub(super) fn prefix(path: &Path, limit: u64) -> Option<(Vec<u8>, bool)> {
    let (file, _) = open_regular(path).ok()?;
    let mut bytes = Vec::new();
    file.take(limit + 1).read_to_end(&mut bytes).ok()?;
    let longer = bytes.len() as u64 > limit;
    bytes.truncate(limit as usize);
    Some((bytes, longer))
}

/// The whole text of a short metadata file, trimmed; `None` when it is missing, longer than the limit or not text.
fn short_text(path: &Path) -> Option<String> {
    let (bytes, longer) = prefix(path, POINTER_LIMIT)?;
    if longer {
        return None;
    }
    Some(String::from_utf8(bytes).ok()?.trim().to_string())
}

fn is(path: &Path, entry: Entry) -> bool {
    links::inspect(path).is_ok_and(|found| found == entry)
}

fn resolve(base: &Path, text: &str) -> Option<PathBuf> {
    if text.is_empty() {
        return None;
    }
    let path = Path::new(text);
    Some(if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    })
}

/// A folder that holds a repository's own data: not a link, with `HEAD`, `objects` and `refs`.
fn is_repository(dir: &Path) -> bool {
    is(dir, Entry::Directory)
        && is(&dir.join("HEAD"), Entry::File)
        && is(&dir.join("objects"), Entry::Directory)
        && is(&dir.join("refs"), Entry::Directory)
}

/// The Git folder of the canonical folder `root`, or `None` when `root` has no `.git` entry at all.
pub(super) fn git_at(root: &Path) -> Option<Git> {
    let dot_git = root.join(".git");
    Some(match links::inspect(&dot_git) {
        Ok(Entry::Missing) => return None,
        Ok(Entry::Directory) => {
            if !is(&dot_git.join("commondir"), Entry::Missing) {
                Git::Unqualified(
                    "The .git folder here shares another repository's data, which OAH does not read.",
                )
            } else if is_repository(&dot_git) {
                Git::WorkTree {
                    git_dir: dot_git.clone(),
                    common_dir: dot_git,
                }
            } else {
                Git::Unqualified("The .git folder here is not a complete Git repository.")
            }
        }
        Ok(Entry::File) => linked(root, &dot_git).unwrap_or(Git::Unqualified(UNVERIFIED_POINTER)),
        Ok(_) => Git::Unqualified("The .git entry here is a link, and OAH does not follow it."),
        Err(_) => Git::Unqualified("The .git entry here could not be read."),
    })
}

/// `path` with `.` dropped and `..` applied by name only; `None` when it is not absolute or `..` leaves its root.
fn lexical(path: &Path) -> Option<PathBuf> {
    let mut out = PathBuf::new();
    let mut depth = 0usize;
    for part in path.components() {
        match part {
            Component::Prefix(_) | Component::RootDir => out.push(part.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                depth = depth.checked_sub(1)?;
                out.pop();
            }
            Component::Normal(name) => {
                depth += 1;
                out.push(name);
            }
        }
    }
    out.has_root().then_some(out)
}

/// Whether two absolute paths name the same entry by their text alone (letter case aside on Windows).
fn same_path(left: &Path, right: &Path) -> bool {
    if cfg!(windows) {
        let text = |path: &Path| path.to_string_lossy().replace('/', "\\").to_lowercase();
        text(left) == text(right)
    } else {
        left == right
    }
}

/// Whether `path` is on the same local disk as the project `root`. On Windows that means a plain drive path with
/// the root's own drive letter, so a network share, a device path or another (possibly mapped) drive is never
/// touched on the word of a `.git` file. Elsewhere every absolute path qualifies.
fn same_disk(root: &Path, path: &Path) -> bool {
    if !cfg!(windows) {
        return true;
    }
    let drive = |path: &Path| match path.components().next() {
        Some(Component::Prefix(prefix)) => match prefix.kind() {
            Prefix::Disk(letter) => Some(letter.to_ascii_uppercase()),
            _ => None,
        },
        _ => None,
    };
    drive(path).is_some() && drive(path) == drive(root)
}

/// The folder a linked work tree's `.git` file names, decided from the text alone, before anything at that path
/// is touched: an absolute path (or one relative to `root`) of the shape `<main>/.git/worktrees/<leaf>` on the
/// project's own disk. Anything else is not a standard local linked work tree.
fn admin_shape(root: &Path, pointer: &str) -> Option<PathBuf> {
    let named = lexical(&resolve(root, pointer.strip_prefix("gitdir:")?.trim())?)?;
    if !same_disk(root, &named) {
        return None;
    }
    let mut names = named.components().rev();
    let shaped = matches!(names.next()?, Component::Normal(_))
        && names.next()?.as_os_str() == "worktrees"
        && names.next()?.as_os_str() == ".git"
        && matches!(names.next()?, Component::Normal(_));
    shaped.then_some(named)
}

/// Whether every folder from the filesystem root down to `path` is a real folder: no link or reparse entry on
/// the way, so the path's text is where it really is.
fn real_folders(path: &Path) -> bool {
    let mut walked = PathBuf::new();
    path.components().all(|part| {
        walked.push(part.as_os_str());
        !matches!(part, Component::Normal(_)) || is(&walked, Entry::Directory)
    })
}

/// A linked work tree, accepted only when both ends agree. The `.git` file must name a standard local folder
/// (`admin_shape`) reached through real folders only; that folder's `gitdir` must name this `.git` file and its
/// `commondir` the `.git` two levels up, both by their text, so no path taken from these files is opened unless
/// it is that same repository; and the repository must be complete.
fn linked(root: &Path, dot_git: &Path) -> Option<Git> {
    let git_dir = admin_shape(root, &short_text(dot_git)?)?;
    if !real_folders(&git_dir) {
        return None;
    }
    let back = lexical(&resolve(&git_dir, &short_text(&git_dir.join("gitdir"))?)?)?;
    if !same_path(&back, dot_git) {
        return None;
    }
    let common_dir = git_dir.parent()?.parent()?.to_path_buf();
    let common = lexical(&resolve(
        &git_dir,
        &short_text(&git_dir.join("commondir"))?,
    )?)?;
    if !same_path(&common, &common_dir) || !is_repository(&common_dir) {
        return None;
    }
    Some(Git::WorkTree {
        git_dir,
        common_dir,
    })
}

/// The Git folder of a root that Git itself already located, with the common folder Git reported. The files must
/// agree with that answer; when they do not, nothing is read.
pub(super) fn confirmed(root: &Path, reported_common_dir: &Path) -> Git {
    let Ok(root) = paths::canonical_folder(root) else {
        return Git::Unqualified("The project folder could not be resolved.");
    };
    match git_at(&root) {
        Some(Git::WorkTree {
            git_dir,
            common_dir,
        }) => {
            let same = paths::canonical_folder(&common_dir).ok()
                == paths::canonical_folder(reported_common_dir).ok();
            if same {
                Git::WorkTree {
                    git_dir,
                    common_dir,
                }
            } else {
                Git::Unqualified(
                    "The Git folder found here is not the one Git reported for this project.",
                )
            }
        }
        Some(other) => other,
        None => {
            Git::Unqualified("Git reported a repository here, but the folder has no .git entry.")
        }
    }
}

/// The project `folder` belongs to, found without starting a program: the nearest folder at or above it with a
/// `.git` entry, or the folder itself. The root is canonical. `OAH_PROJECT_MISSING` when `folder` cannot be
/// resolved and `OAH_PROJECT_NOT_FOLDER` when it is not a folder.
pub(super) fn discover(env: &Env, folder: &Path) -> Result<(PathBuf, Git), Error> {
    let absolute = if folder.is_absolute() {
        folder.to_path_buf()
    } else {
        env.cwd().join(folder)
    };
    let start = paths::canonical_folder(&absolute)?;
    if !start.is_dir() {
        return Err(ProjectError::NotFolder { path: absolute }.into());
    }
    let ceilings: Vec<PathBuf> = env
        .var("GIT_CEILING_DIRECTORIES")
        .filter(|value| !value.is_empty())
        .map(|value| {
            std::env::split_paths(value)
                .filter_map(|path| paths::canonical_folder(&path).ok())
                .collect()
        })
        .unwrap_or_default();
    let mut dir = start.as_path();
    loop {
        if let Some(git) = git_at(dir) {
            return Ok((dir.to_path_buf(), git));
        }
        match dir.parent() {
            Some(parent) if !ceilings.iter().any(|ceiling| ceiling == parent) => dir = parent,
            _ => return Ok((start, Git::None)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn canonical(dir: &tempfile::TempDir) -> PathBuf {
        paths::canonical_folder(dir.path()).unwrap()
    }

    /// A hand-made repository folder: these tests start no Git.
    fn repository(git: &Path) {
        fs::create_dir_all(git.join("objects")).unwrap();
        fs::create_dir_all(git.join("refs/heads")).unwrap();
        fs::write(git.join("HEAD"), "ref: refs/heads/main\n").unwrap();
    }

    /// `main` with a repository and `tree`, a linked work tree of it, wired from both ends.
    fn linked_pair(base: &Path) -> (PathBuf, PathBuf, PathBuf) {
        let (main, tree) = (base.join("main"), base.join("tree"));
        let admin = main.join(".git/worktrees/tree");
        repository(&main.join(".git"));
        fs::create_dir_all(&admin).unwrap();
        fs::create_dir_all(&tree).unwrap();
        fs::write(admin.join("HEAD"), "ref: refs/heads/tree\n").unwrap();
        fs::write(admin.join("commondir"), "../..\n").unwrap();
        fs::write(
            admin.join("gitdir"),
            format!("{}\n", tree.join(".git").display()),
        )
        .unwrap();
        fs::write(tree.join(".git"), format!("gitdir: {}\n", admin.display())).unwrap();
        (main, tree, admin)
    }

    #[test]
    fn an_ordinary_work_tree_needs_a_complete_git_folder() {
        let dir = tempfile::tempdir().unwrap();
        let root = canonical(&dir);
        assert_eq!(git_at(&root), None);
        fs::create_dir(root.join(".git")).unwrap();
        assert!(matches!(git_at(&root), Some(Git::Unqualified(_))));
        repository(&root.join(".git"));
        let git = root.join(".git");
        assert_eq!(
            git_at(&root),
            Some(Git::WorkTree {
                git_dir: git.clone(),
                common_dir: git.clone()
            })
        );
        fs::write(git.join("commondir"), "../elsewhere\n").unwrap();
        assert!(matches!(git_at(&root), Some(Git::Unqualified(_))));
    }

    #[test]
    fn a_linked_work_tree_is_read_only_when_both_ends_agree() {
        let dir = tempfile::tempdir().unwrap();
        let base = canonical(&dir);
        let (main, tree, admin) = linked_pair(&base);
        assert_eq!(
            git_at(&tree),
            Some(Git::WorkTree {
                git_dir: admin.clone(),
                common_dir: main.join(".git")
            })
        );
        // The repository says this folder belongs to another work tree.
        fs::write(
            admin.join("gitdir"),
            format!("{}\n", base.join("other/.git").display()),
        )
        .unwrap();
        assert_eq!(git_at(&tree), Some(Git::Unqualified(UNVERIFIED_POINTER)));
        // A pointer at a repository that is not a work tree folder of it (a submodule or a separate Git folder).
        fs::write(
            tree.join(".git"),
            format!("gitdir: {}\n", main.join(".git").display()),
        )
        .unwrap();
        assert_eq!(git_at(&tree), Some(Git::Unqualified(UNVERIFIED_POINTER)));
        for text in ["", "gitdir:\n", "gitdir: ../missing\n", "something else\n"] {
            fs::write(tree.join(".git"), text).unwrap();
            assert_eq!(
                git_at(&tree),
                Some(Git::Unqualified(UNVERIFIED_POINTER)),
                "{text:?}"
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_linked_git_entry_or_pointer_is_not_followed() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let base = canonical(&dir);
        let (main, tree, admin) = linked_pair(&base);
        let plain = base.join("plain");
        fs::create_dir(&plain).unwrap();
        symlink(main.join(".git"), plain.join(".git")).unwrap();
        assert!(matches!(git_at(&plain), Some(Git::Unqualified(_))));
        // The .git file names a link to the real folder: the name it gives is not a folder of its own.
        let alias = base.join("alias");
        symlink(&admin, &alias).unwrap();
        fs::write(tree.join(".git"), format!("gitdir: {}\n", alias.display())).unwrap();
        assert_eq!(git_at(&tree), Some(Git::Unqualified(UNVERIFIED_POINTER)));
    }

    /// The `.git` file's text alone decides whether its target is looked at.
    #[test]
    fn a_pointer_is_qualified_by_its_text_before_its_target_is_touched() {
        let root = if cfg!(windows) {
            Path::new(r"C:\work\tree")
        } else {
            Path::new("/work/tree")
        };
        let at = |text: &str| admin_shape(root, text);
        let main = if cfg!(windows) {
            r"C:\work\main"
        } else {
            "/work/main"
        };
        let admin = Path::new(main).join(".git").join("worktrees").join("tree");
        assert_eq!(
            at(&format!("gitdir: {}", admin.display())),
            Some(admin.clone())
        );
        assert_eq!(
            at("gitdir: ../main/.git/worktrees/tree"),
            Some(admin.clone())
        );
        assert_eq!(
            at(&format!("gitdir: {}/x/../.git/./worktrees/tree", main)),
            Some(admin)
        );
        for text in [
            "",
            "gitdir:",
            "something else",
            "gitdir: ../main/.git",
            "gitdir: ../main/.git/modules/tree",
            "gitdir: ../main/repo.git/worktrees/tree",
            "gitdir: ../main/.git/worktrees/tree/deeper",
            "gitdir: ../../../../.git/worktrees/tree",
            "gitdir: /.git/worktrees/tree",
        ] {
            assert_eq!(at(text), None, "{text:?}");
        }
        #[cfg(windows)]
        for text in [
            r"gitdir: \\server\share\main\.git\worktrees\tree",
            r"gitdir: \\?\C:\work\main\.git\worktrees\tree",
            r"gitdir: \\.\pipe\main\.git\worktrees\tree",
            r"gitdir: Z:\work\main\.git\worktrees\tree",
            "gitdir: //server/share/main/.git/worktrees/tree",
        ] {
            assert_eq!(at(text), None, "{text:?}");
        }
    }

    #[test]
    fn discovery_walks_up_to_the_git_entry_and_stops_at_a_ceiling() {
        let dir = tempfile::tempdir().unwrap();
        let base = canonical(&dir);
        repository(&base.join("repo/.git"));
        let inner = base.join("repo/a/b");
        fs::create_dir_all(&inner).unwrap();
        fs::create_dir_all(base.join("plain")).unwrap();
        let mut env = Env::new(&base).unwrap();
        env.set("GIT_CEILING_DIRECTORIES", &base);

        let (root, git) = discover(&env, &inner).unwrap();
        assert_eq!(root, base.join("repo"));
        assert!(matches!(git, Git::WorkTree { .. }));
        assert_eq!(
            discover(&env, Path::new("plain")).unwrap(),
            (base.join("plain"), Git::None)
        );
        env.set("GIT_CEILING_DIRECTORIES", base.join("repo"));
        assert_eq!(discover(&env, &inner).unwrap(), (inner.clone(), Git::None));
        assert!(discover(&env, &base.join("missing")).is_err());
        fs::write(base.join("file"), "x").unwrap();
        assert!(discover(&env, &base.join("file")).is_err());
    }

    #[test]
    fn git_reported_folders_must_match_the_files() {
        let dir = tempfile::tempdir().unwrap();
        let base = canonical(&dir);
        let (main, tree, _admin) = linked_pair(&base);
        assert!(matches!(
            confirmed(&tree, &main.join(".git")),
            Git::WorkTree { .. }
        ));
        assert!(matches!(confirmed(&tree, &base), Git::Unqualified(_)));
        assert!(matches!(
            confirmed(&base, &main.join(".git")),
            Git::Unqualified(_)
        ));
    }

    #[test]
    fn only_a_bounded_prefix_of_a_regular_file_is_read() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("f");
        fs::write(&file, b"abcdef").unwrap();
        assert_eq!(prefix(&file, 4), Some((b"abcd".to_vec(), true)));
        assert_eq!(prefix(&file, 6), Some((b"abcdef".to_vec(), false)));
        assert_eq!(prefix(dir.path(), 4), None, "a folder is not read");
        assert_eq!(prefix(&dir.path().join("missing"), 4), None);
    }
}
