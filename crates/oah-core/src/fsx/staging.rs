// 임시 소유자: 이 작업이 배타적으로 만든 비공개 항목과 그 신원 값만 기록하고, 실패하면 기록한 항목만 역순으로 지운다.
//! Private temporary owners for link and copy creation.
//!
//! A [`Stage`] starts from one entry this operation made exclusively under a private name next to the
//! destination: a folder (a copy being filled, or the folder a native-link attempt runs in) or a native link that
//! was moved out of such a folder. Every entry it makes is recorded before the next step that can fail, with a
//! plain identity value read right after making it, so no file stays open. On Unix the record also keeps the
//! entry's kind and, for a link, its stored text: a file system such as ext4 hands a deleted entry's number to the
//! next entry made, so a number alone never shows that an entry is still this operation's. Cleanup removes only
//! recorded entries that are still the ones this operation made, one at a time and never recursively: a
//! collision, a replacement or anything another writer put inside is left exactly as it is, and the failure that
//! started the cleanup is kept. Publishing renames the root to the destination without replacing anything there.

use super::error::{FsxError, FsxErrorKind};
use super::identity::{self, EntryId, MoveProof, Now};
#[cfg(unix)]
use std::ffi::OsString;
use std::fs::{self, File};
use std::io;
use std::path::{Path, PathBuf};

/// How a recorded entry is removed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Shape {
    Folder,
    File,
    Link,
}

#[derive(Debug)]
struct Acquired {
    path: PathBuf,
    /// Read right after the entry was made; `None` when that read failed, and then the entry is never removed.
    id: Option<EntryId>,
    shape: Shape,
    /// The text a recorded link stores, read right after its number and before that number is kept.
    #[cfg(unix)]
    link_text: Option<OsString>,
}

/// What a native-link attempt left at its private entry name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Attempted {
    Nothing,
    /// A symlink or junction, recorded as this attempt's own.
    Link,
    /// Anything else. A folder (a junction whose reparse data was never set) is recorded so cleanup can remove
    /// it while it is empty; any other entry is not something a linker makes and stays where it is.
    Other,
}

/// The entries one operation made, in the order it made them; the first is the root.
#[derive(Debug)]
pub(super) struct Stage {
    entries: Vec<Acquired>,
}

impl Stage {
    /// Makes the root folder exclusively at a private name. An entry already there is never used or removed.
    pub(super) fn folder(path: &Path) -> Result<Stage, FsxError> {
        fs::create_dir(path).map_err(|error| {
            io_error(
                path,
                "cannot exclusively create OAH's private folder",
                error,
            )
        })?;
        let mut stage = Stage {
            entries: Vec::new(),
        };
        stage.record_path(path.to_path_buf(), Shape::Folder)?;
        Ok(stage)
    }

    /// The root's current path.
    pub(super) fn path(&self) -> &Path {
        &self.entries[0].path
    }

    /// Records an entry made a moment ago, then reads its identity. The record exists even if that read fails.
    fn record_path(&mut self, path: PathBuf, shape: Shape) -> Result<(), FsxError> {
        let id = EntryId::of(&path);
        self.record(path, shape, id)
    }

    /// Records a file made a moment ago; its identity is read through the open file, opening nothing new.
    fn record_file(&mut self, path: PathBuf, file: &File) -> Result<(), FsxError> {
        let id = EntryId::of_file(file);
        self.record(path, Shape::File, id)
    }

    fn record(
        &mut self,
        path: PathBuf,
        shape: Shape,
        id: io::Result<EntryId>,
    ) -> Result<(), FsxError> {
        self.entries.push(Acquired {
            path,
            id: None,
            shape,
            #[cfg(unix)]
            link_text: None,
        });
        let last = self.entries.len() - 1;
        #[cfg(unix)]
        let id = id.and_then(|id| {
            if shape == Shape::Link {
                let text = fs::read_link(&self.entries[last].path)?;
                self.entries[last].link_text = Some(text.into_os_string());
            }
            Ok(id)
        });
        match id {
            Ok(id) => {
                self.entries[last].id = Some(id);
                Ok(())
            }
            Err(error) => Err(io_error(
                &self.entries[last].path,
                "was made, but its identity cannot be read, so it will be left in place",
                error,
            )),
        }
    }

    /// Whether a recorded entry is still the one this operation made. On Unix a matching number counts only
    /// together with the recorded kind and link text. An unreadable identity is an error.
    fn now(entry: &Acquired) -> Result<Now, FsxError> {
        let id = entry.id.ok_or_else(|| {
            FsxError::new(
                FsxErrorKind::Io,
                &entry.path,
                "its identity was never read, so it was left in place",
            )
        })?;
        let found = identity::now(&entry.path, id);
        #[cfg(unix)]
        let found = match found {
            Ok(Now::Same) => made_now(entry, &entry.path),
            other => other,
        };
        found.map_err(|error| {
            io_error(
                &entry.path,
                "cannot read the identity of an entry OAH made",
                error,
            )
        })
    }

    /// Nothing is made below the root, removed or published unless the root is still this operation's entry.
    fn require_root(&self) -> Result<EntryId, FsxError> {
        let root = &self.entries[0];
        match Stage::now(root)? {
            Now::Same => Ok(root.id.expect("a matching entry has an identity")),
            Now::Other | Now::Missing => Err(replaced(&root.path)),
        }
    }

    /// Makes one copied folder below the root.
    pub(super) fn directory(&mut self, relative: &Path) -> Result<(), FsxError> {
        self.require_root()?;
        let path = self.path().join(relative);
        fs::create_dir(&path)
            .map_err(|error| io_error(&path, "cannot exclusively create a copied folder", error))?;
        self.record_path(path, Shape::Folder)
    }

