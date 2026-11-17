// 관리 링크: 심볼릭 링크·윈도 정션·복사 대체를 만들고 살피고 지우며, 소유 증거가 파일 시스템과 맞을 때만 지운다.
//! Managed links from an agent's folder to a store folder.
//!
//! A link is made natively (a symlink on macOS and Linux, a directory junction on Windows, which needs no
//! elevation) or, when the caller allows it and the native link cannot be made, as a reported copy. Creating
//! returns a [`ManagedLink`] descriptor; the caller keeps it as ownership evidence. [`remove`] deletes an entry
//! only when the descriptor points into a known store and still matches what is on disk, and it never deletes
//! the link's target. Anything else at a destination is left exactly as it is.

use super::atomic::Fingerprint;
use super::error::{FsxError, FsxErrorKind};
use super::identity::{self, Identity};
use super::staging::CopyStage;
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

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

/// Ownership evidence for one managed link, returned by [`create`] and needed by [`remove`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedLink {
    pub destination: PathBuf,
    pub source: PathBuf,
    pub kind: LinkKind,
    /// The stored target of a symlink or junction; `None` for a copy.
    pub target: Option<PathBuf>,
    /// For a copy: every folder below the root, parents before children.
    pub folders: Vec<PathBuf>,
    /// For a copy: every file below the root.
    pub files: Vec<CopiedFile>,
    identity: Identity,
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
    fn link(&self, source: &Path, destination: &Path, target: &Path) -> io::Result<()>;
}

/// The operating system's own links: symlinks on Unix, directory junctions on Windows.
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

/// Links `request.destination` to `request.source` with the system's native link, falling back to a reported
/// copy when allowed.
pub fn create(request: &LinkRequest<'_>) -> Result<LinkOutcome, FsxError> {
    create_with(request, &SystemLinker)
}

/// [`create`] with an explicit native linker.
///
/// An occupied destination is never adopted or replaced: it is [`FsxErrorKind::LinkExists`]. When the linker
/// fails, the copy fallback runs only if the request allows it, and the outcome then names the reason.
pub fn create_with(
    request: &LinkRequest<'_>,
    linker: &dyn NativeLinker,
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
    if !parent.is_dir() {
        return Err(FsxError::new(
            FsxErrorKind::Io,
            destination,
            "its folder does not exist",
        ));
    }

    let kind = linker.kind();
    let target = match kind {
        LinkKind::Symlink => match request.style {
            TargetStyle::Absolute => source.to_path_buf(),
            TargetStyle::Relative => relative_target(parent, source)?,
        },
        LinkKind::Junction | LinkKind::Copy => source.to_path_buf(),
    };

    let existing = inspect(destination)?;
    if existing != Entry::Missing {
        return Err(FsxError::new(
            FsxErrorKind::LinkExists,
            destination,
            "something else is already there; it was left alone",
        ));
    }

    if kind == LinkKind::Copy {
        return copy_fallback(request, None);
    }
    match linker.link(source, destination, &target) {
        Ok(()) => match verify_native(destination, kind, &target, source) {
            Ok(identity) => Ok(LinkOutcome {
                link: native_descriptor(request, kind, target, identity),
                created: true,
                fallback: None,
            }),
            Err(error) => {
                let removed = error.kind() == FsxErrorKind::LinkUnsupported
                    && inspect(destination)? == Entry::Missing;
                if request.allow_copy && removed {
                    copy_fallback(request, Some(error.detail().to_string()))
                } else {
                    Err(error)
                }
            }
        },
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => Err(FsxError::new(
            FsxErrorKind::LinkExists,
            destination,
            "something appeared there while linking; it was left alone",
        )),
        Err(error) => {
            let reason = format!("{} unavailable: {error}", kind_word(kind));
            if request.allow_copy {
                copy_fallback(request, Some(reason))
            } else {
                Err(FsxError::io(
                    FsxErrorKind::LinkUnsupported,
                    destination,
                    &format!("cannot make a {}", kind_word(kind)),
                    error,
                ))
            }
        }
    }
}

