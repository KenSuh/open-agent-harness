use super::native::{Dir, FileAccess, LockAttempt};
use super::{ObjectId, PublishError};
use rustix::fs::{self as rx, AtFlags, Mode, OFlags, RenameFlags};
use rustix::io::Errno;
use std::ffi::{OsStr, OsString};
use std::fs::{File, TryLockError};
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

type Result<T> = std::result::Result<T, PublishError>;
const DIRECTORY: OFlags = OFlags::DIRECTORY
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

fn native(err: Errno) -> PublishError {
    match err {
        Errno::LOOP | Errno::NOTDIR => PublishError::UnsafePath,
        Errno::XDEV | Errno::NOTSUP => PublishError::Unsupported,
        _ => io::Error::from(err).into(),
    }
}

pub(super) fn root(path: &Path) -> Result<PathBuf> {
    if !path.is_absolute() {
        return Err(PublishError::UnsafePath);
    }
    Ok(PathBuf::from("/"))
}

pub(super) fn open_root(path: &Path) -> Result<File> {
    rx::open(path, DIRECTORY, Mode::empty())
        .map(File::from)
        .map_err(native)
}

pub(super) fn qualify(file: &File) -> Result<()> {
    const MNT_LOCAL: u32 = 0x1000;
    let stat = rx::fstatfs(file).map_err(native)?;
    if stat.f_fstypename.get(..5) != Some(&b"apfs\0".map(u8::cast_signed))
        || stat.f_flags & MNT_LOCAL == 0
    {
        return Err(PublishError::Unsupported);
    }
    Ok(())
}

pub(super) fn identity(file: &File) -> Result<ObjectId> {
    let meta = file.metadata()?;
    let birth = meta
        .created()?
        .duration_since(UNIX_EPOCH)
        .map_err(|_| PublishError::Unsupported)?;
    if meta.ino() == 0 {
        return Err(PublishError::Unsupported);
    }
    Ok(ObjectId {
        volume: meta.dev(),
        index: meta.ino(),
        birth_seconds: birth.as_secs(),
        birth_nanos: birth.subsec_nanos(),
    })
}

pub(super) fn check_named(parent: &Dir, dir: &Dir) -> Result<()> {
    let name = dir.path().file_name().ok_or(PublishError::UnsafePath)?;
    let stat = rx::statat(parent.file(), name, AtFlags::SYMLINK_NOFOLLOW).map_err(native)?;
    if stat.st_ino != dir.id().index || u64::from(stat.st_dev.cast_unsigned()) != dir.id().volume {
        return Err(PublishError::UnsafePath);
    }
    Ok(())
}

pub(super) fn open_dir(dir: &Dir, name: &OsStr) -> Result<File> {
    rx::openat(dir.file(), name, DIRECTORY, Mode::empty())
        .map(File::from)
        .map_err(native)
}

pub(super) fn names(dir: &Dir) -> Result<Vec<OsString>> {
    let entries = rx::Dir::read_from(dir.file()).map_err(native)?;
    let mut names = Vec::new();
    for entry in entries {
        let entry = entry.map_err(native)?;
        let name = entry.file_name().to_bytes();
        if !matches!(name, b"." | b"..") {
            names.push(OsStr::from_bytes(name).to_owned());
        }
    }
    Ok(names)
}

pub(super) fn mkdir(dir: &Dir, name: &str) -> Result<()> {
    rx::mkdirat(dir.file(), name, Mode::RWXU).map_err(native)
}

pub(super) fn regular(dir: &Dir, name: &str, access: FileAccess) -> Result<File> {
    let flags = match access {
        FileAccess::Read | FileAccess::Remove => OFlags::RDONLY,
        FileAccess::Create => OFlags::RDWR | OFlags::CREATE | OFlags::EXCL,
        FileAccess::Lock => OFlags::RDWR | OFlags::CREATE,
    } | OFlags::NOFOLLOW
        | OFlags::CLOEXEC
        | OFlags::NONBLOCK;
    let file =
        File::from(rx::openat(dir.file(), name, flags, Mode::RUSR | Mode::WUSR).map_err(native)?);
    let meta = file.metadata()?;
    if !meta.is_file() || meta.nlink() != 1 {
        return Err(PublishError::UnsafePath);
    }
    Ok(file)
}