    /// Copies one regular file below the root without following links. At most two files are open at once, and
    /// both are closed before this returns.
    pub(super) fn file(&mut self, source: &Path, relative: &Path) -> Result<(), FsxError> {
        self.require_root()?;
        let mut input = open_source(source)?;
        let metadata = input
            .metadata()
            .map_err(|error| io_error(source, "cannot inspect copy source", error))?;
        if !metadata.is_file() {
            return Err(FsxError::new(
                FsxErrorKind::TreeHasLink,
                source,
                "the copy source is no longer a regular file",
            ));
        }
        let path = self.path().join(relative);
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|error| io_error(&path, "cannot exclusively create a copied file", error))?;
        self.record_file(path.clone(), &output)?;
        io::copy(&mut input, &mut output)
            .and_then(|_| output.set_permissions(metadata.permissions()))
            .map_err(|error| io_error(&path, "cannot copy", error))?;
        Ok(())
    }

    /// Records what a native linker left at `entry`, a name inside this stage's root folder that only this
    /// attempt uses. Whatever is there was made by the attempt, so it is this operation's own.
    pub(super) fn record_attempt(&mut self, entry: &Path) -> Result<Attempted, FsxError> {
        self.require_root()?;
        let metadata = match fs::symlink_metadata(entry) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Attempted::Nothing),
            Err(error) => return Err(io_error(entry, "cannot inspect the native attempt", error)),
        };
        let file_type = metadata.file_type();
        if file_type.is_symlink() {
            self.record_path(entry.to_path_buf(), Shape::Link)?;
            Ok(Attempted::Link)
        } else {
            if file_type.is_dir() {
                self.record_path(entry.to_path_buf(), Shape::Folder)?;
            }
            Ok(Attempted::Other)
        }
    }

    /// Moves this attempt's recorded link out of its private folder to `to`, a private name next to the
    /// destination, then removes the emptied folder. Returns a stage that holds just the link.
    pub(super) fn take_link(mut self, to: &Path) -> Result<Stage, FsxError> {
        let is_link = |entry: &Acquired| entry.shape == Shape::Link;
        if self.entries.len() != 2 || !is_link(&self.entries[1]) {
            let primary = FsxError::new(
                FsxErrorKind::Io,
                self.path(),
                "the native attempt holds no single link to move",
            );
            return Err(self.discard_after(primary));
        }
        self.require_root()?;
        if Stage::now(&self.entries[1])? != Now::Same {
            let primary = replaced(&self.entries[1].path);
            return Err(self.discard_after(primary));
        }
        let proof = move_proof(&self.entries[1]).map_err(|error| self.discard_after(error))?;
        if let Err(error) = identity::rename_new(&self.entries[1].path, to) {
            let primary = io_error(to, "cannot move the new link to its private name", error);
            return Err(self.discard_after(primary));
        }
        self.entries[1].path = to.to_path_buf();
        self.entries[1].id = None;
        let id = moved_id(&proof, &self.entries[1]).map_err(|error| self.discard_after(error))?;
        self.entries[1].id = Some(id);
        let link = self.entries.pop().expect("checked above");
        let taken = Stage {
            entries: vec![Acquired {
                path: to.to_path_buf(),
                ..link
            }],
        };
        match self.discard() {
            Ok(()) => Ok(taken),
            Err(error) => Err(taken.discard_after(error)),
        }
    }

    /// Renames the root to `destination` without replacing anything there, after checking it is still this
    /// operation's entry. On failure everything recorded is cleaned up; an occupied destination is
    /// [`FsxErrorKind::LinkExists`] and is left alone. Returns the published entry's identity for reading it back.
    pub(super) fn publish(mut self, destination: &Path) -> Result<EntryId, FsxError> {
        self.require_root()?;
        let proof = move_proof(&self.entries[0]).map_err(|error| self.discard_after(error))?;
        match identity::rename_new(self.path(), destination) {
            Ok(()) => {
                self.entries[0].path = destination.to_path_buf();
                self.entries[0].id = None;
                let id = moved_id(&proof, &self.entries[0])
                    .map_err(|error| self.discard_after(error))?;
                Ok(id)
            }
            Err(error) => {
                let occupied = error.kind() == io::ErrorKind::AlreadyExists
                    || fs::symlink_metadata(destination).is_ok();
                let primary = if occupied {
                    FsxError::io(
                        FsxErrorKind::LinkExists,
                        destination,
                        "something else is already there; it was left alone",
                        error,
                    )
                } else {
                    io_error(destination, "cannot move OAH's new entry into place", error)
                };
                Err(self.discard_after(primary))
            }
        }
    }

    /// Removes every recorded entry, newest first and each only while it is still the entry this operation
    /// made. Nothing below a root that is no longer this operation's is touched, and an entry another writer
    /// replaced stops the cleanup before anything is removed.
    pub(super) fn discard(&self) -> Result<(), FsxError> {
        let root = &self.entries[0];
        match Stage::now(root)? {
            Now::Same => {}
            // The root moved away or was removed: nothing this operation made is at the recorded paths.
            Now::Missing => return Ok(()),
            Now::Other => return Err(replaced(&root.path)),
        }
        for entry in &self.entries[1..] {
            if Stage::now(entry)? == Now::Other {
                return Err(replaced(&entry.path));
            }
        }
        for entry in self.entries.iter().rev() {
            match Stage::now(entry)? {
                Now::Same => remove(entry)?,
                Now::Missing => {}
                Now::Other => return Err(replaced(&entry.path)),
            }
        }
        Ok(())
    }

    /// Cleans up after `primary`, which stays the reported failure; a cleanup failure is attached to it.
    pub(super) fn discard_after(&self, primary: FsxError) -> FsxError {
        match self.discard() {
            Ok(()) => primary,
            Err(cleanup) => primary.with_cleanup(cleanup),
        }
    }
}

