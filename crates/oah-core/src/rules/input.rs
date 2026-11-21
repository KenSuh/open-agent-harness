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

/// Reads `.oah/rules.local.md` through links, but never opens a pipe or a socket, which would block.
fn read_local_rules(root: &Path) -> Result<Option<Vec<u8>>, FsxError> {
    let path = root.join(LOCAL_RULES);
    match std::fs::metadata(&path) {
        Ok(metadata) if !metadata.is_file() => Err(refuse_unsupported(&path)),
        _ => fsx::read_optional(&path),
    }
}

/// The three files the block plan reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RulesFiles {
    pub agents: ProjectFile,
    pub claude: ProjectFile,
    /// `.oah/rules.local.md`, read through links like v0.4.0 (v0.4:src/oah/rules.py:118); `None` when absent.
    pub local_rules: Option<Vec<u8>>,
}

impl RulesFiles {
    pub fn read(root: &Path) -> Result<RulesFiles, FsxError> {
        Ok(RulesFiles {
            agents: ProjectFile::read(root, AGENTS_MD)?,
            claude: ProjectFile::read(root, CLAUDE_MD)?,
            local_rules: read_local_rules(root)?,
        })
    }
}
