use super::native::{Dir, FileAccess};
use super::{ObjectId, PublishError};
use rustix::fs::{self as rx, AtFlags, Mode, OFlags, RenameFlags};
use rustix::io::Errno;
use std::ffi::{OsStr, OsString};
use std::fs::File;
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