fn move_proof(entry: &Acquired) -> Result<MoveProof, FsxError> {
    let id = entry.id.ok_or_else(|| replaced(&entry.path))?;
    MoveProof::acquire(&entry.path, id)
        .map_err(|error| {
            io_error(
                &entry.path,
                "cannot acquire the recorded entry for its move",
                error,
            )
        })?
        .ok_or_else(|| replaced(&entry.path))
}

/// The identity of the entry `proof` moved to `entry`'s new path while it is still the recorded entry, on Unix
/// with the recorded kind and link text too. Anything else is a replacement and is never adopted.
fn moved_id(proof: &MoveProof, entry: &Acquired) -> Result<EntryId, FsxError> {
    let destination = &entry.path;
    let unreadable = |error| io_error(destination, "cannot read the entry after its move", error);
    let id = proof
        .after(destination)
        .map_err(unreadable)?
        .ok_or_else(|| replaced(destination))?;
    #[cfg(unix)]
    if made_now(entry, destination).map_err(unreadable)? != Now::Same {
        return Err(replaced(destination));
    }
    Ok(id)
}

/// What `path` holds now, compared with the kind and, for a link, the stored text recorded for `entry`, once its
/// number matched. A missing entry is an answer; any other failure to read stays an error.
#[cfg(unix)]
fn made_now(entry: &Acquired, path: &Path) -> io::Result<Now> {
    let missing = |error: io::Error| match error.kind() {
        io::ErrorKind::NotFound => Ok(Now::Missing),
        _ => Err(error),
    };
    let file_type = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata.file_type(),
        Err(error) => return missing(error),
    };
    let same = match entry.shape {
        Shape::Folder => file_type.is_dir(),
        Shape::File => file_type.is_file(),
        Shape::Link if !file_type.is_symlink() => false,
        Shape::Link => match fs::read_link(path) {
            Ok(text) => entry.link_text.as_deref() == Some(text.as_os_str()),
            Err(error) => return missing(error),
        },
    };
    Ok(if same { Now::Same } else { Now::Other })
}

fn remove(entry: &Acquired) -> Result<(), FsxError> {
    let removed = match entry.shape {
        Shape::Folder => fs::remove_dir(&entry.path),
        Shape::File => fs::remove_file(&entry.path),
        Shape::Link => remove_link(&entry.path),
    };
    removed.map_err(|error| {
        io_error(
            &entry.path,
            "cannot remove an entry OAH made; it was left in place",
            error,
        )
    })
}

/// Removes a symlink or junction itself, never its target.
#[cfg(windows)]
pub(super) fn remove_link(path: &Path) -> io::Result<()> {
    use std::os::windows::fs::FileTypeExt;
    // A junction or directory symlink is removed as a folder, which deletes only the link.
    if fs::symlink_metadata(path)?.file_type().is_symlink_dir() {
        fs::remove_dir(path)
    } else {
        fs::remove_file(path)
    }
}

/// Removes a symlink itself, never its target.
#[cfg(not(windows))]
pub(super) fn remove_link(path: &Path) -> io::Result<()> {
    fs::remove_file(path)
}

/// Opens a regular file for copying without following a link at its own name.
#[cfg(unix)]
fn open_source(path: &Path) -> Result<File, FsxError> {
    use rustix::fs::{Mode, OFlags, open};
    // NONBLOCK keeps a special file swapped in after the walk from blocking; it does not change regular files.
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
    match open(path, flags, Mode::empty()) {
        Ok(fd) => Ok(File::from(fd)),
        Err(errno) if errno == rustix::io::Errno::LOOP => Err(FsxError::new(
            FsxErrorKind::TreeHasLink,
            path,
            "the copy source is now a link; links are never followed",
        )),
        Err(errno) => Err(io_error(
            path,
            "cannot open copy source without following links",
            errno.into(),
        )),
    }
}

/// Opens a regular file for copying without following a link at its own name.
#[cfg(windows)]
fn open_source(path: &Path) -> Result<File, FsxError> {
    use std::os::windows::fs::OpenOptionsExt;
    const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    fs::OpenOptions::new()
        .read(true)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|error| {
            io_error(
                path,
                "cannot open copy source without following links",
                error,
            )
        })
}

#[cfg(not(any(unix, windows)))]
fn open_source(path: &Path) -> Result<File, FsxError> {
    Err(io_error(
        path,
        "cannot open copy source without following links",
        io::ErrorKind::Unsupported.into(),
    ))
}

fn io_error(path: &Path, action: &str, error: io::Error) -> FsxError {
    FsxError::io(FsxErrorKind::Io, path, action, error)
}

fn replaced(path: &Path) -> FsxError {
    FsxError::new(
        FsxErrorKind::NotOwned,
        path,
        "another entry replaced what OAH made here; it was left alone",
    )
}

#[cfg(target_os = "linux")]
pub(super) mod protected {
    use crate::fsx::{Fingerprint, FsxError, FsxErrorKind};
    use rustix::fd::{AsFd, OwnedFd};
    use rustix::fs::{self as rx, AtFlags, FileType, Mode, OFlags, Statx, StatxFlags};
    use rustix::io::Errno;
    use std::ffi::OsStr;
    use std::fs::File;
    use std::io::Read;
    use std::os::unix::ffi::OsStrExt;
    use std::path::{Path, PathBuf};