/// Whether a removal deleted something.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Removal {
    Removed,
    /// Nothing was at the destination any more.
    AlreadyGone,
}

/// Removes a managed link, checking the evidence first.
///
/// The descriptor's source must lie inside one of `stores`, and the entry at its destination must still be the
/// same symlink text, the same junction target, or a copy whose every folder and file matches the record.
/// Otherwise nothing is touched and the result is [`FsxErrorKind::NotOwned`]. Removing a link deletes only the
/// link; removing a copy deletes only the recorded files and folders.
pub fn remove(link: &ManagedLink, stores: &[&Path]) -> Result<Removal, FsxError> {
    require_absolute(&link.source)?;
    require_absolute(&link.destination)?;
    let source =
        lexical(&link.source).ok_or_else(|| not_owned(link, "its source is not a plain path"))?;
    let in_store = stores.iter().any(|store| match lexical(store) {
        Some(store) => source.starts_with(&store),
        None => false,
    });
    if !in_store {
        return Err(not_owned(link, "its source is outside the known stores"));
    }
    let entry = inspect(&link.destination)?;
    if entry == Entry::Missing {
        return Ok(Removal::AlreadyGone);
    }
    if !identity::matches(&link.destination, &link.identity) {
        return Err(not_owned(link, "another entry replaced the managed entry"));
    }
    match link.kind {
        LinkKind::Symlink | LinkKind::Junction => {
            let target = link
                .target
                .as_ref()
                .ok_or_else(|| not_owned(link, "the descriptor has no link target"))?;
            if !same_native_link(&entry, link.kind, target, &link.source) {
                return Err(not_owned(link, "the entry there is not this link"));
            }
            remove_link_entry(&link.destination).map_err(|error| {
                FsxError::io(
                    FsxErrorKind::Io,
                    &link.destination,
                    "cannot remove the link",
                    error,
                )
            })?;
        }
        LinkKind::Copy => {
            if entry != Entry::Directory {
                return Err(not_owned(link, "the entry there is not a folder"));
            }
            let (folders, files) = match walk(&link.destination) {
                Ok(found) => found,
                Err(error) if error.kind() == FsxErrorKind::TreeHasLink => {
                    return Err(not_owned(
                        link,
                        "the copy now contains a link; it was left alone",
                    ));
                }
                Err(error) => return Err(error),
            };
            let mut recorded_files: Vec<(PathBuf, Fingerprint)> = link
                .files
                .iter()
                .map(|file| (file.relative.clone(), file.fingerprint))
                .collect();
            recorded_files.sort();
            let mut found_files: Vec<(PathBuf, Fingerprint)> = files
                .iter()
                .map(|file| (file.relative.clone(), file.fingerprint))
                .collect();
            found_files.sort();
            let mut recorded_folders = link.folders.clone();
            recorded_folders.sort();
            let mut found_folders = folders.clone();
            found_folders.sort();
            if recorded_files != found_files || recorded_folders != found_folders {
                return Err(not_owned(
                    link,
                    "the copy was changed since OAH made it; it was left alone",
                ));
            }
            for file in &files {
                let path = link.destination.join(&file.relative);
                fs::remove_file(&path).map_err(|error| {
                    FsxError::io(
                        FsxErrorKind::Io,
                        &path,
                        "cannot remove a copied file",
                        error,
                    )
                })?;
            }
            for folder in folders.iter().rev() {
                let path = link.destination.join(folder);
                fs::remove_dir(&path).map_err(|error| {
                    FsxError::io(
                        FsxErrorKind::Io,
                        &path,
                        "cannot remove a copied folder",
                        error,
                    )
                })?;
            }
            fs::remove_dir(&link.destination).map_err(|error| {
                FsxError::io(
                    FsxErrorKind::Io,
                    &link.destination,
                    "cannot remove the copy",
                    error,
                )
            })?;
        }
    }
    Ok(Removal::Removed)
}

fn not_owned(link: &ManagedLink, why: &str) -> FsxError {
    FsxError::new(FsxErrorKind::NotOwned, &link.destination, why)
}

