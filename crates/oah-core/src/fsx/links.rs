// 관리 링크: 심볼릭 링크·윈도 정션·복사 대체를 만들고 살피고 지우며, 소유 증거가 파일 시스템과 맞을 때만 지운다.
//! Managed links from an agent's folder to a store folder.
//!
//! A link is made natively (a symlink on macOS and Linux, a directory junction on Windows, which needs no
//! elevation) or, when the caller allows it and the native link cannot be made, as a reported copy. The native
//! attempt runs inside a private folder that the call makes exclusively next to the destination, so whatever a
//! failed attempt leaves behind is the call's own to clean up and never occupies the destination. A verified
//! link, like a finished copy, is renamed into place without replacing anything.
//!
//! Creating returns a [`ManagedLink`] record: plain data the caller keeps in its own state, also across
//! processes, as ownership evidence. [`remove`] deletes an entry only when the record points into a known store
//! and still matches what is on disk, and it never deletes the link's target. Anything else at a destination is
//! left exactly as it is.

use super::atomic::Fingerprint;
use super::error::{FsxError, FsxErrorKind};
pub use super::identity::EntryIdentity;
use super::identity::{self, EntryId, Now};
use super::staging::{self, Attempted, Stage};
use std::ffi::{OsStr, OsString};
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
///
/// A plain record with no hidden state: the caller stores these fields (a [`Fingerprint`] round-trips through
/// [`Fingerprint::to_hex`] and [`Fingerprint::from_hex`]) and may rebuild the record in a later process. It
/// authorizes nothing by itself; [`remove`] acts only while the source lies in a known store and the disk still
/// matches the record, including the original entry identity. Restoring a record never captures a new identity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedLink {
    pub destination: PathBuf,
    pub source: PathBuf,
    pub kind: LinkKind,
    /// Identity captured from the created entry, retained unchanged across restoration. Missing or unsupported
    /// evidence refuses removal; never substitute the identity of whatever is at the path when loading.
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
    /// [`create_with`] passes a path inside a private folder it made for this attempt, never the public
    /// destination, and `target` exactly as it must read at the public destination: a relative target is
    /// relative to the destination's folder and is never rewritten for the private folder.
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
/// An occupied destination is never adopted or replaced: it is [`FsxErrorKind::LinkExists`]. The linker runs in
/// a private folder this call makes exclusively; everything the attempt leaves there is removed again, so a
/// failed or unusable native link never occupies the destination. When the native link cannot be made or used,
/// the copy fallback runs only if the request allows it and that cleanup succeeded, and the outcome then names
/// the reason. A cleanup that cannot finish is reported with the original failure and no fallback.
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
    let name = destination
        .file_name()
        .ok_or_else(|| FsxError::new(FsxErrorKind::Io, destination, "has no file name"))?;
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
        return copy_fallback(request, parent, name, None);
    }
    match native(request, linker, kind, &target, parent, name) {
        Ok(link) => Ok(LinkOutcome {
            link,
            created: true,
            fallback: None,
        }),
        Err(Failure::Unsupported { reason, .. }) if request.allow_copy => {
            copy_fallback(request, parent, name, Some(reason))
        }
        Err(Failure::Unsupported { error, .. } | Failure::Fatal(error)) => Err(error),
    }
}

/// Why a native attempt gave no link. Either way the attempt's own entries were cleaned up first, or the
/// error carries the cleanup that could not finish.
enum Failure {
    /// The link cannot be made or used here, and the attempt left nothing behind; a copy may stand in.
    /// `reason` is what the copy reports.
    Unsupported { error: FsxError, reason: String },
    /// Anything else, reported as it is and never replaced by a copy.
    Fatal(FsxError),
}

/// Attempt numbers of this process; with the process id they make the private names unique.
static ATTEMPTS: AtomicU64 = AtomicU64::new(0);

