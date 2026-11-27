use super::native::{Dir, FileAccess, Kind, LockAttempt};
use super::{ObjectId, PublishError};
use std::ffi::{OsStr, c_void};
use std::fs::{self, File, OpenOptions};
use std::io;
use std::mem::{offset_of, size_of};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Component, Path, PathBuf, Prefix};
use std::ptr::null_mut;
use windows_sys::Win32::Storage::FileSystem::{
    DELETE, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT, FILE_LIST_DIRECTORY, FILE_READ_ATTRIBUTES, FILE_RENAME_INFO,
    FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, FileRenameInfo, GetDriveTypeW,
    GetVolumeInformationByHandleW, SetFileInformationByHandle,
};

type Result<T> = std::result::Result<T, PublishError>;

pub(super) fn root(path: &Path) -> Result<PathBuf> {
    let mut components = path.components();
    let (Some(Component::Prefix(prefix)), Some(Component::RootDir)) =
        (components.next(), components.next())
    else {
        return Err(PublishError::UnsafePath);
    };
    let (Prefix::Disk(letter) | Prefix::VerbatimDisk(letter)) = prefix.kind() else {
        return Err(PublishError::Unsupported);
    };
    let drive = [u16::from(letter), 58, 92, 0];
    // SAFETY: drive is a live, NUL-terminated UTF-16 drive root.
    if unsafe { GetDriveTypeW(drive.as_ptr()) } != 3 {
        return Err(PublishError::Unsupported);
    }
    let mut root = PathBuf::from(prefix.as_os_str());
    root.push(r"\");
    Ok(root)
}

pub(super) fn qualify(file: &File) -> Result<()> {
    let mut name = [0u16; 32];
    // SAFETY: file is open, name has space for 32 wide characters, all omitted outputs are optional.
    let ok = unsafe {
        GetVolumeInformationByHandleW(
            file.as_raw_handle(),
            null_mut(),
            0,
            null_mut(),
            null_mut(),
            null_mut(),
            name.as_mut_ptr(),
            32,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error().into());
    }
    if !name.starts_with(&b"NTFS\0".map(u16::from)) {
        return Err(PublishError::Unsupported);
    }
    Ok(())
}

fn open_directory(path: &Path, access: u32) -> Result<File> {
    let file = OpenOptions::new()
        .access_mode(access)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS)
        .open(path)?;
    plain(&file, true)?;
    Ok(file)
}

fn pin_directory(path: &Path) -> Result<File> {
    // NTFS ignores metadata-only opens for sharing. List access makes the pin deny DELETE opens.
    open_directory(path, FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY)
}

fn plain(file: &File, directory: bool) -> Result<()> {
    let info = winapi_util::file::information(file)?;
    let attrs = info.file_attributes();
    if attrs & u64::from(FILE_ATTRIBUTE_REPARSE_POINT) != 0
        || (attrs & u64::from(FILE_ATTRIBUTE_DIRECTORY) != 0) != directory
        || (!directory && info.number_of_links() != 1)
    {
        return Err(PublishError::UnsafePath);
    }
    Ok(())
}

pub(super) fn identity(file: &File) -> Result<ObjectId> {
    let info = winapi_util::file::information(file)?;
    let ticks = info.creation_time().ok_or(PublishError::Unsupported)?;
    if info.file_index() == 0 {
        return Err(PublishError::Unsupported);
    }
    Ok(ObjectId {
        volume: info.volume_serial_number(),
        index: info.file_index(),
        birth_seconds: ticks / 10_000_000,
        birth_nanos: u32::try_from(ticks % 10_000_000).map_err(|_| PublishError::Unsupported)?
            * 100,
    })
}

pub(super) fn open_root(path: &Path) -> Result<File> {
    pin_directory(path)
}
pub(super) fn permissions(file: &File) -> Result<u32> {
    use std::os::windows::fs::MetadataExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_READONLY, FILE_ATTRIBUTE_SYSTEM,
    };
    Ok(file.metadata()?.file_attributes()
        & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_READONLY | FILE_ATTRIBUTE_SYSTEM))
}
pub(super) fn open_dir(dir: &Dir, name: &OsStr) -> Result<File> {
    pin_directory(&dir.path().join(name))
}

pub(super) fn check_named(_parent: &Dir, dir: &Dir) -> Result<()> {
    // All ancestor handles deny delete sharing, including on the source's path while it is renamed.
    plain(dir.file(), true)
}

pub(super) fn names(dir: &Dir) -> Result<Vec<std::ffi::OsString>> {
    Ok(fs::read_dir(dir.path())?
        .map(|entry| entry.map(|e| e.file_name()))
        .collect::<io::Result<_>>()?)
}

