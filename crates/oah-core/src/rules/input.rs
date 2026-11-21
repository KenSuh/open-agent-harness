// 규칙 입력: 프로젝트 파일을 링크를 따라가지 않고 읽어, 순수 계획 함수가 받을 바이트·없음·링크 상태로 만든다.
//! What the pure planners read. [`ProjectFile::read`] inspects every component of a project-relative path
//! without following it, so a linked file or a linked folder on the way is reported as a link and never read
//! through (v0.4:src/oah/rules.py:105-109 refuses to write through links). Reading writes nothing.

use crate::fsx::{self, FsxError, FsxErrorKind, links::Entry};
use std::path::{Path, PathBuf};

/// `AGENTS.md`, relative to the project root.
pub const AGENTS_MD: &str = "AGENTS.md";
/// `CLAUDE.md`, relative to the project root.
pub const CLAUDE_MD: &str = "CLAUDE.md";
/// The project's own rules, appended inside the block.
pub const LOCAL_RULES: &str = ".oah/rules.local.md";

/// A project file as the planners see it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProjectFile {
    Missing,
    /// The file, or a folder on its path, is a symbolic link or junction. `target` is that link's stored target.
    Link {
        target: PathBuf,
    },
    Regular(Vec<u8>),
}

impl ProjectFile {
    /// Reads `relative` (with `/`) under `root` without following any link below `root`.
    pub fn read(root: &Path, relative: &str) -> Result<ProjectFile, FsxError> {
        let mut path = root.to_path_buf();
        let mut parts = relative.split('/').peekable();
        while let Some(part) = parts.next() {
            path.push(part);
            let last = parts.peek().is_none();
            match fsx::links::inspect(&path)? {
                Entry::Missing => return Ok(ProjectFile::Missing),
                Entry::Symlink { target } | Entry::Junction { target } => {
                    return Ok(ProjectFile::Link { target });
                }
                // A folder on the way is walked on; a regular file there fails the next inspect or read at once.
                Entry::Directory if !last => {}
                Entry::File if last => {}
                // The final entry is neither a file nor a folder to walk through: a pipe or socket would block
                // a read until a writer shows up, so it is refused before any read.
                _ if last => return Err(refuse_unsupported(&path)),
                _ => {}
            }
        }
        Ok(match fsx::read_optional(&path)? {
            Some(bytes) => ProjectFile::Regular(bytes),
            None => ProjectFile::Missing,
        })
    }

    pub fn exists(&self) -> bool {
        !matches!(self, ProjectFile::Missing)
    }

    /// The bytes, when this is a regular file.
    pub fn bytes(&self) -> Option<&[u8]> {
        match self {
            ProjectFile::Regular(bytes) => Some(bytes),
            _ => None,
        }
    }
}

/// The file layer's own refusal for an entry that is not a regular file (a pipe, a socket, a device, a folder
/// where a file is needed). It is built from the path alone: nothing is opened or read, so it cannot block.
fn refuse_unsupported(path: &Path) -> FsxError {
    FsxError::new(
        FsxErrorKind::Io,
        path,
        "not a regular file; only regular files are read",
    )
}

/// What `.oah/rules.local.md` holds when inspected without following links or reparse entries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum LocalRulesRead {
    Missing,
    Regular(Vec<u8>),
    /// The traversed ancestor (`.oah`) or leaf (`.oah/rules.local.md`) is a link or reparse entry.
    RefusedLink {
        entry: &'static str,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum LocalStep {
    Continue,
    ReadRegular,
    Stop(LocalRulesRead),
}

/// Classifies one inspected component of `.oah/rules.local.md` without exposing stored link targets.
fn classify_local_component(
    entry: Entry,
    is_reparse: bool,
    last: bool,
    path: &Path,
    label: &'static str,
) -> Result<LocalStep, FsxError> {
    match entry {
        Entry::Missing => Ok(LocalStep::Stop(LocalRulesRead::Missing)),
        Entry::Symlink { .. } | Entry::Junction { .. } => {
            Ok(LocalStep::Stop(LocalRulesRead::RefusedLink {
                entry: label,
            }))
        }
        _ if is_reparse => Ok(LocalStep::Stop(LocalRulesRead::RefusedLink {
            entry: label,
        })),
        Entry::Directory if !last => Ok(LocalStep::Continue),
        Entry::File if last => Ok(LocalStep::ReadRegular),
        _ => Err(refuse_unsupported(path)),
    }
}

fn inspect_local_component(
    path: &Path,
    last: bool,
    label: &'static str,
) -> Result<LocalStep, FsxError> {
    let entry = fsx::links::inspect(path)?;
    let is_reparse = match entry {
        Entry::Missing | Entry::Symlink { .. } | Entry::Junction { .. } => false,
        _ => is_reparse_point(path)?,
    };
    classify_local_component(entry, is_reparse, last, path, label)
}