    const DIRECTORY: OFlags = OFlags::RDONLY
        .union(OFlags::DIRECTORY)
        .union(OFlags::CLOEXEC)
        .union(OFlags::NOFOLLOW);
    pub(in crate::fsx) const FILE_FLAGS: OFlags = OFlags::CLOEXEC
        .union(OFlags::NOFOLLOW)
        .union(OFlags::NONBLOCK);
    const ENROLLMENT: &str = ".oah-enrollment";
    const PRIVATE: &str = ".oah-links";
    pub(in crate::fsx) const PUBLIC: &str = "links";

    #[derive(PartialEq, Eq)]
    struct Binding(u32, u32, u64, i64, u32);

    impl Binding {
        fn parse(line: &str) -> Option<Self> {
            let mut fields = line.split_ascii_whitespace();
            let binding = Self(
                fields.next()?.parse().ok()?,
                fields.next()?.parse().ok()?,
                fields.next()?.parse().ok()?,
                fields.next()?.parse().ok()?,
                fields.next()?.parse().ok()?,
            );
            fields.next().is_none().then_some(binding)
        }

        fn matches(&self, stat: &Statx) -> bool {
            StatxFlags::from_bits_retain(stat.stx_mask).contains(StatxFlags::BTIME)
                && *self
                    == Self(
                        stat.stx_dev_major,
                        stat.stx_dev_minor,
                        stat.stx_ino,
                        stat.stx_btime.tv_sec,
                        stat.stx_btime.tv_nsec,
                    )
        }
    }

    struct Enrollment {
        writer: (u32, u32),
        incarnation: Fingerprint,
        anchor: Binding,
        public: Binding,
        private: Binding,
    }

    impl Enrollment {
        fn parse(text: &str) -> Option<Self> {
            let mut lines = text.lines();
            if lines.next()? != "oah-protected-links-v1" {
                return None;
            }
            let (uid, gid) = lines.next()?.split_once(' ')?;
            let spec = Self {
                writer: (uid.parse().ok()?, gid.parse().ok()?),
                incarnation: Fingerprint::from_hex(lines.next()?)?,
                anchor: Binding::parse(lines.next()?)?,
                public: Binding::parse(lines.next()?)?,
                private: Binding::parse(lines.next()?)?,
            };
            (spec.writer.0 != 0 && spec.writer.1 != 0 && lines.next().is_none()).then_some(spec)
        }
    }

