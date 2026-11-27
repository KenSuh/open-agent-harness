// 영향 지도 카드: Git 을 실행하지 않고 Git 자체 파일의 앞부분만 읽어 커밋·추적 파일 유무와 이 체크아웃의 지도 파일 존재를 확인한다.
//! The Impact Map card. It never starts Git: whether the repository has a commit and tracks files is read from
//! short prefixes of Git's own `HEAD`, `packed-refs` and `index`, and anything those do not settle is `unknown`.
//! A map is only ever reported as a file that exists where the source CLI stores this checkout's map
//! (`src/oah/impact/mapstore.py` `map_path`); its contents are not read, so it is never called current.

use super::Card;
use super::locate::{Git, POINTER_LIMIT, prefix};
use crate::dto::{ProjectToolDto, ProjectToolIdDto, ToolPathBaseDto, ToolPathDto};
use crate::fsx::links::{self, Entry};
use crate::paths;
use crate::rules::input::{ExpectedEntry, InspectedEntry, inspect_project_entry};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::Path;

/// The most of `packed-refs` that is searched for the checked-out branch.
const PACKED_REFS_LIMIT: u64 = 4 * 1024 * 1024;
/// `DIRC`, the version and the entry count.
const INDEX_HEADER: u64 = 12;
/// How many names of the Git folder are looked at for a shared index.
const GIT_DIR_SCAN_LIMIT: usize = 512;

/// Whether the checked-out branch has a commit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Head {
    Commit,
    /// `HEAD` names a branch that does not exist yet: no commit has been made.
    Unborn,
    Unknown(&'static str),
}

fn is_object_id(text: &str) -> bool {
    matches!(text.len(), 40 | 64) && text.bytes().all(|byte| byte.is_ascii_hexdigit())
}

/// A ref name that is safe to look up as a path below the Git folder.
fn is_ref_name(name: &str) -> bool {
    name.starts_with("refs/")
        && name.split('/').all(|part| {
            !part.is_empty()
                && part != "."
                && part != ".."
                && !part
                    .chars()
                    .any(|c| c.is_control() || matches!(c, '\\' | ':' | ' '))
        })
}

fn head(git_dir: &Path, common_dir: &Path) -> Head {
    const UNREADABLE: &str = "Git's HEAD file could not be read.";
    let Some((bytes, false)) = prefix(&git_dir.join("HEAD"), POINTER_LIMIT) else {
        return Head::Unknown(UNREADABLE);
    };
    let Ok(text) = String::from_utf8(bytes) else {
        return Head::Unknown(UNREADABLE);
    };
    let text = text.trim();
    if is_object_id(text) {
        return Head::Commit;
    }
    let Some(name) = text.strip_prefix("ref:").map(str::trim) else {
        return Head::Unknown(UNREADABLE);
    };
    if !is_ref_name(name) {
        return Head::Unknown("Git's HEAD names something OAH does not read directly.");
    }
    if matches!(
        links::inspect(&common_dir.join("reftable")),
        Ok(Entry::Directory)
    ) {
        return Head::Unknown(
            "This repository stores its branches in a format OAH does not read directly.",
        );
    }
    // The branch as a file of its own: every folder on the way must be a real folder, never a link.
    let mut parts = name.split('/').peekable();
    let mut loose = common_dir.to_path_buf();
    let mut found = true;
    while let Some(part) = parts.next() {
        loose.push(part);
        let wanted = if parts.peek().is_some() {
            Entry::Directory
        } else {
            Entry::File
        };
        if !links::inspect(&loose).is_ok_and(|entry| entry == wanted) {
            found = false;
            break;
        }
    }
    if found {
        return Head::Commit;
    }
    match prefix(&common_dir.join("packed-refs"), PACKED_REFS_LIMIT) {
        None => Head::Unborn,
        Some((bytes, longer)) => {
            let packed = bytes.split(|byte| *byte == b'\n').any(|line| {
                let line = line.strip_suffix(b"\r").unwrap_or(line);
                line.split(|byte| *byte == b' ').nth(1) == Some(name.as_bytes())
            });
            if packed {
                Head::Commit
            } else if longer {
                Head::Unknown("This repository's branch list is too long for OAH to read directly.")
            } else {
                Head::Unborn
            }
        }
    }
}