fn kind_word(kind: LinkKind) -> &'static str {
    match kind {
        LinkKind::Symlink => "symlink",
        LinkKind::Junction => "junction",
        LinkKind::Copy => "copy",
    }
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

fn native_descriptor(
    request: &LinkRequest<'_>,
    kind: LinkKind,
    target: PathBuf,
    identity: Identity,
) -> ManagedLink {
    ManagedLink {
        destination: request.destination.to_path_buf(),
        source: request.source.to_path_buf(),
        kind,
        target: Some(target),
        folders: Vec::new(),
        files: Vec::new(),
        identity,
    }
}

fn same_native_link(entry: &Entry, kind: LinkKind, target: &Path, source: &Path) -> bool {
    match (entry, kind) {
        (Entry::Symlink { target: found }, LinkKind::Symlink) => found == target,
        (Entry::Junction { target: found }, LinkKind::Junction) => same_folder(found, source),
        _ => false,
    }
}

fn same_folder(a: &Path, b: &Path) -> bool {
    match (fs::canonicalize(a), fs::canonicalize(b)) {
        (Ok(a), Ok(b)) => a == b,
        _ => false,
    }
}

fn verify_native(
    destination: &Path,
    kind: LinkKind,
    target: &Path,
    source: &Path,
) -> Result<Identity, FsxError> {
    let entry = inspect(destination)?;
    if !same_native_link(&entry, kind, target, source) {
        return Err(FsxError::new(
            FsxErrorKind::NotOwned,
            destination,
            "the new entry is not the requested link; it was left alone",
        ));
    }
    let identity = identity::capture(destination).map_err(|error| {
        FsxError::io(
            FsxErrorKind::LinkUnsupported,
            destination,
            "cannot retain the new link's identity",
            error,
        )
    })?;
    let resolved = fs::canonicalize(destination);
    let reaches_source = match (&resolved, fs::canonicalize(source)) {
        (Ok(found), Ok(expected)) => *found == expected,
        _ => false,
    };
    if reaches_source && identity::matches(destination, &identity) {
        return Ok(identity);
    }
    if !identity::matches(destination, &identity)
        || !same_native_link(&inspect(destination)?, kind, target, source)
    {
        return Err(FsxError::new(
            FsxErrorKind::NotOwned,
            destination,
            "another entry replaced the new link; it was left alone",
        ));
    }
    remove_link_entry(destination).map_err(|error| {
        FsxError::io(
            FsxErrorKind::LinkUnsupported,
            destination,
            "cannot remove the acquired unusable link",
            error,
        )
    })?;
    let what = format!(
        "the new {} does not resolve to the source folder",
        kind_word(kind)
    );
    Err(match resolved {
        // Keep the operating system's reason, such as a junction that cannot be traversed.
        Err(error) => FsxError::io(FsxErrorKind::LinkUnsupported, destination, &what, error),
        Ok(_) => FsxError::new(FsxErrorKind::LinkUnsupported, destination, what),
    })
}

#[cfg(windows)]
fn remove_link_entry(path: &Path) -> io::Result<()> {
    use std::os::windows::fs::FileTypeExt;
    // A junction or directory symlink is removed as a folder, which deletes only the link.
    if fs::symlink_metadata(path)?.file_type().is_symlink_dir() {
        fs::remove_dir(path)
    } else {
        fs::remove_file(path)
    }
}

#[cfg(not(windows))]
fn remove_link_entry(path: &Path) -> io::Result<()> {
    fs::remove_file(path)
}