    // One private descriptor anchors the lock; the second slot is reused for public directory or journal I/O.
    pub(in crate::fsx) struct Namespace<'a> {
        pub anchor: &'a Path,
        pub private: OwnedFd,
        pub mount: u64,
        enrollment: Enrollment,
    }

    impl<'a> Namespace<'a> {
        pub fn open(anchor: &'a Path) -> Result<Self, FsxError> {
            let directory = open_anchor(anchor)?;
            let stat = descriptor(&directory, anchor)?;
            if !is_ext4_mount(stat.stx_mnt_id)? {
                return Err(refuse(anchor, "the enrolled anchor must be on local ext4"));
            }
            let path = anchor.join(ENROLLMENT);
            let fd = rx::openat(
                &directory,
                ENROLLMENT,
                OFlags::RDONLY | FILE_FLAGS,
                Mode::empty(),
            )
            .map_err(|err| authority_open_error(&path, err))?;
            let manifest = descriptor(&fd, &path)?;
            if (manifest.stx_uid, manifest.stx_gid) != (0, 0)
                || manifest.stx_mode != 0o100644
                || manifest.stx_nlink != 1
                || manifest.stx_mnt_id != stat.stx_mnt_id
            {
                return Err(refuse(
                    &path,
                    "unexpected enrollment owner, mode, type or mount",
                ));
            }
            mode_only(&fd, &path, false)?;
            let enrollment = Enrollment::parse(&read_text(fd, &path, 1024)?)
                .ok_or_else(|| refuse(&path, "unrecognized enrollment"))?;
            if !enrollment.anchor.matches(&stat) {
                return Err(refuse(anchor, "enrollment does not bind the opened anchor"));
            }
            if !writer_credentials(enrollment.writer)? {
                return Err(refuse(
                    anchor,
                    "this thread is not the enrolled unprivileged writer",
                ));
            }
            let path = anchor.join(PRIVATE);
            let private = rx::openat(&directory, PRIVATE, DIRECTORY, Mode::empty())
                .map_err(|err| authority_open_error(&path, err))?;
            drop(directory);
            let ns = Self {
                anchor,
                private,
                mount: stat.stx_mnt_id,
                enrollment,
            };
            ns.verify_directory(&ns.private, &path, 0o700, &ns.enrollment.private)?;
            drop(ns.public_folder()?);
            match rx::flock(&ns.private, rx::FlockOperation::NonBlockingLockExclusive) {
                Ok(()) => Ok(ns),
                Err(Errno::WOULDBLOCK) => Err(FsxError::io(
                    FsxErrorKind::Busy,
                    &path,
                    "the enrolled writer lock is busy",
                    Errno::WOULDBLOCK.into(),
                )),
                Err(err) => Err(io(&path, "cannot lock the enrolled journal directory", err)),
            }
        }

        pub fn public_folder(&self) -> Result<OwnedFd, FsxError> {
            let path = self.anchor.join(PUBLIC);
            // The enrolled directories are siblings whose names can only be changed by the provisioner.
            let fd = rx::openat(&self.private, "../links", DIRECTORY, Mode::empty())
                .map_err(|err| authority_open_error(&path, err))?;
            self.verify_directory(&fd, &path, 0o755, &self.enrollment.public)?;
            Ok(fd)
        }

        pub fn private_path(&self, name: &str) -> PathBuf {
            self.anchor.join(PRIVATE).join(name)
        }

        pub fn writer(&self) -> (u32, u32) {
            self.enrollment.writer
        }

        pub fn incarnation(&self) -> Fingerprint {
            self.enrollment.incarnation
        }

        fn verify_directory(
            &self,
            fd: &OwnedFd,
            path: &Path,
            mode: u16,
            binding: &Binding,
        ) -> Result<(), FsxError> {
            let stat = descriptor(fd, path)?;
            if (stat.stx_uid, stat.stx_gid) != self.writer()
                || stat.stx_mode != 0o040000 | mode
                || stat.stx_mnt_id != self.mount
                || !binding.matches(&stat)
            {
                return Err(refuse(
                    path,
                    "the writer directory does not match its enrollment",
                ));
            }
            mode_only(fd, path, true)
        }
    }

    pub(in crate::fsx) fn canonical_absolute(path: &Path) -> bool {
        let raw = path.as_os_str().as_bytes();
        raw == b"/"
            || (raw.len() < 4096
                && raw.strip_prefix(b"/").is_some_and(|tail| {
                    tail.split(|&byte| byte == b'/')
                        .all(|name| !matches!(name, b"" | b"." | b"..") && !name.contains(&0))
                }))
    }

    fn open_anchor(anchor: &Path) -> Result<OwnedFd, FsxError> {
        if !canonical_absolute(anchor) || anchor.parent().is_none() {
            return Err(refuse(
                anchor,
                "the enrollment anchor must be a canonical non-root path",
            ));
        }
        let mut path = PathBuf::from("/");
        let mut directory = rx::open(&path, DIRECTORY, Mode::empty())
            .map_err(|err| authority_open_error(&path, err))?;
        let root = verify_ancestor(&directory, &path)?;
        let root_type = rx::fstatfs(&directory)
            .map_err(|err| io(&path, "cannot inspect the root filesystem", err))?
            .f_type;
        if !matches!(root_type, 0x794c7630 | 0xef53) {
            return Err(refuse(
                &path,
                "expected an overlay or ext-family root mount",
            ));
        }
        for component in anchor.components().skip(1) {
            let name = component.as_os_str();
            path.push(name);
            let next = rx::openat(&directory, name, DIRECTORY, Mode::empty())
                .map_err(|err| authority_open_error(&path, err))?;
            let stat = verify_ancestor(&next, &path)?;
            if path != anchor && stat.stx_mnt_id != root.stx_mnt_id {
                return Err(refuse(
                    &path,
                    "only the enrollment anchor may cross a mount boundary",
                ));
            }
            directory = next;
        }
        Ok(directory)
    }

    fn verify_ancestor(fd: &OwnedFd, path: &Path) -> Result<Statx, FsxError> {
        let stat = descriptor(fd, path)?;
        if (stat.stx_uid, stat.stx_gid, stat.stx_mode) != (0, 0, 0o040755) {
            return Err(refuse(
                path,
                "the enrollment path requires a root-owned 0755 directory",
            ));
        }
        mode_only(fd, path, true)?;
        Ok(stat)
    }

    fn mode_only(fd: &OwnedFd, path: &Path, directory: bool) -> Result<(), FsxError> {
        for name in ["system.posix_acl_access", "system.posix_acl_default"]
            .into_iter()
            .take(if directory { 2 } else { 1 })
        {
            match rx::fgetxattr_query(fd, name) {
                Err(Errno::NODATA | Errno::NOTSUP) => {}
                Ok(_) => return Err(refuse(path, "an extended POSIX ACL is unsupported")),
                Err(err) => return Err(io(path, "cannot inspect POSIX ACL", err)),
            }
        }
        Ok(())
    }

    fn is_ext4_mount(id: u64) -> Result<bool, FsxError> {
        let info = proc_text("/proc/thread-self/mountinfo", 1 << 20)?;
        let id = id.to_string();
        Ok(info
            .lines()
            .filter_map(|line| line.split_once(" - "))
            .any(|(mount, fs)| {
                mount.split_ascii_whitespace().next() == Some(&id)
                    && fs.split_ascii_whitespace().next() == Some("ext4")
            }))
    }

    fn writer_credentials((uid, gid): (u32, u32)) -> Result<bool, FsxError> {
        for file in ["/proc/thread-self/uid_map", "/proc/thread-self/gid_map"] {
            if !proc_text(file, 256)?
                .split_ascii_whitespace()
                .eq(["0", "0", "4294967295"])
            {
                return Ok(false);
            }
        }
        let status = proc_text("/proc/thread-self/status", 1 << 16)?;
        let field = |key| {
            status.lines().find_map(|line| {
                let (name, value) = line.split_once(':')?;
                (name == key).then(|| value.trim())
            })
        };
        let matches_id = |key, id: u32| {
            field(key).is_some_and(|value| {
                value
                    .split_ascii_whitespace()
                    .map(str::parse::<u32>)
                    .eq(std::iter::repeat_n(Ok(id), 4))
            })
        };
        Ok(matches_id("Uid", uid)
            && matches_id("Gid", gid)
            && field("NoNewPrivs") == Some("1")
            && ["CapInh", "CapPrm", "CapEff", "CapAmb"]
                .into_iter()
                .all(|key| field(key) == Some("0000000000000000")))
    }

    fn proc_text(file: &str, limit: u64) -> Result<String, FsxError> {
        let path = Path::new(file);
        let fd = rx::open(path, OFlags::RDONLY | FILE_FLAGS, Mode::empty())
            .map_err(|err| authority_open_error(path, err))?;
        if rx::fstatfs(&fd)
            .map_err(|err| io(path, "cannot inspect procfs", err))?
            .f_type
            != rx::PROC_SUPER_MAGIC
            || FileType::from_raw_mode(u32::from(descriptor(&fd, path)?.stx_mode))
                != FileType::RegularFile
        {
            return Err(refuse(path, "expected a kernel procfs record"));
        }
        read_text(fd, path, limit)
    }

    fn read_text(fd: OwnedFd, path: &Path, limit: u64) -> Result<String, FsxError> {
        let mut bytes = Vec::new();
        File::from(fd)
            .take(limit + 1)
            .read_to_end(&mut bytes)
            .map_err(|err| io(path, "cannot read the authority record", err))?;
        if bytes.len() as u64 > limit {
            return Err(refuse(path, "the authority record is too large"));
        }
        String::from_utf8(bytes).map_err(|_| refuse(path, "the authority record is not UTF-8"))
    }

    pub(in crate::fsx) fn descriptor(fd: &impl AsFd, path: &Path) -> Result<Statx, FsxError> {
        probe(fd, OsStr::new(""), path)?.ok_or_else(|| refuse(path, "the opened entry is missing"))
    }

    pub(in crate::fsx) fn probe(
        fd: &impl AsFd,
        name: &OsStr,
        display: &Path,
    ) -> Result<Option<Statx>, FsxError> {
        let required = StatxFlags::BASIC_STATS | StatxFlags::MNT_ID;
        let stat = rx::statx(
            fd,
            name,
            AtFlags::EMPTY_PATH | AtFlags::SYMLINK_NOFOLLOW | AtFlags::NO_AUTOMOUNT,
            required | StatxFlags::BTIME,
        );
        match stat {
            Ok(stat) if StatxFlags::from_bits_retain(stat.stx_mask).contains(required) => {
                Ok(Some(stat))
            }
            Ok(_) | Err(Errno::NOSYS) => {
                Err(refuse(display, "mount and entry metadata are unavailable"))
            }
            Err(Errno::NOENT) => Ok(None),
            Err(err) => Err(io(display, "cannot inspect the anchored entry", err)),
        }
    }

    fn authority_open_error(path: &Path, err: Errno) -> FsxError {
        match err {
            Errno::NOENT | Errno::NOTDIR | Errno::LOOP => refuse(
                path,
                "an enrolled component is missing or not a plain entry",
            ),
            _ => io(path, "cannot open the enrolled component", err),
        }
    }

    pub(in crate::fsx) fn refuse(path: &Path, why: &str) -> FsxError {
        FsxError::new(FsxErrorKind::NotOwned, path, why)
    }

    pub(in crate::fsx) fn io(
        path: &Path,
        action: &str,
        err: impl Into<std::io::Error>,
    ) -> FsxError {
        FsxError::io(FsxErrorKind::Io, path, action, err.into())
    }
}