/// Makes the native link in a private folder, moves it next to the destination where it resolves exactly as it
/// will once published, checks it, renames it into place without replacing anything and reads it back.
fn native(
    request: &LinkRequest<'_>,
    linker: &dyn NativeLinker,
    kind: LinkKind,
    target: &Path,
    parent: &Path,
    name: &OsStr,
) -> Result<ManagedLink, Failure> {
    let source = request.source;
    let destination = request.destination;
    let number = ATTEMPTS.fetch_add(1, Ordering::Relaxed);
    let mut attempt =
        Stage::folder(&private_name(parent, name, number, "attempt")).map_err(Failure::Fatal)?;
    let entry = attempt.path().join(name);
    let made = linker.link(source, &entry, target);
    if let Err(error) = &made
        && error.kind() == io::ErrorKind::AlreadyExists
    {
        // The private name was taken before the attempt could make anything, so that entry is not its own.
        let primary = FsxError::new(
            FsxErrorKind::NotOwned,
            &entry,
            "something else appeared in OAH's private attempt folder; it was left alone",
        );
        return Err(Failure::Fatal(attempt.discard_after(primary)));
    }
    // Record what the attempt left before anything else can fail.
    let attempted = attempt
        .record_attempt(&entry)
        .map_err(|error| Failure::Fatal(attempt.discard_after(error)))?;
    if let Err(error) = made {
        let reason = format!("{} unavailable: {error}", kind_word(kind));
        let primary = FsxError::io(
            FsxErrorKind::LinkUnsupported,
            destination,
            &format!("cannot make a {}", kind_word(kind)),
            error,
        );
        return Err(unsupported(&attempt, primary, Some(reason)));
    }
    let made_link = attempted == Attempted::Link
        && same_native_link(
            &inspect(&entry).map_err(|error| Failure::Fatal(attempt.discard_after(error)))?,
            kind,
            target,
            source,
        );
    if !made_link {
        let primary = FsxError::new(
            FsxErrorKind::LinkUnsupported,
            destination,
            format!(
                "the {} attempt did not make a link storing the requested target",
                kind_word(kind)
            ),
        );
        return Err(unsupported(&attempt, primary, None));
    }
    let staged = attempt
        .take_link(&private_name(parent, name, number, "link"))
        .map_err(Failure::Fatal)?;
    let expected = fs::canonicalize(source).map_err(|error| {
        let primary = FsxError::io(
            FsxErrorKind::Io,
            source,
            "cannot resolve the source folder",
            error,
        );
        Failure::Fatal(staged.discard_after(primary))
    })?;
    match fs::canonicalize(staged.path()) {
        Ok(found) if found == expected => {}
        resolved => {
            let what = format!(
                "the new {} does not resolve to the source folder",
                kind_word(kind)
            );
            let primary = match resolved {
                // Keep the operating system's reason, such as a junction that cannot be traversed.
                Err(error) => {
                    FsxError::io(FsxErrorKind::LinkUnsupported, destination, &what, error)
                }
                Ok(_) => FsxError::new(FsxErrorKind::LinkUnsupported, destination, what),
            };
            return Err(unsupported(&staged, primary, None));
        }
    }
    let id = staged.publish(destination).map_err(Failure::Fatal)?;
    read_back_native(destination, id, kind, target, source).map_err(Failure::Fatal)?;
    Ok(ManagedLink {
        destination: destination.to_path_buf(),
        source: source.to_path_buf(),
        kind,
        identity: Some(id.evidence()),
        target: Some(target.to_path_buf()),
        folders: Vec::new(),
        files: Vec::new(),
    })
}

/// Cleans up an attempt that gave no usable link. A copy may stand in only when nothing was left behind.
fn unsupported(stage: &Stage, error: FsxError, reason: Option<String>) -> Failure {
    match stage.discard() {
        Ok(()) => Failure::Unsupported {
            reason: reason.unwrap_or_else(|| error.detail().to_string()),
            error,
        },
        Err(cleanup) => Failure::Fatal(error.with_cleanup(cleanup)),
    }
}