/// Whether Git's index lists any file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Tracked {
    Some,
    None,
    Unknown,
}

fn tracked(git_dir: &Path) -> Tracked {
    let index = git_dir.join("index");
    let count = match links::inspect(&index) {
        // Git reads a missing index as an empty one.
        Ok(Entry::Missing) => 0,
        Ok(Entry::File) => match prefix(&index, INDEX_HEADER) {
            Some((bytes, _)) if bytes.len() == 12 && &bytes[..4] == b"DIRC" => {
                u32::from_be_bytes([bytes[8], bytes[9], bytes[10], bytes[11]])
            }
            _ => return Tracked::Unknown,
        },
        _ => return Tracked::Unknown,
    };
    if count > 0 {
        return Tracked::Some;
    }
    // With a split index the entries live in a shared file, and this header can say zero.
    let Ok(entries) = fs::read_dir(git_dir) else {
        return Tracked::Unknown;
    };
    for (seen, entry) in entries.enumerate() {
        if seen >= GIT_DIR_SCAN_LIMIT {
            return Tracked::Unknown;
        }
        match entry {
            Ok(entry)
                if entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("sharedindex.") =>
            {
                return Tracked::Unknown;
            }
            Ok(_) => {}
            Err(_) => return Tracked::Unknown,
        }
    }
    Tracked::None
}

fn map_file(root: &Path, state: &Path, common_dir: &Path) -> Option<ToolPathDto> {
    let root = paths::canonical_folder(root).ok()?;
    let root = root.to_str()?;
    let key: String = Sha256::digest(root.as_bytes())[..8]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let file = state
        .join("impact")
        .join("maps")
        .join(format!("{key}.json"));
    if !matches!(
        inspect_project_entry(&file, ExpectedEntry::RegularFile),
        Ok(InspectedEntry::Allowed)
    ) {
        return None;
    }
    let (base, path) = if state == common_dir.join("oah") {
        (
            ToolPathBaseDto::GitCommonDir,
            format!("oah/impact/maps/{key}.json"),
        )
    } else {
        (ToolPathBaseDto::StateDir, format!("impact/maps/{key}.json"))
    };
    Some(ToolPathDto {
        label: String::from("Map file"),
        path,
        base,
    })
}

/// Whether `graphify-out/graph.json` is a regular file in the project. It is named, never read or trusted.
fn has_graphify_file(root: &Path) -> bool {
    let folder = root.join("graphify-out");
    matches!(
        inspect_project_entry(&folder, ExpectedEntry::Directory),
        Ok(InspectedEntry::Allowed)
    ) && matches!(
        inspect_project_entry(&folder.join("graph.json"), ExpectedEntry::RegularFile),
        Ok(InspectedEntry::Allowed)
    )
}

fn no_git(root: &Path) -> Card {
    let empty = fs::read_dir(root).is_ok_and(|mut entries| entries.next().is_none());
    if empty {
        Card::new(
            "no-git",
            "This folder is empty and is not a Git repository.",
            "An Impact Map needs a Git repository with at least one commit. Add your files, run git init \
             here and make the first commit. Then check again. OAH does not run Git for you.",
        )
    } else {
        Card::new(
            "no-git",
            "This folder is not a Git repository.",
            "An Impact Map needs a Git repository with at least one commit. Run git init here and make \
             the first commit. Then check again. OAH does not run Git for you.",
        )
    }
}

fn unknown(reason: &str) -> Card {
    Card::new(
        "unknown",
        "OAH could not tell this repository's state from its files.",
        "No action needed. OAH does not run Git to check, and building a map from this app is not \
         available yet.",
    )
    .reason(reason.to_string())
}