#[cfg(all(test, unix))]
mod tests {
    // Every case starts from a stage whose recorded entry number names another writer's replacement.
    use super::*;
    use std::ffi::OsString;
    use std::os::unix::fs::symlink;

    /// A source folder and another writer's folder, each holding one file.
    struct Case {
        _dir: tempfile::TempDir,
        root: PathBuf,
        source: PathBuf,
        elsewhere: PathBuf,
    }

    impl Case {
        fn new() -> Case {
            let dir = tempfile::tempdir().unwrap();
            let root = fs::canonicalize(dir.path()).unwrap();
            let source = root.join("source");
            fs::create_dir(&source).unwrap();
            fs::write(source.join("keep.txt"), b"source bytes").unwrap();
            let elsewhere = root.join("elsewhere");
            fs::create_dir(&elsewhere).unwrap();
            fs::write(elsewhere.join("theirs.txt"), b"their bytes").unwrap();
            Case {
                _dir: dir,
                root,
                source,
                elsewhere,
            }
        }

        /// Neither the source nor the other writer's folder gained, lost or changed anything.
        fn untouched(&self) -> Result<(), String> {
            unchanged(&self.source, &folder_of("keep.txt", b"source bytes"))?;
            unchanged(&self.elsewhere, &folder_of("theirs.txt", b"their bytes"))
        }
    }

    /// An entry itself, without following a link.
    #[derive(Debug, PartialEq, Eq)]
    enum Seen {
        Missing,
        Link(OsString),
        File(Vec<u8>),
        /// Each child's name and, for a file, its bytes.
        Folder(Vec<(String, Vec<u8>)>),
    }

    fn folder_of(name: &str, bytes: &[u8]) -> Seen {
        Seen::Folder(vec![(name.to_string(), bytes.to_vec())])
    }

    fn seen(path: &Path) -> Seen {
        let Ok(metadata) = fs::symlink_metadata(path) else {
            return Seen::Missing;
        };
        let file_type = metadata.file_type();
        if file_type.is_symlink() {
            Seen::Link(fs::read_link(path).unwrap().into_os_string())
        } else if file_type.is_dir() {
            let mut children: Vec<_> = fs::read_dir(path)
                .unwrap()
                .map(|child| {
                    let child = child.unwrap().path();
                    let name = child.file_name().unwrap().to_string_lossy().into_owned();
                    (name, fs::read(&child).unwrap_or_default())
                })
                .collect();
            children.sort();
            Seen::Folder(children)
        } else {
            Seen::File(fs::read(path).unwrap())
        }
    }

    fn unchanged(path: &Path, before: &Seen) -> Result<(), String> {
        let after = seen(path);
        if after == *before {
            Ok(())
        } else {
            Err(format!(
                "{} changed from {before:?} to {after:?}",
                path.display()
            ))
        }
    }

    fn refused<T>(result: Result<T, FsxError>) -> Result<(), String> {
        match result {
            Err(error) if error.kind() == FsxErrorKind::NotOwned => Ok(()),
            Err(error) => Err(format!(
                "failed as {:?}, not NotOwned: {error}",
                error.kind()
            )),
            Ok(_) => Err("adopted the replacement".to_string()),
        }
    }