#[cfg(windows)]
fn is_reparse_point(path: &Path) -> Result<bool, FsxError> {
    use std::os::windows::fs::MetadataExt;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;
    match std::fs::symlink_metadata(path) {
        Ok(metadata) => Ok(metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(FsxError::io(
            FsxErrorKind::Io,
            path,
            "cannot inspect",
            error,
        )),
    }
}

#[cfg(not(windows))]
fn is_reparse_point(_path: &Path) -> Result<bool, FsxError> {
    Ok(false)
}

/// Reads `.oah/rules.local.md` under `root` only when absent or a regular in-project file.
///
/// Inspects `.oah` and `.oah/rules.local.md` without following links or reparse entries, then opens the leaf
/// through `fsx::read_regular_optional`.
pub(crate) fn read_local_rules_checked(root: &Path) -> Result<LocalRulesRead, FsxError> {
    let dir_path = root.join(".oah");
    match inspect_local_component(&dir_path, false, ".oah")? {
        LocalStep::Continue => {}
        LocalStep::Stop(result) => return Ok(result),
        LocalStep::ReadRegular => unreachable!("non-final component cannot read a file"),
    }
    let file_path = dir_path.join("rules.local.md");
    match inspect_local_component(&file_path, true, LOCAL_RULES)? {
        LocalStep::ReadRegular => {}
        LocalStep::Stop(result) => return Ok(result),
        LocalStep::Continue => unreachable!("final component cannot continue"),
    }
    Ok(match fsx::read_regular_optional(&file_path)? {
        Some((bytes, _)) => LocalRulesRead::Regular(bytes),
        None => LocalRulesRead::Missing,
    })
}

fn refuse_link_io(path: &Path, entry: &'static str) -> FsxError {
    FsxError::new(
        FsxErrorKind::Io,
        path,
        format!("{entry} is a link or reparse entry; only regular project files are read"),
    )
}

/// The three files the block plan reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RulesFiles {
    pub agents: ProjectFile,
    pub claude: ProjectFile,
    /// `.oah/rules.local.md`, when absent (`None`) or a regular file in the selected project (`Some`).
    pub local_rules: Option<Vec<u8>>,
}

impl RulesFiles {
    pub fn read(root: &Path) -> Result<RulesFiles, FsxError> {
        let agents = ProjectFile::read(root, AGENTS_MD)?;
        let claude = ProjectFile::read(root, CLAUDE_MD)?;
        let local_rules = match read_local_rules_checked(root)? {
            LocalRulesRead::Missing => None,
            LocalRulesRead::Regular(bytes) => Some(bytes),
            LocalRulesRead::RefusedLink { entry } => {
                return Err(refuse_link_io(
                    &root.join(".oah").join("rules.local.md"),
                    entry,
                ));
            }
        };
        Ok(RulesFiles {
            agents,
            claude,
            local_rules,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_component_refuses_links_and_reparse_entries_at_leaf_and_ancestor() {
        let path = Path::new("/project/.oah/rules.local.md");
        let ancestor = Path::new("/project/.oah");
        let link_target = PathBuf::from("inert.md");

        for (target_path, last, label) in [(ancestor, false, ".oah"), (path, true, LOCAL_RULES)] {
            assert_eq!(
                classify_local_component(Entry::Missing, false, last, target_path, label).unwrap(),
                LocalStep::Stop(LocalRulesRead::Missing)
            );
            assert_eq!(
                classify_local_component(
                    Entry::Symlink {
                        target: link_target.clone()
                    },
                    false,
                    last,
                    target_path,
                    label
                )
                .unwrap(),
                LocalStep::Stop(LocalRulesRead::RefusedLink { entry: label })
            );
            assert_eq!(
                classify_local_component(
                    Entry::Junction {
                        target: link_target.clone()
                    },
                    true,
                    last,
                    target_path,
                    label
                )
                .unwrap(),
                LocalStep::Stop(LocalRulesRead::RefusedLink { entry: label })
            );
            for reparse_entry in [Entry::Directory, Entry::File, Entry::Other] {
                assert_eq!(
                    classify_local_component(reparse_entry, true, last, target_path, label)
                        .unwrap(),
                    LocalStep::Stop(LocalRulesRead::RefusedLink { entry: label })
                );
            }
        }

        assert_eq!(
            classify_local_component(Entry::Directory, false, false, ancestor, ".oah").unwrap(),
            LocalStep::Continue
        );
        assert_eq!(
            classify_local_component(Entry::File, false, true, path, LOCAL_RULES).unwrap(),
            LocalStep::ReadRegular
        );

        for (entry, last, p, label) in [
            (Entry::Directory, true, path, LOCAL_RULES),
            (Entry::File, false, ancestor, ".oah"),
            (Entry::Other, false, ancestor, ".oah"),
            (Entry::Other, true, path, LOCAL_RULES),
        ] {
            let err = classify_local_component(entry, false, last, p, label).unwrap_err();
            assert_eq!(err.kind(), FsxErrorKind::Io);
            assert_eq!(err.path(), p);
            assert!(err.io_error().is_none());
        }
    }
}