pub(super) fn rename_new(
    dir: &Dir,
    name: &str,
    dest: &Dir,
    to: &str,
    expected: &ObjectId,
) -> Result<()> {
    let source = open_dir(dir, name.as_ref())?;
    if identity(&source)? != *expected {
        return Err(PublishError::Changed);
    }
    rx::renameat_with(dir.file(), name, dest.file(), to, RenameFlags::NOREPLACE).map_err(native)?;
    let landed = open_dir(dest, to.as_ref()).and_then(|file| identity(&file));
    if !landed.is_ok_and(|id| id == *expected) {
        return Err(PublishError::MovedUnverified {
            path: dest.path().join(to),
        });
    }
    Ok(())
}

pub(super) fn sync_dir(dir: &Dir) -> Result<()> {
    rx::fsync(dir.file()).map_err(native)
}

pub(super) fn permissions(file: &File) -> Result<u32> {
    Ok(file.metadata()?.mode() & 0o7777)
}

pub(super) fn plain_payload(file: &File) -> Result<()> {
    const SYSTEM_PROVENANCE: &[u8] = b"com.apple.provenance\0";
    let mut names = [0u8; SYSTEM_PROVENANCE.len()];
    match rx::flistxattr(file, &mut names[..]) {
        Ok(0) => Ok(()),
        Ok(n) if names.get(..n) == Some(SYSTEM_PROVENANCE) => Ok(()),
        Ok(_) | Err(Errno::RANGE) => Err(PublishError::Changed),
        Err(err) => Err(native(err)),
    }
}

pub(super) fn remove_file(dir: &Dir, name: &str, file: &File) -> Result<()> {
    let current = regular(dir, name, FileAccess::Read)?;
    if identity(&current)? != identity(file)? {
        return Err(PublishError::Changed);
    }
    rx::unlinkat(dir.file(), name, AtFlags::empty()).map_err(native)
}

pub(super) fn remove_empty(dir: &Dir, name: &str, expected: &ObjectId) -> Result<()> {
    let file = open_dir(dir, name.as_ref())?;
    if identity(&file)? != *expected {
        return Err(PublishError::Changed);
    }
    plain_payload(&file)?;
    rx::unlinkat(dir.file(), name, AtFlags::REMOVEDIR).map_err(native)
}

pub(super) fn mkdir_shared(dir: &Dir, name: &str) -> Result<()> {
    // The process umask applies, as it does to a folder the standard library makes.
    rx::mkdirat(dir.file(), name, Mode::from_raw_mode(0o777)).map_err(native)
}

/// Creates the new regular file `name` for a staged write, with the permissions a plain create gives.
pub(super) fn stage(dir: &Dir, name: &str) -> Result<File> {
    let flags = OFlags::RDWR | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    rx::openat(dir.file(), name, flags, Mode::from_raw_mode(0o666))
        .map(File::from)
        .map_err(native)
}

pub(super) fn open_file(dir: &Dir, name: &str, _hold: bool) -> Result<File> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK;
    for _ in 0..8 {
        let file = File::from(rx::openat(dir.file(), name, flags, Mode::empty()).map_err(native)?);
        let meta = file.metadata()?;
        if !meta.is_file() {
            return Err(PublishError::UnsafePath);
        }
        match meta.nlink() {
            1 => return Ok(file),
            // Replaced between the open and this look: the name now has its successor, so open that.
            0 => {}
            _ => return Err(PublishError::UnsafePath),
        }
    }
    Err(PublishError::Busy)
}