pub(super) fn mkdir(dir: &Dir, name: &str) -> Result<()> {
    Ok(fs::create_dir(dir.path().join(name))?)
}

pub(super) fn regular(dir: &Dir, name: &str, access: FileAccess) -> Result<File> {
    let mut opts = OpenOptions::new();
    opts.read(true)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .share_mode(FILE_SHARE_READ);
    match access {
        FileAccess::Read => {}
        FileAccess::ReadShared => {
            opts.share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE);
        }
        FileAccess::Remove => {
            opts.access_mode(windows_sys::Win32::Storage::FileSystem::FILE_GENERIC_READ | DELETE);
        }
        FileAccess::Create => {
            opts.write(true).create_new(true);
        }
        FileAccess::Lock => {
            opts.write(true)
                .create(true)
                .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE);
        }
    }
    let file = opts.open(dir.path().join(name))?;
    plain(&file, false)?;
    Ok(file)
}

#[repr(C)]
struct RenameBuffer {
    flags: u32,
    root: *mut c_void,
    length: u32,
    name: [u16; 32768],
}

const _: () = assert!(
    offset_of!(RenameBuffer, name) == offset_of!(FILE_RENAME_INFO, FileName)
        && offset_of!(RenameBuffer, length) == offset_of!(FILE_RENAME_INFO, FileNameLength)
        && offset_of!(RenameBuffer, root) == offset_of!(FILE_RENAME_INFO, RootDirectory)
);

pub(super) fn rename_new(
    dir: &Dir,
    name: &str,
    dest: &Dir,
    to: &str,
    expected: &ObjectId,
) -> Result<()> {
    let source = open_directory(&dir.path().join(name), FILE_READ_ATTRIBUTES | DELETE)?;
    if identity(&source)? != *expected {
        return Err(PublishError::Changed);
    }
    rename_handle(&source, &dest.path().join(to), 0)
}

/// `FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS`: one atomic step gives the name to
/// the renamed file; handles already open on the replaced file stay valid.
const REPLACE_ATOMICALLY: u32 = 0x1 | 0x2;

/// Renames the entry open as `source` (with DELETE access) to the absolute `path`. With `flags` 0 it never
/// replaces an entry. The path is always absolute: this call resolves a bare name against the process's current
/// folder, not the folder of the file.
fn rename_handle(source: &File, path: &Path, flags: u32) -> Result<()> {
    use windows_sys::Win32::Storage::FileSystem::FileRenameInfoEx;
    if !path.is_absolute() {
        return Err(PublishError::UnsafePath);
    }
    let class = if flags == 0 {
        FileRenameInfo
    } else {
        FileRenameInfoEx
    };
    let mut info = RenameBuffer {
        flags,
        root: null_mut(),
        length: 0,
        name: [0; 32768],
    };
    let units: Vec<_> = path.as_os_str().encode_wide().collect();
    if units.contains(&0) || units.len() >= info.name.len() {
        return Err(PublishError::UnsafePath);
    }
    info.name[..units.len()].copy_from_slice(&units);
    info.length = u32::try_from(units.len() * 2).map_err(|_| PublishError::UnsafePath)?;
    let used =
        (offset_of!(RenameBuffer, name) + units.len() * 2).max(size_of::<FILE_RENAME_INFO>());
    let size = u32::try_from(used).map_err(|_| PublishError::Unsupported)?;
    // SAFETY: the repr(C) header matches FILE_RENAME_INFO and the initialized tail contains the
    // absolute UTF-16 name. ReplaceIfExists is false. The DELETE handle and both parents stay live.
    let ok = unsafe {
        SetFileInformationByHandle(
            source.as_raw_handle(),
            class,
            (&raw const info).cast(),
            size,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error().into());
    }
    Ok(())
}

pub(super) fn sync_dir(_dir: &Dir) -> Result<()> {
    // NTFS journals name changes. There is no unprivileged directory FlushFileBuffers contract.
    Ok(())
}

pub(super) fn plain_payload(file: &File) -> Result<()> {
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_STREAM_INFO, FileStreamInfo, GetFileInformationByHandleEx,
    };
    #[repr(C)]
    struct Streams {
        next: u32,
        length: u32,
        size: i64,
        allocated: i64,
        name: [u16; 256],
    }
    const _: () = assert!(offset_of!(Streams, name) == offset_of!(FILE_STREAM_INFO, StreamName));
    let mut streams = Streams {
        next: 0,
        length: 0,
        size: 0,
        allocated: 0,
        name: [0; 256],
    };
    let size = u32::try_from(size_of::<Streams>()).map_err(|_| PublishError::Unsupported)?;
    // SAFETY: the initialized, aligned FILE_STREAM_INFO prefix and its tail stay writable for size bytes.
    let ok = unsafe {
        GetFileInformationByHandleEx(
            file.as_raw_handle(),
            FileStreamInfo,
            (&raw mut streams).cast(),
            size,
        )
    };
    if ok == 0 {
        let err = io::Error::last_os_error();
        return match err.raw_os_error() {
            Some(38) => Ok(()),
            Some(234) => Err(PublishError::Changed),
            _ => Err(err.into()),
        };
    }
    if streams.next != 0
        || !(streams.length == 0
            || (streams.length == 14 && streams.name[..7] == b"::$DATA".map(u16::from)))
    {
        return Err(PublishError::Changed);
    }
    Ok(())
}