/// The target text for a symlink in `folder` that reaches `source`, computed between the real locations of
/// both so the link resolves even when a parent folder is itself reached through a link.
fn relative_target(folder: &Path, source: &Path) -> Result<PathBuf, FsxError> {
    let from = fs::canonicalize(folder)
        .map_err(|error| FsxError::io(FsxErrorKind::Io, folder, "cannot resolve", error))?;
    let to = fs::canonicalize(source)
        .map_err(|error| FsxError::io(FsxErrorKind::Io, source, "cannot resolve", error))?;
    let from: Vec<Component<'_>> = from.components().collect();
    let to: Vec<Component<'_>> = to.components().collect();
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

/// Walks a real folder without following links: folders (parents first) and files, both relative to `root`.
/// Any link or special entry inside is [`FsxErrorKind::TreeHasLink`].
fn walk(root: &Path) -> Result<(Vec<PathBuf>, Vec<CopiedFile>), FsxError> {
    let mut folders = Vec::new();
    let mut files = Vec::new();
    let mut pending = vec![PathBuf::new()];
    while let Some(relative) = pending.pop() {
        let folder = root.join(&relative);
        let mut children: Vec<_> = fs::read_dir(&folder)
            .map_err(|error| FsxError::io(FsxErrorKind::Io, &folder, "cannot list", error))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| FsxError::io(FsxErrorKind::Io, &folder, "cannot list", error))?;
        children.sort_by_key(|child| child.file_name());
        let mut subfolders = Vec::new();
        for child in children {
            let child_relative = relative.join(child.file_name());
            let path = root.join(&child_relative);
            let metadata = fs::symlink_metadata(&path)
                .map_err(|error| FsxError::io(FsxErrorKind::Io, &path, "cannot inspect", error))?;
            let file_type = metadata.file_type();
            if file_type.is_symlink() {
                return Err(FsxError::new(
                    FsxErrorKind::TreeHasLink,
                    &path,
                    "is a link; links inside a copied folder are never followed",
                ));
            } else if file_type.is_dir() {
                folders.push(child_relative.clone());
                subfolders.push(child_relative);
            } else if file_type.is_file() {
                let bytes = fs::read(&path)
                    .map_err(|error| FsxError::io(FsxErrorKind::Io, &path, "cannot read", error))?;
                files.push(CopiedFile {
                    relative: child_relative,
                    fingerprint: Fingerprint::of(&bytes),
                    executable: is_executable(&metadata),
                });
            } else {
                return Err(FsxError::new(
                    FsxErrorKind::TreeHasLink,
                    &path,
                    "is neither a file nor a folder and cannot be copied",
                ));
            }
        }
        // Depth-first in name order, keeping parents before their children.
        pending.extend(subfolders.into_iter().rev());
    }
    Ok((folders, files))
}

#[cfg(unix)]
fn is_executable(metadata: &fs::Metadata) -> bool {
    use std::os::unix::fs::PermissionsExt;
    metadata.permissions().mode() & 0o111 != 0
}

#[cfg(not(unix))]
fn is_executable(_metadata: &fs::Metadata) -> bool {
    false
}

