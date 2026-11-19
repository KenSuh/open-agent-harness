// 관리 링크의 상태를 읽고, 등록된 Linux 네임스페이스의 링크·복사 작업을 제공한다.
//! Managed links from an agent's folder to a store folder.
//!
//! [`remove`] preserves present entries described by legacy [`ManagedLink`] records. In an enrolled Linux
//! namespace, [`create_protected`] acquires a native symlink and [`create_protected_copy`] acquires a
//! directory copy. Both issue a persistable
//! [`ProtectedLinkRef`] for [`remove_protected`].

use super::atomic::Fingerprint;
use super::error::{FsxError, FsxErrorKind};
#[cfg(test)]
use super::identity::EntryId;
pub use super::identity::EntryIdentity;
pub use super::ownership::{
    ProtectedLinkRef, create_protected, create_protected_copy, remove_protected,
};
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};

/// What exists at a path, read without following it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Entry {
    Missing,
    /// A symbolic link and its target exactly as stored, relative or absolute.
    Symlink {
        target: PathBuf,
    },
    /// A Windows directory junction and its absolute target.
    Junction {
        target: PathBuf,
    },
    /// A real folder.
    Directory,
    /// A regular file.
    File,
    /// Something else, such as a socket or another kind of reparse point.
    Other,
}

/// Reads what is at `path` without following links.
pub fn inspect(path: &Path) -> Result<Entry, FsxError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Entry::Missing),
        Err(error) => {
            return Err(FsxError::io(
                FsxErrorKind::Io,
                path,
                "cannot inspect",
                error,
            ));
        }
    };
    let file_type = metadata.file_type();
    if file_type.is_symlink() {
        #[cfg(windows)]
        if let Ok(target) = junction::get_target(path) {
            return Ok(Entry::Junction { target });
        }
        let target = fs::read_link(path)
            .map_err(|error| FsxError::io(FsxErrorKind::Io, path, "cannot read the link", error))?;
        return Ok(Entry::Symlink { target });
    }
    if file_type.is_dir() {
        Ok(Entry::Directory)
    } else if file_type.is_file() {
        Ok(Entry::File)
    } else {
        Ok(Entry::Other)
    }
}

/// How a symlink stores its target. Junctions always store an absolute target.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetStyle {
    /// The source path as given; used for links from a user's agent folders into user stores.
    Absolute,
    /// A path relative to the link's folder, so a project keeps working when it moves; used inside projects.
    Relative,
}

/// The kind of entry a managed link is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LinkKind {
    Symlink,
    Junction,
    /// A copy of the source tree, made because no native link was possible. It uses disk space and does not
    /// follow later changes to the source; it must be copied again after the source changes.
    Copy,
}

/// One file of a copy fallback.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CopiedFile {
    /// The path below the copy's root.
    pub relative: PathBuf,
    pub fingerprint: Fingerprint,
    /// Whether the source file had an executable bit (always false on Windows).
    pub executable: bool,
}

/// Legacy inventory for one previously recorded managed link, accepted by [`remove`].
///
/// A plain record with no hidden state: the caller stores these fields (a [`Fingerprint`] round-trips through
/// [`Fingerprint::to_hex`] and [`Fingerprint::from_hex`]) and may rebuild the record in a later process.
/// The record has no authority to delete a present entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedLink {
    pub destination: PathBuf,
    pub source: PathBuf,
    pub kind: LinkKind,
    /// Identity captured from the created entry, retained unchanged across restoration.
    pub identity: Option<EntryIdentity>,
    /// The stored target of a symlink or junction; `None` for a copy.
    pub target: Option<PathBuf>,
    /// For a copy: every folder below the root, parents before children.
    pub folders: Vec<PathBuf>,
    /// For a copy: every file below the root.
    pub files: Vec<CopiedFile>,
}

/// A request to link `destination` to the folder `source`.
#[derive(Debug, Clone, Copy)]
pub struct LinkRequest<'a> {
    /// The store folder; absolute, and a real folder rather than a link.
    pub source: &'a Path,
    /// Where the link goes; absolute, in an existing folder.
    pub destination: &'a Path,
    pub style: TargetStyle,
    /// Whether a copy may stand in when no native link can be made.
    pub allow_copy: bool,
}

/// The result of [`create`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinkOutcome {
    pub link: ManagedLink,
    /// Whether this operation acquired the entry. Occupied entries are refused, never adopted.
    pub created: bool,
    /// Why the native link was not made, when the result is a copy.
    pub fallback: Option<String>,
}

/// Makes the native link of this platform.
pub trait NativeLinker {
    /// The kind of link this linker makes.
    fn kind(&self) -> LinkKind;
    /// Creates the link at `destination`; `target` is the text to store (symlinks) and `source` the folder.
    ///
    /// [`create_with`] currently reports unavailable staging before invoking this method.
    fn link(&self, source: &Path, destination: &Path, target: &Path) -> io::Result<()>;
}