fn delete_on_close(file: &File) -> Result<()> {
    use windows_sys::Win32::Storage::FileSystem::{FILE_DISPOSITION_INFO, FileDispositionInfo};
    let info = FILE_DISPOSITION_INFO { DeleteFile: true };
    let size =
        u32::try_from(size_of::<FILE_DISPOSITION_INFO>()).map_err(|_| PublishError::Unsupported)?;
    // SAFETY: the verified DELETE handle is live and info is an aligned FILE_DISPOSITION_INFO of size bytes.
    if unsafe {
        SetFileInformationByHandle(
            file.as_raw_handle(),
            FileDispositionInfo,
            (&raw const info).cast(),
            size,
        )
    } == 0
    {
        return Err(io::Error::last_os_error().into());
    }
    Ok(())
}

pub(super) fn remove_file(_dir: &Dir, _name: &str, file: &File) -> Result<()> {
    delete_on_close(file)
}

pub(super) fn remove_empty(dir: &Dir, name: &str, expected: &ObjectId) -> Result<()> {
    let file = open_directory(&dir.path().join(name), FILE_READ_ATTRIBUTES | DELETE)?;
    if identity(&file)? != *expected {
        return Err(PublishError::Changed);
    }
    plain_payload(&file)?;
    delete_on_close(&file)
}

pub(super) fn entries(dir: &Dir, limit: usize) -> Result<(Vec<(std::ffi::OsString, Kind)>, bool)> {
    use std::os::windows::fs::MetadataExt;
    let mut found = Vec::new();
    for entry in fs::read_dir(dir.path())? {
        let entry = entry?;
        if found.len() == limit {
            return Ok((found, true));
        }
        // The attributes come with the listing itself; nothing is opened and no link is followed.
        let attrs = entry.metadata()?.file_attributes();
        let kind = if attrs & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            Kind::Other
        } else if attrs & FILE_ATTRIBUTE_DIRECTORY != 0 {
            Kind::Dir
        } else {
            Kind::File
        };
        found.push((entry.file_name(), kind));
    }
    Ok((found, false))
}

pub(super) fn mkdir_shared(dir: &Dir, name: &str) -> Result<()> {
    mkdir(dir, name)
}

/// A sharing or lock violation clears by itself when the other process closes the file.
fn busy(err: PublishError) -> PublishError {
    match err {
        PublishError::Io(err) if matches!(err.raw_os_error(), Some(32 | 33)) => PublishError::Busy,
        other => other,
    }
}

/// Creates the new regular file `name` for a staged write, open so that it can be renamed through its handle.
pub(super) fn stage(dir: &Dir, name: &str) -> Result<File> {
    use windows_sys::Win32::Storage::FileSystem::{FILE_GENERIC_READ, FILE_GENERIC_WRITE};
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .access_mode(FILE_GENERIC_READ | FILE_GENERIC_WRITE | DELETE)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(dir.path().join(name))?;
    plain(&file, false)?;
    Ok(file)
}

/// Opens the regular file `name` for reading. With `hold` the handle is the one a replacement is checked against:
/// while it is open no process can write the file in place or replace it with a classic rename, and the atomic
/// rename of [`exchange_file`] still can.
pub(super) fn open_file(dir: &Dir, name: &str, hold: bool) -> Result<File> {
    use windows_sys::Win32::Storage::FileSystem::{FILE_GENERIC_READ, FILE_SHARE_DELETE};
    if !hold {
        return regular(dir, name, FileAccess::Read).map_err(busy);
    }
    let file = OpenOptions::new()
        .access_mode(FILE_GENERIC_READ)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(dir.path().join(name))
        .map_err(|err| busy(err.into()))?;
    plain(&file, false)?;
    Ok(file)
}