fn git(root: &Path, git_dir: &Path, common_dir: &Path, state: Option<&Path>) -> Card {
    match head(git_dir, common_dir) {
        Head::Unborn => {
            return Card::new(
                "no-commit",
                "This Git repository has no commits yet.",
                "Make the first commit, then check again. OAH does not commit for you.",
            );
        }
        Head::Unknown(reason) => return unknown(reason),
        Head::Commit => {}
    }
    if let Some(map) = state.and_then(|state| map_file(root, state, common_dir)) {
        return Card::new(
            "map-found",
            "An Impact Map file exists for this checkout. OAH has not checked whether it is current.",
            "Checking or refreshing it from this app is not available yet. With the OAH source CLI, run \
             oah impact status.",
        )
        .path(map);
    }
    match tracked(git_dir) {
        Tracked::Some => Card::new(
            "ready",
            "Git tracks files here. No Impact Map has been built for this checkout.",
            "Building the map from this app is not available yet. With the OAH source CLI, run oah \
             impact build.",
        ),
        Tracked::None => Card::new(
            "no-tracked-files",
            "This Git repository has commits, but Git tracks no files right now.",
            "Add and commit the files the map should cover, then check again.",
        ),
        Tracked::Unknown => unknown("Git's list of tracked files could not be read directly."),
    }
}

/// The card of the project at `root`. `state` is the source CLI's state folder, when the project has one.
pub(super) fn card(root: &Path, found: &Git, state: Option<&Path>) -> ProjectToolDto {
    let card = match found {
        Git::None => no_git(root),
        Git::WorkTree {
            git_dir,
            common_dir,
        } => git(root, git_dir, common_dir, state),
        Git::Unqualified(reason) => unknown(reason),
    };
    let card = if has_graphify_file(root) {
        card.note(
            "graphify-out/graph.json exists. OAH has not verified it and does not use it for this status.",
        )
    } else {
        card
    };
    card.into_dto(ProjectToolIdDto::ImpactMap)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A hand-made Git folder: these tests start no Git.
    fn repo(head_text: &str) -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let git = dir.path().join(".git");
        fs::create_dir_all(git.join("refs/heads")).unwrap();
        fs::write(git.join("HEAD"), head_text).unwrap();
        (dir, git)
    }

    const ID: &str = "0123456789abcdef0123456789abcdef01234567";

    #[test]
    fn head_is_a_commit_only_when_the_branch_exists_loose_or_packed() {
        let (_dir, git) = repo("ref: refs/heads/main\n");
        assert_eq!(head(&git, &git), Head::Unborn);
        fs::write(
            git.join("packed-refs"),
            format!("# pack-refs\n{ID} refs/heads/other\n"),
        )
        .unwrap();
        assert_eq!(head(&git, &git), Head::Unborn);
        fs::write(git.join("packed-refs"), format!("{ID} refs/heads/main\r\n")).unwrap();
        assert_eq!(head(&git, &git), Head::Commit);
        fs::remove_file(git.join("packed-refs")).unwrap();
        fs::write(git.join("refs/heads/main"), format!("{ID}\n")).unwrap();
        assert_eq!(head(&git, &git), Head::Commit);
    }

    #[test]
    fn a_detached_head_is_a_commit_and_anything_else_is_unknown() {
        let (_dir, git) = repo(&format!("{ID}\n"));
        assert_eq!(head(&git, &git), Head::Commit);
        for text in [
            "",
            "ref: ../../outside\n",
            "ref: refs/heads/a b\n",
            "garbage\n",
        ] {
            fs::write(git.join("HEAD"), text).unwrap();
            assert!(matches!(head(&git, &git), Head::Unknown(_)), "{text:?}");
        }
        fs::write(git.join("HEAD"), "ref: refs/heads/main\n").unwrap();
        fs::create_dir(git.join("reftable")).unwrap();
        assert!(matches!(head(&git, &git), Head::Unknown(_)));
    }

    #[test]
    fn the_index_header_says_whether_files_are_tracked() {
        let (_dir, git) = repo("ref: refs/heads/main\n");
        assert_eq!(tracked(&git), Tracked::None, "a missing index is empty");
        let header = |count: u32| {
            [
                b"DIRC".as_slice(),
                &2u32.to_be_bytes(),
                &count.to_be_bytes(),
            ]
            .concat()
        };
        fs::write(git.join("index"), header(3)).unwrap();
        assert_eq!(tracked(&git), Tracked::Some);
        fs::write(git.join("index"), header(0)).unwrap();
        assert_eq!(tracked(&git), Tracked::None);
        fs::write(git.join("sharedindex.0123"), b"x").unwrap();
        assert_eq!(
            tracked(&git),
            Tracked::Unknown,
            "a split index hides the count"
        );
        fs::write(git.join("index"), b"not an index").unwrap();
        assert_eq!(tracked(&git), Tracked::Unknown);
    }
}