/// A direct, unmanaged primitive for Unix symlinks and Windows directory junctions.
///
/// [`NativeLinker::link`] acts at the supplied destination and returns an I/O result. The caller
/// owns validation, staging and cleanup, including any directory left by a failed Windows junction
/// attempt. For an ownership record, use the protected APIs in an enrolled namespace.
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemLinker;

impl NativeLinker for SystemLinker {
    fn kind(&self) -> LinkKind {
        if cfg!(windows) {
            LinkKind::Junction
        } else {
            LinkKind::Symlink
        }
    }

    #[cfg(unix)]
    fn link(&self, _source: &Path, destination: &Path, target: &Path) -> io::Result<()> {
        std::os::unix::fs::symlink(target, destination)
    }

    #[cfg(windows)]
    fn link(&self, source: &Path, destination: &Path, _target: &Path) -> io::Result<()> {
        junction::create(source, destination)
    }

    #[cfg(not(any(unix, windows)))]
    fn link(&self, _source: &Path, _destination: &Path, _target: &Path) -> io::Result<()> {
        Err(io::Error::from(io::ErrorKind::Unsupported))
    }
}

/// Requests a managed native link, with a copy fallback when allowed and staging authority is available.
///
/// Valid ordinary requests currently return [`FsxErrorKind::LinkUnsupported`] before staging on every
/// platform. Enrolled Linux callers can use [`create_protected`] or [`create_protected_copy`].
pub fn create(request: &LinkRequest<'_>) -> Result<LinkOutcome, FsxError> {
    create_with(request, &SystemLinker)
}

/// [`create`] with an explicit native linker.
///
/// An occupied destination is [`FsxErrorKind::LinkExists`]. A request that reaches staging currently returns
/// [`FsxErrorKind::LinkUnsupported`] without calling [`NativeLinker::link`] or starting a copy.
pub fn create_with(
    request: &LinkRequest<'_>,
    _linker: &dyn NativeLinker,
) -> Result<LinkOutcome, FsxError> {
    let source = request.source;
    let destination = request.destination;
    require_absolute(source)?;
    require_absolute(destination)?;
    match fs::symlink_metadata(source) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
        Ok(_) => {
            return Err(FsxError::new(
                FsxErrorKind::Io,
                source,
                "is not a real folder; a store entry must be a folder",
            ));
        }
        Err(error) => {
            return Err(FsxError::io(
                FsxErrorKind::Io,
                source,
                "cannot read the source folder",
                error,
            ));
        }
    }
    let parent = destination
        .parent()
        .ok_or_else(|| FsxError::new(FsxErrorKind::Io, destination, "has no parent folder"))?;
    destination
        .file_name()
        .ok_or_else(|| FsxError::new(FsxErrorKind::Io, destination, "has no file name"))?;
    if !parent.is_dir() {
        return Err(FsxError::new(
            FsxErrorKind::Io,
            destination,
            "its folder does not exist",
        ));
    }

    let existing = inspect(destination)?;
    if existing != Entry::Missing {
        return Err(FsxError::new(
            FsxErrorKind::LinkExists,
            destination,
            "something else is already there; it was left alone",
        ));
    }

    Err(FsxError::new(
        FsxErrorKind::LinkUnsupported,
        destination,
        "cannot prove authority for managed link staging at this destination",
    ))
}

/// Whether a removal deleted something.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Removal {
    Removed,
    /// Nothing was at the destination any more.
    AlreadyGone,
}

/// Checks a legacy link record for ordinary absence.
///
/// [`Removal::AlreadyGone`] is an advisory metadata observation with no durability or
/// concurrent ownership guarantee. A present entry or an unverified ancestor, including a
/// symlink or Windows reparse point, returns [`FsxErrorKind::NotOwned`]. A destination ending
/// in a separator or a dot component is refused before lookup. The check leaves entries in place.
pub fn remove(link: &ManagedLink, stores: &[&Path]) -> Result<Removal, FsxError> {
    let destination = link.destination.as_path();
    require_absolute(&link.source)?;
    require_absolute(destination)?;
    let raw_leaf = destination
        .as_os_str()
        .as_encoded_bytes()
        .rsplit(|&byte| std::path::is_separator(char::from(byte)))
        .next();
    match raw_leaf {
        Some([]) => return Err(not_owned(link, "its destination has a trailing separator")),
        Some([b'.']) => {
            return Err(not_owned(
                link,
                "its destination has a terminal dot component",
            ));
        }
        Some([b'.', b'.']) => {
            return Err(not_owned(
                link,
                "its destination has a terminal parent component",
            ));
        }
        _ => {}
    }
    if lexical(destination).is_none() {
        return Err(not_owned(
            link,
            "its destination contains a parent component",
        ));
    }
    if !source_in_stores(&link.source, stores) {
        return Err(not_owned(
            link,
            "its source is outside the known stores or is not a plain path",
        ));
    }
    // Probe complete absolute ancestors from the root down, without opening a bare drive prefix.
    let parents: Vec<_> = destination
        .ancestors()
        .skip(1)
        .take_while(|parent| parent.is_absolute())
        .collect();
    for &parent in parents.iter().rev() {
        match copy_root_is_directory(parent) {
            Ok(true) => {}
            Ok(false) => return Err(not_owned(link, "its destination has an unverified parent")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return Ok(Removal::AlreadyGone);
            }
            Err(error) => {
                return Err(FsxError::io(
                    FsxErrorKind::Io,
                    parent,
                    "cannot inspect the destination parent",
                    error,
                ));
            }
        }
    }
    match fs::symlink_metadata(destination) {
        Ok(_) => Err(not_owned(
            link,
            "a legacy record cannot prove ownership of the present entry",
        )),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            if parents.is_empty() {
                Err(not_owned(link, "its destination has no observable parent"))
            } else {
                Ok(Removal::AlreadyGone)
            }
        }
        Err(error) => Err(FsxError::io(
            FsxErrorKind::Io,
            destination,
            "cannot inspect",
            error,
        )),
    }
}