/// Copies the source tree into a hidden sibling folder and renames it into place, then checks the copy.
fn copy_fallback(
    request: &LinkRequest<'_>,
    reason: Option<String>,
) -> Result<LinkOutcome, FsxError> {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let source = request.source;
    let destination = request.destination;
    let (folders, files) = walk(source)?;
    let parent = destination.parent().unwrap_or(destination);
    let name = destination
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let staging = parent.join(format!(
        ".{name}.{}.{}.copy.tmp",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    copy_at(request, reason, &staging, folders, files)
}

fn copy_at(
    request: &LinkRequest<'_>,
    reason: Option<String>,
    staging: &Path,
    folders: Vec<PathBuf>,
    files: Vec<CopiedFile>,
) -> Result<LinkOutcome, FsxError> {
    let destination = request.destination;
    let mut stage = CopyStage::acquire(staging)?;
    let populated = (|| {
        for folder in &folders {
            stage.directory(folder)?;
        }
        for file in &files {
            stage.file(&request.source.join(&file.relative), &file.relative)?;
        }
        Ok(())
    })();
    if let Err(error) = populated {
        stage.cleanup()?;
        return Err(error);
    }
    if let Err(error) = stage.publish(destination) {
        stage.cleanup()?;
        let kind = if inspect(destination)? != Entry::Missing {
            FsxErrorKind::LinkExists
        } else {
            FsxErrorKind::Io
        };
        return Err(FsxError::io(
            kind,
            destination,
            "cannot move the copy into place",
            error,
        ));
    }
    let link = ManagedLink {
        destination: destination.to_path_buf(),
        source: request.source.to_path_buf(),
        kind: LinkKind::Copy,
        target: None,
        folders,
        files,
        identity: stage.identity.clone(),
    };
    let (found_folders, found_files) = walk(destination)?;
    let same_files = found_files.len() == link.files.len()
        && found_files
            .iter()
            .zip(&link.files)
            .all(|(found, recorded)| {
                found.relative == recorded.relative && found.fingerprint == recorded.fingerprint
            });
    if !identity::matches(destination, &link.identity)
        || found_folders != link.folders
        || !same_files
    {
        return Err(FsxError::new(
            FsxErrorKind::Unverified,
            destination,
            "copied, but the copy read back differs from the source",
        ));
    }
    Ok(LinkOutcome {
        link,
        created: true,
        fallback: Some(reason.unwrap_or_else(|| "a copy was requested".to_string())),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cleanup_ownership_copy_collisions_and_replacements_are_preserved() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let source = root.join("source");
        fs::create_dir(&source).unwrap();
        fs::write(source.join("keep.txt"), "source bytes").unwrap();
        let destination = root.join("destination");
        let request = LinkRequest {
            source: &source,
            destination: &destination,
            style: TargetStyle::Absolute,
            allow_copy: true,
        };
        let staging = root.join("staging");
        fs::create_dir(&staging).unwrap();
        fs::write(staging.join("user.txt"), "user bytes").unwrap();
        let (folders, files) = walk(&source).unwrap();
        assert!(copy_at(&request, None, &staging, folders, files).is_err());
        assert_eq!(fs::read(staging.join("user.txt")).unwrap(), b"user bytes");
        let acquired_path = root.join("acquired-stage");
        let acquired = CopyStage::acquire(&acquired_path).unwrap();
        let staging = acquired_path;
        fs::rename(&staging, root.join("original-staging")).unwrap();
        fs::create_dir(&staging).unwrap();
        fs::write(staging.join("replacement.txt"), "replacement bytes").unwrap();
        assert_eq!(
            acquired.cleanup().unwrap_err().kind(),
            FsxErrorKind::NotOwned
        );
        assert_eq!(
            fs::read(staging.join("replacement.txt")).unwrap(),
            b"replacement bytes"
        );

        let unexpected = root.join("unexpected-child-stage");
        let acquired = CopyStage::acquire(&unexpected).unwrap();
        fs::write(unexpected.join("user.txt"), "foreign child").unwrap();
        assert!(acquired.cleanup().is_err());
        assert_eq!(
            fs::read(unexpected.join("user.txt")).unwrap(),
            b"foreign child"
        );

        fs::create_dir(&destination).unwrap();
        let user_destination = identity::capture(&destination).unwrap();
        let (folders, files) = walk(&source).unwrap();
        let owned_stage = root.join("owned-stage");
        assert_eq!(
            copy_at(&request, None, &owned_stage, folders, files)
                .unwrap_err()
                .kind(),
            FsxErrorKind::LinkExists
        );
        assert!(identity::matches(&destination, &user_destination));
        assert!(!owned_stage.exists());

        let (folders, files) = walk(&source).unwrap();
        fs::remove_file(source.join("keep.txt")).unwrap();
        let failed_stage = root.join("failed-stage");
        assert!(copy_at(&request, None, &failed_stage, folders, files).is_err());
        assert!(!failed_stage.exists());
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
            target: Some(PathBuf::from("s")),
            folders: Vec::new(),
            files: Vec::new(),
            identity: identity::capture(tempfile::NamedTempFile::new().unwrap().path()).unwrap(),
        };
        let store = PathBuf::from(if cfg!(windows) {
            r"C:\x\store"
        } else {
            "/x/store"
        });
        let error = remove(&link, &[store.as_path()]).unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::NotOwned);
    }
}
