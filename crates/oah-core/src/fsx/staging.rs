// 임시 소유자: 이 작업이 배타적으로 만든 비공개 항목과 그 신원 값만 기록하고, 실패하면 기록한 항목만 역순으로 지운다.
//! Private temporary owners for link and copy creation.
//!
//! A [`Stage`] starts from one entry this operation made exclusively under a private name next to the
//! destination: a folder (a copy being filled, or the folder a native-link attempt runs in) or a native link that
//! was moved out of such a folder. Every entry it makes is recorded before the next step that can fail, with a
//! plain identity value read right after making it, so no file stays open. Cleanup removes only recorded entries
//! that are still the ones this operation made, one at a time and never recursively: a collision, a replacement
//! or anything another writer put inside is left exactly as it is, and the failure that started the cleanup is
//! kept. Publishing renames the root to the destination without replacing anything there.

use super::error::{FsxError, FsxErrorKind};
use super::identity::{self, EntryId, MoveProof, Now};
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
        });
        let last = self.entries.len() - 1;
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

    /// Whether a recorded entry is still the one this operation made. An unreadable identity is an error.
    fn now(entry: &Acquired) -> Result<Now, FsxError> {
        let id = entry.id.ok_or_else(|| {
            FsxError::new(
                FsxErrorKind::Io,
                &entry.path,
                "its identity was never read, so it was left in place",
            )
        })?;
        identity::now(&entry.path, id).map_err(|error| {
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
        let id = moved_id(&proof, to).map_err(|error| self.discard_after(error))?;
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
                let id =
                    moved_id(&proof, destination).map_err(|error| self.discard_after(error))?;
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

fn moved_id(proof: &MoveProof, destination: &Path) -> Result<EntryId, FsxError> {
    proof
        .after(destination)
        .map_err(|error| io_error(destination, "cannot read the entry after its move", error))?
        .ok_or_else(|| replaced(destination))
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
