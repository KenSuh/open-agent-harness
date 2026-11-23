use super::native::{Dir, FileAccess};
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
    FILE_SHARE_READ, FILE_SHARE_WRITE, FileRenameInfo, GetDriveTypeW,
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
    let mut info = RenameBuffer {
        flags: 0,
        root: null_mut(),
        length: 0,
        name: [0; 32768],
    };
    let path = dest.path().join(to);
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
            FileRenameInfo,
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