    /// What another writer makes after deleting the recorded entry.
    #[derive(Debug, Clone, Copy)]
    enum Foreign {
        /// A symlink storing the other writer's folder, as in the failing Linux run.
        LinkElsewhere,
        /// A symlink reaching the same source folder through other stored text.
        LinkOtherText,
        LinkPathAlias(&'static str, &'static str),
        File,
        Folder,
    }

    fn replace(case: &Case, path: &Path, foreign: Foreign) {
        if fs::symlink_metadata(path).unwrap().is_dir() {
            fs::remove_dir_all(path).unwrap();
        } else {
            fs::remove_file(path).unwrap();
        }
        match foreign {
            Foreign::LinkElsewhere => symlink(&case.elsewhere, path).unwrap(),
            Foreign::LinkOtherText => symlink("source", path).unwrap(),
            Foreign::LinkPathAlias(prefix, suffix) => {
                let mut text = OsString::from(prefix);
                text.push(case.root.as_os_str());
                text.push(suffix);
                assert_eq!(Path::new(&text), case.source);
                assert_ne!(text.as_os_str(), case.source.as_os_str());
                symlink(&text, path).unwrap();
                assert_eq!(fs::read_link(path).unwrap().into_os_string(), text);
            }
            Foreign::File => fs::write(path, b"their file").unwrap(),
            Foreign::Folder => {
                fs::create_dir(path).unwrap();
                fs::write(path.join("theirs.txt"), b"their bytes").unwrap();
            }
        }
    }

    /// Records for entry `index` the number its replacement holds now and reports whether the file system had
    /// already handed it over. Linux file systems such as ext4 give a deleted entry's number to the next entry
    /// made, so this is the record such a stage holds after another writer swaps its entry; setting it shows
    /// that case on every Unix file system.
    fn reuse_number(stage: &mut Stage, index: usize) -> bool {
        let entry = &mut stage.entries[index];
        let now = EntryId::of(&entry.path).unwrap();
        let natural = entry.id == Some(now);
        entry.id = Some(now);
        natural
    }

    /// A link made in an attempt folder and moved to its private name, as a native link is before publishing.
    fn taken_link(case: &Case, target: &Path) -> Stage {
        let folder = case.root.join(".skill.attempt.tmp");
        let mut attempt = Stage::folder(&folder).unwrap();
        symlink(target, folder.join("skill")).unwrap();
        assert_eq!(
            attempt.record_attempt(&folder.join("skill")).unwrap(),
            Attempted::Link
        );
        attempt
            .take_link(&case.root.join(".skill.link.tmp"))
            .unwrap()
    }

    #[derive(Debug, Clone, Copy)]
    enum LinkStep {
        Publish,
        Discard,
        VerifyMove,
    }

    fn link_stage(foreign: Foreign, step: LinkStep) -> (bool, Result<(), String>) {
        let case = Case::new();
        let mut stage = taken_link(&case, &case.source);
        let staged = stage.path().to_path_buf();
        replace(&case, &staged, foreign);
        let natural = reuse_number(&mut stage, 0);
        let before = seen(&staged);
        let destination = case.root.join("skill");
        let (refusal, occupied, vacant) = match step {
            LinkStep::Publish => (refused(stage.publish(&destination)), &staged, &destination),
            LinkStep::Discard => (refused(stage.discard()), &staged, &destination),
            LinkStep::VerifyMove => {
                let entry = &mut stage.entries[0];
                let proof = MoveProof::acquire(&staged, entry.id.take().unwrap())
                    .unwrap()
                    .unwrap();
                identity::rename_new(&staged, &destination).unwrap();
                entry.path = destination.clone();
                (refused(moved_id(&proof, entry)), &destination, &staged)
            }
        };
        let held = refusal
            .and_then(|()| unchanged(occupied, &before))
            .and_then(|()| unchanged(vacant, &Seen::Missing))
            .and_then(|()| case.untouched());
        (natural, held)
    }

    /// The link an attempt recorded, swapped for another writer's entry, is neither moved out nor removed.
    fn attempt_stage(foreign: Foreign, take: bool) -> (bool, Result<(), String>) {
        let case = Case::new();
        let folder = case.root.join(".skill.attempt.tmp");
        let mut stage = Stage::folder(&folder).unwrap();
        let entry = folder.join("skill");
        symlink(&case.source, &entry).unwrap();
        assert_eq!(stage.record_attempt(&entry).unwrap(), Attempted::Link);
        replace(&case, &entry, foreign);
        let natural = reuse_number(&mut stage, 1);
        let before = seen(&entry);
        let to = case.root.join(".skill.link.tmp");
        let refusal = if take {
            refused(stage.take_link(&to))
        } else {
            refused(stage.discard())
        };
        let held = refusal
            .and_then(|()| unchanged(&entry, &before))
            .and_then(|()| unchanged(&to, &Seen::Missing))
            .and_then(|()| case.untouched());
        (natural, held)
    }

    #[derive(Debug, Clone, Copy)]
    enum CopyStep {
        Directory,
        File,
        Publish,
        Discard,
    }