/// The published destination must still be the entry this call renamed there and still store the requested
/// target. Anything else means another writer replaced it, and that entry is left alone.
fn read_back_native(
    destination: &Path,
    id: EntryId,
    kind: LinkKind,
    target: &Path,
    source: &Path,
) -> Result<(), FsxError> {
    let now = identity::now(destination, id).map_err(|error| {
        FsxError::io(
            FsxErrorKind::Io,
            destination,
            "cannot read back the new link",
            error,
        )
    })?;
    if now == Now::Same && same_native_link(&inspect(destination)?, kind, target, source) {
        Ok(())
    } else {
        Err(FsxError::new(
            FsxErrorKind::NotOwned,
            destination,
            "another entry replaced the new link; it was left alone",
        ))
    }
}

/// A hidden name next to the destination that only this process and this number use.
fn private_name(parent: &Path, name: &OsStr, number: u64, role: &str) -> PathBuf {
    let mut text = OsString::from(".");
    text.push(name);
    text.push(format!(".{}.{number}.{role}.tmp", std::process::id()));
    parent.join(text)
}

/// Whether a removal deleted something.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Removal {
    Removed,
    /// Nothing was at the destination any more.
    AlreadyGone,
}

/// Removes a managed link, checking its record first.
///
/// A destination ending in a separator or a `.` component is refused before lookup.
/// The record's source must lie inside one of `stores`, and the entry at its destination must still be the
/// original entry identity plus the same symlink text, junction target, or copy folder/file inventory.
/// Otherwise nothing is touched and the result is [`FsxErrorKind::NotOwned`]. Removing a link deletes only the
/// link; removing a copy deletes only the recorded files and folders.
///
/// The record may come from an earlier process. A same-target replacement with a different identity is refused.
/// Entry-number reuse after deletion and concurrent check-to-path-use races remain limitations of plain evidence.
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
        _ => {}
    }
    let source =
        lexical(&link.source).ok_or_else(|| not_owned(link, "its source is not a plain path"))?;
    let in_store = stores.iter().any(|store| match lexical(store) {
        Some(store) => source.starts_with(&store),
        None => false,
    });
    if !in_store {
        return Err(not_owned(link, "its source is outside the known stores"));
    }
    let entry = inspect(destination)?;
    if entry == Entry::Missing {
        return Ok(Removal::AlreadyGone);
    }
    require_record_identity(link)?;
    match link.kind {
        LinkKind::Symlink | LinkKind::Junction => {
            let target = link
                .target
                .as_ref()
                .ok_or_else(|| not_owned(link, "the record has no link target"))?;
            // The source's store decides ownership only when the recorded target really points at that source.
            if !target_points_at(destination, target, &link.source) {
                return Err(not_owned(
                    link,
                    "the record's link target does not point at its source",
                ));
            }
            if !same_native_link(&entry, link.kind, target, &link.source) {
                return Err(not_owned(link, "the entry there is not this link"));
            }
            require_record_identity(link)?;
            staging::remove_link(destination).map_err(|error| {
                FsxError::io(
                    FsxErrorKind::Io,
                    destination,
                    "cannot remove the link",
                    error,
                )
            })?;
        }
        LinkKind::Copy => {
            let plain = |path: &Path| {
                path.components().next().is_some()
                    && path
                        .components()
                        .all(|component| matches!(component, Component::Normal(_)))
            };
            let paths_plain = link.folders.iter().all(|folder| plain(folder))
                && link.files.iter().all(|file| plain(&file.relative));
            if link.target.is_some() || !paths_plain {
                return Err(not_owned(link, "the record is not a copy record"));
            }
            if entry != Entry::Directory {
                return Err(not_owned(link, "the entry there is not a folder"));
            }
            let (folders, files) = match walk(destination) {
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
                require_record_identity(link)?;
                let path = destination.join(&file.relative);
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
                require_record_identity(link)?;
                let path = destination.join(folder);
                fs::remove_dir(&path).map_err(|error| {
                    FsxError::io(
                        FsxErrorKind::Io,
                        &path,
                        "cannot remove a copied folder",
                        error,
                    )
                })?;
            }
            require_record_identity(link)?;
            fs::remove_dir(destination).map_err(|error| {
                FsxError::io(
                    FsxErrorKind::Io,
                    destination,
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

/// Historical evidence is supplied by the record, never obtained by adopting the current entry.
fn require_record_identity(link: &ManagedLink) -> Result<(), FsxError> {
    let id = link
        .identity
        .and_then(EntryIdentity::entry_id)
        .ok_or_else(|| not_owned(link, "the record has no supported original entry identity"))?;
    match identity::now(&link.destination, id).map_err(|error| {
        FsxError::io(
            FsxErrorKind::Io,
            &link.destination,
            "cannot read the managed entry identity",
            error,
        )
    })? {
        Now::Same => Ok(()),
        Now::Other | Now::Missing => Err(not_owned(
            link,
            "another entry replaced the recorded entry; it was left alone",
        )),
    }
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

/// Whether a link at `destination` storing `target` points at `source`. A relative target is read from the real
/// folder of the link, as [`relative_target`] computed it, so a link whose source is gone is still recognized.
fn target_points_at(destination: &Path, target: &Path, source: &Path) -> bool {
    let pointed = if target.is_absolute() {
        lexical(target)
    } else {
        destination
            .parent()
            .and_then(|folder| fs::canonicalize(folder).ok())
            .and_then(|folder| resolve_dots(&folder.join(target)))
    };
    pointed.is_some_and(|pointed| {
        lexical(source).is_some_and(|source| source == pointed)
            || fs::canonicalize(source).is_ok_and(|source| source == pointed)
    })
}

/// The path with `.` removed and each `..` taking away the component before it; `None` when a `..` would climb
/// above the root.
fn resolve_dots(path: &Path) -> Option<PathBuf> {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if !out.pop() {
                    return None;
                }
            }
            other => out.push(other.as_os_str()),
        }
    }
    Some(out)
}

fn same_native_link(entry: &Entry, kind: LinkKind, target: &Path, source: &Path) -> bool {
    match (entry, kind) {
        (Entry::Symlink { target: found }, LinkKind::Symlink) => found == target,
        // The stored target itself, or the same folder when both still resolve.
        (Entry::Junction { target: found }, LinkKind::Junction) => {
            found == target || same_folder(found, source)
        }
        _ => false,
    }
}

fn same_folder(a: &Path, b: &Path) -> bool {
    match (fs::canonicalize(a), fs::canonicalize(b)) {
        (Ok(a), Ok(b)) => a == b,
        _ => false,
    }
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

/// Copy numbers of this process, counted apart from native attempts.
static COPIES: AtomicU64 = AtomicU64::new(0);

/// Copies the source tree into a hidden sibling folder and renames it into place, then checks the copy.
fn copy_fallback(
    request: &LinkRequest<'_>,
    parent: &Path,
    name: &OsStr,
    reason: Option<String>,
) -> Result<LinkOutcome, FsxError> {
    let (folders, files) = walk(request.source)?;
    let number = COPIES.fetch_add(1, Ordering::Relaxed);
    copy_at(
        request,
        reason,
        &private_name(parent, name, number, "copy"),
        folders,
        files,
    )
}

fn copy_at(
    request: &LinkRequest<'_>,
    reason: Option<String>,
    staging: &Path,
    folders: Vec<PathBuf>,
    files: Vec<CopiedFile>,
) -> Result<LinkOutcome, FsxError> {
    let destination = request.destination;
    let mut stage = Stage::folder(staging)?;
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
        return Err(stage.discard_after(error));
    }
    let id = stage.publish(destination)?;
    let link = ManagedLink {
        destination: destination.to_path_buf(),
        source: request.source.to_path_buf(),
        kind: LinkKind::Copy,
        identity: Some(id.evidence()),
        target: None,
        folders,
        files,
    };
    read_back_copy(&link)?;
    Ok(LinkOutcome {
        link,
        created: true,
        fallback: Some(reason.unwrap_or_else(|| "a copy was requested".to_string())),
    })
}

fn read_back_copy(link: &ManagedLink) -> Result<(), FsxError> {
    let destination = &link.destination;
    // A trailing separator or dot can make a no-follow lookup traverse the final link.
    let root = destination.components().as_path();
    let unreadable = |error| {
        FsxError::io(
            FsxErrorKind::Io,
            destination,
            "copied, but the copy cannot be read back",
            error,
        )
    };
    let different = || {
        FsxError::new(
            FsxErrorKind::Unverified,
            destination,
            "copied, but the copy read back differs from the source",
        )
    };
    if !copy_root_is_directory(root).map_err(unreadable)? {
        return Err(different());
    }
    let id = link
        .identity
        .and_then(EntryIdentity::entry_id)
        .ok_or_else(different)?;
    let (found_folders, found_files) = walk(root)?;
    let same_files = found_files.len() == link.files.len()
        && found_files
            .iter()
            .zip(&link.files)
            .all(|(found, recorded)| {
                found.relative == recorded.relative && found.fingerprint == recorded.fingerprint
            });
    let same_entry = identity::now(root, id).map_err(unreadable)? == Now::Same;
    if !same_entry || found_folders != link.folders || !same_files {
        return Err(different());
    }
    match copy_root_is_directory(root) {
        Ok(true) => Ok(()),
        Ok(false) => Err(different()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Err(different()),
        Err(error) => Err(unreadable(error)),
    }
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

    /// Every name in `folder`, sorted.
    fn names(folder: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(folder)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    fn folder_with_source() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let source = root.join("source");
        fs::create_dir(&source).unwrap();
        fs::write(source.join("keep.txt"), "source bytes").unwrap();
        (dir, root, source)
    }

    #[cfg(any(unix, windows))]
    #[test]
    fn copy_read_back_requires_a_real_root() {
        let (_dir, root, source) = folder_with_source();
        let destination = root.join("destination");
        let request = LinkRequest {
            source: &source,
            destination: &destination,
            style: TargetStyle::Absolute,
            allow_copy: true,
        };
        let (folders, files) = walk(&source).unwrap();
        let copy = copy_at(&request, None, &root.join("staging"), folders, files).unwrap();
        assert_eq!(copy.link.kind, LinkKind::Copy);

        let published = root.join("published-directory");
        fs::rename(&destination, &published).unwrap();
        SystemLinker
            .link(&published, &destination, &published)
            .unwrap();
        // Supply a matching number deterministically while keeping the real copy's inventory.
        let id = EntryId::of(&destination).unwrap();
        let mut link = ManagedLink {
            identity: Some(id.evidence()),
            ..copy.link
        };
        let error = read_back_copy(&link).unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::Unverified);

        let child = published.join("foreign-link");
        SystemLinker.link(&source, &child, &source).unwrap();
        for spelling in [destination.clone(), destination.join("")] {
            link.destination = spelling;
            let error = read_back_copy(&link).unwrap_err();
            assert_eq!(
                error.kind(),
                FsxErrorKind::Unverified,
                "the root must be rejected before its children are read"
            );
        }
        assert_eq!(identity::now(&destination, id).unwrap(), Now::Same);
        assert_eq!(
            fs::read(published.join("keep.txt")).unwrap(),
            b"source bytes"
        );
    }

    #[test]
    fn cleanup_ownership_copy_collisions_and_replacements_are_preserved() {
        let (_dir, root, source) = folder_with_source();
        let destination = root.join("destination");
        let request = LinkRequest {
            source: &source,
            destination: &destination,
            style: TargetStyle::Absolute,
            allow_copy: true,
        };
        // A colliding private name is never used or cleaned.
        let staging = root.join("staging");
        fs::create_dir(&staging).unwrap();
        fs::write(staging.join("user.txt"), "user bytes").unwrap();
        let (folders, files) = walk(&source).unwrap();
        assert!(copy_at(&request, None, &staging, folders, files).is_err());
        assert_eq!(fs::read(staging.join("user.txt")).unwrap(), b"user bytes");

        // A stage whose root another writer replaced removes nothing below that path.
        let acquired_path = root.join("acquired-stage");
        let acquired = Stage::folder(&acquired_path).unwrap();
        fs::rename(&acquired_path, root.join("original-staging")).unwrap();
        fs::create_dir(&acquired_path).unwrap();
        fs::write(acquired_path.join("replacement.txt"), "replacement bytes").unwrap();
        assert_eq!(
            acquired.discard().unwrap_err().kind(),
            FsxErrorKind::NotOwned
        );
        assert_eq!(
            fs::read(acquired_path.join("replacement.txt")).unwrap(),
            b"replacement bytes"
        );

        // A foreign child keeps the stage folder in place.
        let unexpected = root.join("unexpected-child-stage");
        let acquired = Stage::folder(&unexpected).unwrap();
        fs::write(unexpected.join("user.txt"), "foreign child").unwrap();
        assert!(acquired.discard().is_err());
        assert_eq!(
            fs::read(unexpected.join("user.txt")).unwrap(),
            b"foreign child"
        );

        // An occupied destination is refused and the stage is cleaned.
        fs::create_dir(&destination).unwrap();
        let user_destination = EntryId::of(&destination).unwrap();
        let (folders, files) = walk(&source).unwrap();
        let owned_stage = root.join("owned-stage");
        assert_eq!(
            copy_at(&request, None, &owned_stage, folders, files)
                .unwrap_err()
                .kind(),
            FsxErrorKind::LinkExists
        );
        assert_eq!(
            identity::now(&destination, user_destination).unwrap(),
            Now::Same
        );
        assert!(!owned_stage.exists());
        fs::remove_dir(&destination).unwrap();

        // A failed copy cleans its stage.
        let (folders, files) = walk(&source).unwrap();
        fs::remove_file(source.join("keep.txt")).unwrap();
        let failed_stage = root.join("failed-stage");
        assert!(copy_at(&request, None, &failed_stage, folders, files).is_err());
        assert!(!failed_stage.exists());
    }

    #[test]
    fn cleanup_ownership_keeps_the_primary_failure_when_cleanup_cannot_finish() {
        let (_dir, root, source) = folder_with_source();
        let path = root.join("stage");
        let mut stage = Stage::folder(&path).unwrap();
        stage.directory(Path::new("made")).unwrap();
        fs::write(path.join("foreign.txt"), "foreign bytes").unwrap();
        let primary = stage
            .file(&source.join("missing.txt"), Path::new("copied.txt"))
            .unwrap_err();
        let error = stage.discard_after(primary);
        assert_eq!(error.kind(), FsxErrorKind::Io);
        assert_eq!(
            error.io_error().map(io::Error::kind),
            Some(io::ErrorKind::NotFound),
            "the primary cause survives"
        );
        let cleanup = error
            .cleanup_error()
            .expect("the cleanup failure is attached");
        assert_eq!(cleanup.path(), path.as_path());
        assert!(error.to_string().contains("cleanup also failed"), "{error}");
        assert!(!path.join("made").exists(), "its own folder was removed");
        assert_eq!(
            fs::read(path.join("foreign.txt")).unwrap(),
            b"foreign bytes"
        );
    }

    #[test]
    fn cleanup_ownership_attempt_folder_cleans_only_its_own_entries() {
        let (_dir, root, source) = folder_with_source();
        let name = OsStr::new("skill");

        // A half-made junction (a plain folder) left by a failed attempt is the attempt's own.
        let folder = private_name(&root, name, 0, "attempt");
        let mut attempt = Stage::folder(&folder).unwrap();
        fs::create_dir(folder.join(name)).unwrap();
        assert_eq!(
            attempt.record_attempt(&folder.join(name)).unwrap(),
            Attempted::Other
        );
        attempt.discard().unwrap();
        assert_eq!(names(&root), ["source"]);

        // A link is moved next to the destination and the emptied folder goes.
        let folder = private_name(&root, name, 1, "attempt");
        let mut attempt = Stage::folder(&folder).unwrap();
        SystemLinker
            .link(&source, &folder.join(name), &source)
            .unwrap();
        assert_eq!(
            attempt.record_attempt(&folder.join(name)).unwrap(),
            Attempted::Link
        );
        let staged_path = private_name(&root, name, 1, "link");
        let staged = attempt.take_link(&staged_path).unwrap();
        assert!(!folder.exists());
        assert_eq!(fs::canonicalize(&staged_path).unwrap(), source);
        staged.discard().unwrap();
        assert_eq!(names(&root), ["source"]);
        assert_eq!(fs::read(source.join("keep.txt")).unwrap(), b"source bytes");

        // Something else inside the private folder stays, and so does the folder.
        let folder = private_name(&root, name, 2, "attempt");
        let mut attempt = Stage::folder(&folder).unwrap();
        fs::write(folder.join("other.txt"), "foreign bytes").unwrap();
        assert_eq!(
            attempt.record_attempt(&folder.join(name)).unwrap(),
            Attempted::Nothing
        );
        assert!(attempt.discard().is_err());
        assert_eq!(
            fs::read(folder.join("other.txt")).unwrap(),
            b"foreign bytes"
        );

        // A replaced attempt folder is left exactly as the other writer made it.
        let folder = private_name(&root, name, 3, "attempt");
        let mut attempt = Stage::folder(&folder).unwrap();
        fs::create_dir(folder.join(name)).unwrap();
        attempt.record_attempt(&folder.join(name)).unwrap();
        fs::rename(&folder, root.join("moved-attempt")).unwrap();
        fs::create_dir_all(folder.join(name)).unwrap();
        fs::write(folder.join(name).join("theirs.txt"), "their bytes").unwrap();
        assert_eq!(
            attempt.discard().unwrap_err().kind(),
            FsxErrorKind::NotOwned
        );
        assert_eq!(
            fs::read(folder.join(name).join("theirs.txt")).unwrap(),
            b"their bytes"
        );

        // A staged link another writer replaced is never published or removed.
        let folder = private_name(&root, name, 4, "attempt");
        let mut attempt = Stage::folder(&folder).unwrap();
        SystemLinker
            .link(&source, &folder.join(name), &source)
            .unwrap();
        attempt.record_attempt(&folder.join(name)).unwrap();
        let staged_path = private_name(&root, name, 4, "link");
        let staged = attempt.take_link(&staged_path).unwrap();
        let elsewhere = root.join("elsewhere");
        fs::create_dir(&elsewhere).unwrap();
        staging::remove_link(&staged_path).unwrap();
        SystemLinker
            .link(&elsewhere, &staged_path, &elsewhere)
            .unwrap();
        let destination = root.join("skill");
        assert_eq!(
            staged.publish(&destination).unwrap_err().kind(),
            FsxErrorKind::NotOwned
        );
        assert_eq!(inspect(&destination).unwrap(), Entry::Missing);
        assert_eq!(fs::canonicalize(&staged_path).unwrap(), elsewhere);
    }

    #[test]
    fn cleanup_ownership_published_link_replacement_is_left_alone() {
        let (_dir, root, source) = folder_with_source();
        let name = OsStr::new("skill");
        let kind = SystemLinker.kind();
        let elsewhere = root.join("elsewhere");
        fs::create_dir(&elsewhere).unwrap();
        for (number, replacement) in [(0, &elsewhere), (1, &source)] {
            let destination = root.join(format!("skill-{number}"));
            let folder = private_name(&root, name, number, "attempt");
            let mut attempt = Stage::folder(&folder).unwrap();
            SystemLinker
                .link(&source, &folder.join(name), &source)
                .unwrap();
            attempt.record_attempt(&folder.join(name)).unwrap();
            let staged = attempt
                .take_link(&private_name(&root, name, number, "link"))
                .unwrap();
            let id = staged.publish(&destination).unwrap();
            // Another writer swaps the published link before it is read back, even for an identical one.
            fs::rename(&destination, root.join(format!("ours-{number}"))).unwrap();
            SystemLinker
                .link(replacement, &destination, replacement)
                .unwrap();
            let error = read_back_native(&destination, id, kind, &source, &source).unwrap_err();
            assert_eq!(error.kind(), FsxErrorKind::NotOwned);
            assert_eq!(fs::canonicalize(&destination).unwrap(), *replacement);
        }
        assert_eq!(fs::read(source.join("keep.txt")).unwrap(), b"source bytes");
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