fn not_owned(link: &ManagedLink, why: &str) -> FsxError {
    FsxError::new(FsxErrorKind::NotOwned, &link.destination, why)
}

pub(super) fn source_in_stores(source: &Path, stores: &[&Path]) -> bool {
    lexical(source).is_some_and(|source| {
        stores
            .iter()
            .filter(|store| store.is_absolute())
            .any(|store| lexical(store).is_some_and(|store| source.starts_with(&store)))
    })
}

fn require_absolute(path: &Path) -> Result<(), FsxError> {
    if path.is_absolute() {
        Ok(())
    } else {
        Err(FsxError::new(
            FsxErrorKind::Io,
            path,
            "is not an absolute path",
        ))
    }
}

/// The path with `.` removed; `None` when it contains `..`, which ownership checks never guess through.
fn lexical(path: &Path) -> Option<PathBuf> {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => return None,
            other => out.push(other.as_os_str()),
        }
    }
    Some(out)
}

#[cfg(target_os = "linux")]
pub(super) fn relative_between(folder: &Path, source: &Path) -> Result<PathBuf, FsxError> {
    let from: Vec<Component<'_>> = folder.components().collect();
    let to: Vec<Component<'_>> = source.components().collect();
    let common = from.iter().zip(&to).take_while(|(a, b)| a == b).count();
    if common == 0 {
        return Err(FsxError::new(
            FsxErrorKind::LinkUnsupported,
            source,
            "has no relative path from the link's folder (different drive or root)",
        ));
    }
    let mut out = PathBuf::new();
    for _ in common..from.len() {
        out.push("..");
    }
    for component in &to[common..] {
        out.push(component.as_os_str());
    }
    if out.as_os_str().is_empty() {
        out.push(".");
    }
    Ok(out)
}

fn copy_root_is_directory(root: &Path) -> io::Result<bool> {
    let metadata = fs::symlink_metadata(root)?;
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Ok(false);
        }
    }
    Ok(metadata.is_dir())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn folder_with_source() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let source = root.join("source");
        fs::create_dir(&source).unwrap();
        fs::write(source.join("keep.txt"), "source bytes").unwrap();
        (dir, root, source)
    }

    #[test]
    fn lexical_drops_dots_and_refuses_parent_steps() {
        assert_eq!(lexical(Path::new("/a/./b")).unwrap(), PathBuf::from("/a/b"));
        assert!(lexical(Path::new("/a/../b")).is_none());
    }

    #[test]
    fn a_descriptor_outside_every_store_is_not_owned() {
        let link = ManagedLink {
            destination: PathBuf::from(if cfg!(windows) {
                r"C:\x\dest"
            } else {
                "/x/dest"
            }),
            source: PathBuf::from(if cfg!(windows) {
                r"C:\x\elsewhere\s"
            } else {
                "/x/elsewhere/s"
            }),
            kind: LinkKind::Symlink,
            identity: None,
            target: Some(PathBuf::from("s")),
            folders: Vec::new(),
            files: Vec::new(),
        };
        let store = PathBuf::from(if cfg!(windows) {
            r"C:\x\store"
        } else {
            "/x/store"
        });
        let error = remove(&link, &[store.as_path()]).unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    }

    #[test]
    fn a_copy_record_with_a_path_outside_the_copy_is_refused() {
        let (_dir, root, source) = folder_with_source();
        let store = root.clone();
        let destination = root.join("copy");
        fs::create_dir(&destination).unwrap();
        fs::write(destination.join("keep.txt"), "source bytes").unwrap();
        let file = CopiedFile {
            relative: PathBuf::from("..").join("keep.txt"),
            fingerprint: Fingerprint::of(b"source bytes"),
            executable: false,
        };
        let link = ManagedLink {
            destination: destination.clone(),
            source,
            kind: LinkKind::Copy,
            identity: Some(EntryId::of(&destination).unwrap().evidence()),
            target: None,
            folders: Vec::new(),
            files: vec![file],
        };
        let error = remove(&link, &[store.as_path()]).unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::NotOwned);
        assert_eq!(
            fs::read(destination.join("keep.txt")).unwrap(),
            b"source bytes"
        );
    }
}