    /// A copy whose root folder another writer swapped makes, publishes and removes nothing, and nothing is made
    /// through a swapped-in link.
    fn copy_root(foreign: Foreign, step: CopyStep) -> (bool, Result<(), String>) {
        let case = Case::new();
        let path = case.root.join(".skill.copy.tmp");
        let mut stage = Stage::folder(&path).unwrap();
        replace(&case, &path, foreign);
        let natural = reuse_number(&mut stage, 0);
        let before = seen(&path);
        let destination = case.root.join("skill");
        let refusal = match step {
            CopyStep::Directory => refused(stage.directory(Path::new("made"))),
            CopyStep::File => {
                refused(stage.file(&case.source.join("keep.txt"), Path::new("keep.txt")))
            }
            CopyStep::Publish => refused(stage.publish(&destination)),
            CopyStep::Discard => refused(stage.discard()),
        };
        let held = refusal
            .and_then(|()| unchanged(&path, &before))
            .and_then(|()| unchanged(&destination, &Seen::Missing))
            .and_then(|()| case.untouched());
        (natural, held)
    }

    /// A copied file or folder another writer swapped stops the cleanup before anything is removed.
    fn copy_child(folder: bool, foreign: Foreign) -> (bool, Result<(), String>) {
        let case = Case::new();
        let path = case.root.join(".skill.copy.tmp");
        let mut stage = Stage::folder(&path).unwrap();
        stage.directory(Path::new("made")).unwrap();
        stage
            .file(&case.source.join("keep.txt"), Path::new("keep.txt"))
            .unwrap();
        let (index, child) = if folder {
            (1, path.join("made"))
        } else {
            (2, path.join("keep.txt"))
        };
        replace(&case, &child, foreign);
        let natural = reuse_number(&mut stage, index);
        let before = (seen(&path), seen(&child));
        let held = refused(stage.discard())
            .and_then(|()| unchanged(&path, &before.0))
            .and_then(|()| unchanged(&child, &before.1))
            .and_then(|()| case.untouched());
        (natural, held)
    }

    /// The same steps still accept this operation's own entries.
    fn own_entries() -> Result<(), String> {
        let case = Case::new();
        let destination = case.root.join("skill");
        let mut text = case.source.clone().into_os_string();
        text.push("/.");
        let target = Path::new(&text);
        taken_link(&case, target)
            .publish(&destination)
            .map_err(|error| format!("its own link was refused: {error}"))?;
        let stage = taken_link(&case, target);
        stage
            .discard()
            .map_err(|error| format!("its own link cleanup was refused: {error}"))?;
        unchanged(stage.path(), &Seen::Missing)?;
        unchanged(&destination, &Seen::Link(text))?;
        let path = case.root.join(".skill.copy.tmp");
        let mut stage = Stage::folder(&path).unwrap();
        stage
            .directory(Path::new("made"))
            .and_then(|()| stage.file(&case.source.join("keep.txt"), Path::new("keep.txt")))
            .and_then(|()| stage.discard())
            .map_err(|error| format!("its own copy was refused: {error}"))?;
        unchanged(&path, &Seen::Missing)?;
        case.untouched()
    }

    #[test]
    fn reused_entry_number_never_adopts_another_kind_or_link_text() {
        own_entries().unwrap();
        println!("OWN_ENTRIES accepted");
        let mut cases = Vec::new();
        for foreign in [
            Foreign::LinkElsewhere,
            Foreign::LinkOtherText,
            Foreign::File,
            Foreign::Folder,
        ] {
            for step in [LinkStep::Publish, LinkStep::Discard] {
                let name = format!("link-stage {step:?} over {foreign:?}");
                cases.push((name, link_stage(foreign, step)));
            }
        }
        for foreign in [Foreign::LinkElsewhere, Foreign::File, Foreign::Folder] {
            for take in [true, false] {
                let step = if take { "take_link" } else { "discard" };
                let name = format!("attempt-stage {step} over {foreign:?}");
                cases.push((name, attempt_stage(foreign, take)));
            }
        }
        for foreign in [
            Foreign::LinkPathAlias("", "/source/"),
            Foreign::LinkPathAlias("", "//source"),
            Foreign::LinkPathAlias("/", "/source"),
            Foreign::LinkPathAlias("", "/./source"),
            Foreign::LinkPathAlias("", "/source/."),
        ] {
            for step in [LinkStep::Publish, LinkStep::Discard, LinkStep::VerifyMove] {
                let name = format!("link-stage {step:?} over {foreign:?}");
                cases.push((name, link_stage(foreign, step)));
            }
            for take in [true, false] {
                let step = if take { "take_link" } else { "discard" };
                let name = format!("attempt-stage {step} over {foreign:?}");
                cases.push((name, attempt_stage(foreign, take)));
            }
        }
        for foreign in [Foreign::LinkElsewhere, Foreign::File] {
            for step in [
                CopyStep::Directory,
                CopyStep::File,
                CopyStep::Publish,
                CopyStep::Discard,
            ] {
                let name = format!("copy-root {step:?} over {foreign:?}");
                cases.push((name, copy_root(foreign, step)));
            }
        }
        for (folder, foreign) in [
            (false, Foreign::LinkElsewhere),
            (false, Foreign::Folder),
            (true, Foreign::File),
            (true, Foreign::LinkElsewhere),
        ] {
            let child = if folder { "folder" } else { "file" };
            let name = format!("copy-{child} discard over {foreign:?}");
            cases.push((name, copy_child(folder, foreign)));
        }
        let mut adopted = Vec::new();
        for (name, (natural, held)) in &cases {
            match held {
                Ok(()) => println!("REFUSED {name} natural_reuse={natural}"),
                Err(why) => {
                    println!("ADOPTED {name} natural_reuse={natural}: {why}");
                    adopted.push(name.as_str());
                }
            }
        }
        println!(
            "REUSE_GUARD cases={} refused={} adopted={}",
            cases.len(),
            cases.len() - adopted.len(),
            adopted.len()
        );
        assert!(
            adopted.is_empty(),
            "a reused entry number adopted another writer's entry: {adopted:?}"
        );
    }
}