/// Whether `name` in `dir` is, without following a link, the open `file`.
fn is_entry(dir: &Dir, name: &str, file: &File) -> Result<bool> {
    let held = file.metadata()?;
    match rx::statat(dir.file(), name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(stat) => {
            Ok(stat.st_ino == held.ino() && u64::from(stat.st_dev.cast_unsigned()) == held.dev())
        }
        Err(Errno::NOENT) => Ok(false),
        Err(err) => Err(native(err)),
    }
}

pub(super) fn try_lock(dir: &Dir, name: &str) -> Result<LockAttempt> {
    let flags =
        OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK;
    let file = match rx::openat(dir.file(), name, flags, Mode::from_raw_mode(0o666)) {
        Ok(file) => File::from(file),
        // The holder removed the lock file between the lookup and the open of this create-or-open.
        Err(Errno::NOENT) => return Ok(LockAttempt::Retry),
        Err(err) => return Err(native(err)),
    };
    if !file.metadata()?.is_file() {
        return Err(PublishError::UnsafePath);
    }
    match file.try_lock() {
        // A releasing holder removes the name before unlocking, so only a lock on the file the name still has
        // counts.
        Ok(()) if is_entry(dir, name, &file)? => Ok(LockAttempt::Held(file)),
        Ok(()) => Ok(LockAttempt::Retry),
        Err(TryLockError::WouldBlock) => Ok(LockAttempt::Busy),
        Err(TryLockError::Error(err)) => Err(err.into()),
    }
}

pub(super) fn unlock(dir: &Dir, name: &str, file: File) -> Result<()> {
    let removed = match is_entry(dir, name, &file) {
        Ok(true) => rx::unlinkat(dir.file(), name, AtFlags::empty()).map_err(native),
        Ok(false) => Ok(()),
        Err(err) => Err(err),
    };
    drop(file);
    removed
}

/// Gives the staged `file` the vacant name `name`; never replaces an entry.
pub(super) fn install_file(dir: &Dir, staged: &str, file: &File, name: &str) -> Result<()> {
    if !is_entry(dir, staged, file)? {
        return Err(PublishError::Changed);
    }
    rx::renameat_with(dir.file(), staged, dir.file(), name, RenameFlags::NOREPLACE)
        .map_err(native)?;
    if !is_entry(dir, name, file).is_ok_and(|same| same) {
        return Err(PublishError::MovedUnverified {
            path: dir.path().join(name),
        });
    }
    Ok(())
}

/// Swaps the staged `new` file with the entry `name`, which must be `old`, in one atomic step. `true`: afterwards
/// `name` is `new` and the staged name is `old`, for the caller to check and remove. When another entry had taken `name` just before the swap, it is swapped back.
pub(super) fn exchange_file(
    dir: &Dir,
    staged: &str,
    new: &File,
    name: &str,
    old: &File,
) -> Result<bool> {
    if !is_entry(dir, staged, new)? || !is_entry(dir, name, old)? {
        return Err(PublishError::Changed);
    }
    let swap = || {
        rx::renameat_with(dir.file(), staged, dir.file(), name, RenameFlags::EXCHANGE)
            .map_err(native)
    };
    swap()?;
    let landed = |entry: &str, file: &File| is_entry(dir, entry, file).is_ok_and(|same| same);
    if landed(name, new) && landed(staged, old) {
        return Ok(true);
    }
    if landed(name, new) && swap().is_ok() && landed(staged, new) {
        return Err(PublishError::Changed);
    }
    Err(PublishError::MovedUnverified {
        path: dir.path().join(staged),
    })
}

/// Undoes [`exchange_file`] while both entries are still the ones it left.
pub(super) fn revert_file(
    dir: &Dir,
    staged: &str,
    new: &File,
    name: &str,
    old: &File,
) -> Result<()> {
    let unverified = || PublishError::MovedUnverified {
        path: dir.path().join(staged),
    };
    if !is_entry(dir, name, new).is_ok_and(|same| same)
        || !is_entry(dir, staged, old).is_ok_and(|same| same)
    {
        return Err(unverified());
    }
    rx::renameat_with(dir.file(), staged, dir.file(), name, RenameFlags::EXCHANGE)
        .map_err(|_| unverified())
}