pub(super) fn try_lock(dir: &Dir, name: &str) -> Result<LockAttempt> {
    use std::os::windows::fs::MetadataExt;
    const GENERIC_READ: u32 = 0x8000_0000;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    const FILE_FLAG_DELETE_ON_CLOSE: u32 = 0x0400_0000;
    let path = dir.path().join(name);
    // The lock is an exclusive open that Windows deletes on close, so an entry that is not a plain file is
    // refused before it is opened that way.
    match fs::symlink_metadata(&path) {
        Ok(meta)
            if !meta.is_file() || meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 =>
        {
            return Err(PublishError::UnsafePath);
        }
        Ok(_) => {}
        Err(err) if err.kind() == io::ErrorKind::NotFound => {}
        Err(err) => return Err(err.into()),
    }
    let opened = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .access_mode(GENERIC_READ | GENERIC_WRITE | DELETE)
        .share_mode(0)
        .custom_flags(FILE_FLAG_DELETE_ON_CLOSE | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(&path);
    match opened {
        Ok(file) => {
            plain(&file, false)?;
            Ok(LockAttempt::Held(file))
        }
        // 32: another writer holds it. 5: the file its holder just closed is still "delete pending".
        Err(err) if matches!(err.raw_os_error(), Some(5 | 32)) => Ok(LockAttempt::Busy),
        Err(err) => Err(err.into()),
    }
}

pub(super) fn unlock(_dir: &Dir, _name: &str, file: File) -> Result<()> {
    drop(file);
    Ok(())
}

/// Gives the staged `file` the vacant name `name`; never replaces an entry.
pub(super) fn install_file(dir: &Dir, _staged: &str, file: &File, name: &str) -> Result<()> {
    rename_handle(file, &dir.path().join(name), 0)
}

fn links(file: &File) -> Result<u64> {
    Ok(winapi_util::file::information(file)?.number_of_links())
}

/// Gives the name `name`, which must still be the file `old` was opened from, to the staged `new` file in one atomic
/// rename: a reader finds the old file or the new one, never a missing name, also when this process stops
/// half-way. `false`: the old file is not moved aside, the rename removed its name.
///
/// `old` comes from [`open_file`] with `hold`, so its bytes cannot have changed, and a classic rename onto the name is
/// refused while it is open. Another program's own atomic rename or delete is not, so the name is compared with
/// `old` immediately before the rename and `old` must have lost its name after it.
pub(super) fn exchange_file(
    dir: &Dir,
    _staged: &str,
    new: &File,
    name: &str,
    old: &File,
) -> Result<bool> {
    use windows_sys::Win32::Storage::FileSystem::FILE_SHARE_DELETE;
    let target = dir.path().join(name);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    loop {
        let current = OpenOptions::new()
            .access_mode(FILE_READ_ATTRIBUTES)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&target)
            .map_err(|_| PublishError::Changed)?;
        if plain(&current, false).is_err()
            || identity(&current)? != identity(old)?
            || links(old)? != 1
        {
            return Err(PublishError::Changed);
        }
        drop(current);
        match rename_handle(new, &target, REPLACE_ATOMICALLY).map_err(busy) {
            Ok(()) => break,
            // A reader that does not share deletion has the file open; it closes in a moment.
            Err(PublishError::Busy) if std::time::Instant::now() < deadline => {
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(err) => return Err(err),
        }
    }
    if links(old)? != 0 {
        return Err(PublishError::MovedUnverified { path: target });
    }
    Ok(false)
}

/// Never reached on Windows: [`exchange_file`] does not move the old file aside, so there is nothing to undo.
pub(super) fn revert_file(
    dir: &Dir,
    _staged: &str,
    _new: &File,
    name: &str,
    _old: &File,
) -> Result<()> {
    Err(PublishError::MovedUnverified {
        path: dir.path().join(name),
    })
}

pub(super) fn publish_new(dir: &Dir, temp: &str, name: &str, data: &[u8]) -> Result<()> {
    use std::io::Write;
    use windows_sys::Win32::Storage::FileSystem::{FILE_GENERIC_READ, FILE_GENERIC_WRITE};

    // One handle creates, writes and renames the file, so nothing can replace it in between.
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .access_mode(FILE_GENERIC_READ | FILE_GENERIC_WRITE | DELETE)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(dir.path().join(temp))?;
    let target = dir.path().join(name);
    let landed = plain(&file, false)
        .and_then(|()| Ok(file.write_all(data)?))
        .and_then(|()| Ok(file.sync_all()?))
        .and_then(|()| rename_shared(&file, &target));
    if let Err(err) = landed {
        let _ = delete_on_close(&file);
        return Err(err);
    }
    Ok(())
}

/// [`rename_handle`] without replacement, retried for two seconds while another process (a scanner, an indexer)
/// briefly holds the new file open without sharing its name.
fn rename_shared(file: &File, target: &Path) -> Result<()> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    loop {
        match rename_handle(file, target, 0) {
            Err(PublishError::Io(err))
                if matches!(err.raw_os_error(), Some(5 | 32))
                    && std::time::Instant::now() < deadline =>
            {
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            outcome => return outcome,
        }
    }
}
