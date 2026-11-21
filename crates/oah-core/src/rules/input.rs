// 규칙 입력: 프로젝트 파일을 링크나 재파스 지점을 따라가지 않고 읽어, 순수 계획 함수가 받을 바이트·없음·링크 상태로 만든다.
//! What the pure planners read. [`ProjectFile::read`] inspects every component of a project-relative path
//! without following it, so a linked or reparse file or folder on the way is reported as a link and never read
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
    /// The file, or a folder on its path, is a symbolic link, junction or reparse entry. `target` is that link's
    /// stored target, or the reparse entry's path when no link target is exposed.
    Link {
        target: PathBuf,
    },
    Regular(Vec<u8>),
}

/// What one inspected component of a project-relative path permits next.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ComponentStep {
    Continue,
    ReadRegular,
    Stop(ProjectFile),
}

impl ProjectFile {
    /// Reads `relative` (with `/`) under `root` without following any link or reparse entry below `root`.
    pub fn read(root: &Path, relative: &str) -> Result<ProjectFile, FsxError> {
        let mut path = root.to_path_buf();
        let mut parts = relative.split('/').peekable();
        while let Some(part) = parts.next() {
            if part.is_empty() || part == "." || part == ".." || part.contains('\\') {
                return Err(refuse_unsupported(&path));
            }
            path.push(part);
            let last = parts.peek().is_none();
            match inspect_component(&path, last)? {
                ComponentStep::Continue => {}
                ComponentStep::ReadRegular => break,
                ComponentStep::Stop(file) => return Ok(file),
            }
        }
        Ok(match fsx::read_regular_optional(&path)? {
            Some((bytes, _)) => ProjectFile::Regular(bytes),
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

/// Classifies one inspected project-relative component without following links or reparse entries.
pub(crate) fn classify_component(
    entry: Entry,
    is_reparse: bool,
    last: bool,
    path: &Path,
) -> Result<ComponentStep, FsxError> {
    match entry {
        Entry::Missing => Ok(ComponentStep::Stop(ProjectFile::Missing)),
        Entry::Symlink { target } | Entry::Junction { target } => {
            Ok(ComponentStep::Stop(ProjectFile::Link { target }))
        }
        _ if is_reparse => Ok(ComponentStep::Stop(ProjectFile::Link {
            target: path.to_path_buf(),
        })),
        Entry::Directory if !last => Ok(ComponentStep::Continue),
        Entry::File if last => Ok(ComponentStep::ReadRegular),
        // Any other entry at the leaf (a pipe, socket, device or folder) or a non-folder ancestor is refused
        // before opening or descending into it.
        _ => Err(refuse_unsupported(path)),
    }
}

fn inspect_component(path: &Path, last: bool) -> Result<ComponentStep, FsxError> {
    let entry = fsx::links::inspect(path)?;
    let is_reparse = match entry {
        Entry::Missing | Entry::Symlink { .. } | Entry::Junction { .. } => false,
        _ => is_reparse_point(path)?,
    };
    classify_component(entry, is_reparse, last, path)
}

#[cfg(windows)]
pub(crate) fn is_reparse_point(path: &Path) -> Result<bool, FsxError> {
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
pub(crate) fn is_reparse_point(_path: &Path) -> Result<bool, FsxError> {
    Ok(false)
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

fn refuse_link_io(path: &Path, target: &Path) -> FsxError {
    FsxError::new(
        FsxErrorKind::Io,
        path,
        format!(
            "is a link or reparse entry ({}); only regular project files are read",
            target.display()
        ),
    )
}

fn join_relative(root: &Path, relative: &str) -> PathBuf {
    relative
        .split('/')
        .fold(root.to_path_buf(), |path, part| path.join(part))
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
        let local_rules = match ProjectFile::read(root, LOCAL_RULES)? {
            ProjectFile::Missing => None,
            ProjectFile::Regular(bytes) => Some(bytes),
            ProjectFile::Link { target } => {
                return Err(refuse_link_io(&join_relative(root, LOCAL_RULES), &target));
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

        for (target_path, last) in [(ancestor, false), (path, true)] {
            assert_eq!(
                classify_component(Entry::Missing, false, last, target_path).unwrap(),
                ComponentStep::Stop(ProjectFile::Missing)
            );
            assert_eq!(
                classify_component(
                    Entry::Symlink {
                        target: link_target.clone()
                    },
                    false,
                    last,
                    target_path
                )
                .unwrap(),
                ComponentStep::Stop(ProjectFile::Link {
                    target: link_target.clone()
                })
            );
            assert_eq!(
                classify_component(
                    Entry::Junction {
                        target: link_target.clone()
                    },
                    true,
                    last,
                    target_path
                )
                .unwrap(),
                ComponentStep::Stop(ProjectFile::Link {
                    target: link_target.clone()
                })
            );
            for reparse_entry in [Entry::Directory, Entry::File, Entry::Other] {
                assert_eq!(
                    classify_component(reparse_entry, true, last, target_path).unwrap(),
                    ComponentStep::Stop(ProjectFile::Link {
                        target: target_path.to_path_buf()
                    })
                );
            }
        }

        assert_eq!(
            classify_component(Entry::Directory, false, false, ancestor).unwrap(),
            ComponentStep::Continue
        );
        assert_eq!(
            classify_component(Entry::File, false, true, path).unwrap(),
            ComponentStep::ReadRegular
        );

        for (entry, last, p) in [
            (Entry::Directory, true, path),
            (Entry::File, false, ancestor),
            (Entry::Other, false, ancestor),
            (Entry::Other, true, path),
        ] {
            let err = classify_component(entry, false, last, p).unwrap_err();
            assert_eq!(err.kind(), FsxErrorKind::Io);
            assert_eq!(err.path(), p);
            assert!(err.io_error().is_none());
        }
    }
}
