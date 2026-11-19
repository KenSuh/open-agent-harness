// fsx 오류 형식: 바쁨·입출력·검증 실패·이미 있음·링크 소유 실패를 안정된 코드와 경로로 나눠 알린다.
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};

/// What went wrong, as a stable category with its `OAH_*` code.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FsxErrorKind {
    /// Another writer held the file's lock for the whole bounded wait.
    Busy,
    /// An I/O error before anything was replaced; the destination is unchanged.
    Io,
    /// The destination was replaced, but syncing its folder or reading it back failed or differed.
    /// The caller must not assume either the old or the new content.
    Unverified,
    /// A file that must not be overwritten already exists.
    Exists,
    /// A link was asked for where something else already exists; it was left alone.
    LinkExists,
    /// The entry on disk does not match the ownership evidence; it was left alone.
    NotOwned,
    /// Neither a native link nor an allowed fallback could be made.
    LinkUnsupported,
    /// A tree to copy contains a symlink or junction, which is never followed.
    TreeHasLink,
}

impl FsxErrorKind {
    /// The stable `OAH_*` code.
    pub fn code(self) -> &'static str {
        match self {
            FsxErrorKind::Busy => "OAH_FILE_BUSY",
            FsxErrorKind::Io => "OAH_FILE_IO",
            FsxErrorKind::Unverified => "OAH_FILE_UNVERIFIED",
            FsxErrorKind::Exists => "OAH_FILE_EXISTS",
            FsxErrorKind::LinkExists => "OAH_LINK_EXISTS",
            FsxErrorKind::NotOwned => "OAH_LINK_NOT_OWNED",
            FsxErrorKind::LinkUnsupported => "OAH_LINK_UNSUPPORTED",
            FsxErrorKind::TreeHasLink => "OAH_LINK_TREE_HAS_LINK",
        }
    }
}

/// A file-system failure with its path and, when there is one, the operating-system error.
#[derive(Debug)]
pub struct FsxError {
    kind: FsxErrorKind,
    path: PathBuf,
    detail: String,
    source: Option<io::Error>,
    /// A cleanup that failed after this failure; it never replaces this failure.
    cleanup: Option<Box<FsxError>>,
}

impl FsxError {
    pub(crate) fn new(kind: FsxErrorKind, path: &Path, detail: impl Into<String>) -> Self {
        FsxError {
            kind,
            path: path.to_path_buf(),
            detail: detail.into(),
            source: None,
            cleanup: None,
        }
    }

    pub(crate) fn io(kind: FsxErrorKind, path: &Path, what: &str, source: io::Error) -> Self {
        FsxError {
            kind,
            path: path.to_path_buf(),
            detail: format!("{what}: {source}"),
            source: Some(source),
            cleanup: None,
        }
    }

    pub fn kind(&self) -> FsxErrorKind {
        self.kind
    }

    pub fn code(&self) -> &'static str {
        self.kind.code()
    }

    /// The file or folder the operation was about.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The operating-system error, when one caused this.
    pub fn io_error(&self) -> Option<&io::Error> {
        self.source.as_ref()
    }

    /// When cleaning up after this failure also failed: what was left in place, and why.
    pub fn cleanup_error(&self) -> Option<&FsxError> {
        self.cleanup.as_deref()
    }
}

impl fmt::Display for FsxError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}: {} ({})",
            self.kind.code(),
            self.detail,
            self.path.display()
        )?;
        match &self.cleanup {
            Some(cleanup) => write!(f, "; cleanup also failed: {cleanup}"),
            None => Ok(()),
        }
    }
}

impl std::error::Error for FsxError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.source
            .as_ref()
            .map(|error| error as &(dyn std::error::Error + 'static))
    }
}
