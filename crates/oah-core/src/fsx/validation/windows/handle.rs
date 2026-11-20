// Windows 폴더 핸들 도우미: 비재해석 열기, 핸들 기반 최종 경로 조회, 고정 드라이브 검사, 핸들 기반 삭제 표시를 제공한다.
//! Handle-bound Windows folder, volume, and delete-on-close helpers.

use super::super::windows_rules::{MAX_WIDE_UNITS, windows_drive_root_units};
use super::super::{io_error, refuse};
use crate::fsx::error::FsxError;
use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io;
use std::mem::size_of;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Component, Path, PathBuf, Prefix};
use std::time::{Duration, Instant};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_DISPOSITION_INFO,
    FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_NAME_NORMALIZED,
    FILE_READ_ATTRIBUTES, FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_TRAVERSE, FileDispositionInfo,
    GetDriveTypeW, GetFinalPathNameByHandleW, SYNCHRONIZE, SetFileInformationByHandle,
    VOLUME_NAME_DOS,
};

/// How long a removal retries while Windows still reports the copy busy or the folder not yet empty.
const REMOVE_FOR: Duration = Duration::from_secs(2);
/// What `GetDriveTypeW` returns for a fixed local drive, as opposed to a removable, network, optical or RAM drive.
const DRIVE_FIXED: u32 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct WinId {
    pub volume: u64,
    pub index: u64,
}

impl WinId {
    pub(super) fn of(info: &winapi_util::file::Information) -> Self {
        Self {
            volume: info.volume_serial_number(),
            index: info.file_index(),
        }
    }
}

/// Opens the folder entry itself, never a reparse point's target, sharing read, write and `share`, and requires a
/// plain folder. Returns the handle and its volume/file identity.
pub(super) fn open_folder(path: &Path, access: u32, share: u32) -> Result<(File, WinId), FsxError> {
    let folder = OpenOptions::new()
        .access_mode(access | FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | share)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|error| io_error(path, "cannot open the folder", error))?;
    let info = winapi_util::file::information(&folder)
        .map_err(|error| io_error(path, "cannot inspect", error))?;
    if !is_plain_dir(info.file_attributes()) {
        return Err(refuse(path, "it is a reparse point or not a folder"));
    }
    Ok((folder, WinId::of(&info)))
}

/// Resolves the normalized final DOS path directly from an already-open verified handle.
pub(super) fn final_path_by_handle(handle: &File, path: &Path) -> Result<PathBuf, FsxError> {
    let flags = VOLUME_NAME_DOS | FILE_NAME_NORMALIZED;
    let mut buf = vec![0_u16; 260];
    for _ in 0..3 {
        let cap = u32::try_from(buf.len()).unwrap_or(u32::MAX);
        // SAFETY: `handle` is live and `buf` is writable for `cap` UTF-16 code units.
        let len = unsafe {
            GetFinalPathNameByHandleW(handle.as_raw_handle(), buf.as_mut_ptr(), cap, flags)
        };
        if len == 0 {
            return Err(io_error(
                path,
                "cannot resolve the final path of the folder handle",
                io::Error::last_os_error(),
            ));
        }
        let len_usize = usize::try_from(len).unwrap_or(usize::MAX);
        if len_usize < buf.len() {
            if buf[len_usize] != 0 || buf[..len_usize].contains(&0) {
                return Err(io_error(
                    path,
                    "cannot resolve the final path of the folder handle",
                    io::Error::new(io::ErrorKind::InvalidData, "malformed final path buffer"),
                ));
            }
            return Ok(PathBuf::from(OsString::from_wide(&buf[..len_usize])));
        }
        if len_usize > MAX_WIDE_UNITS {
            return Err(refuse(path, "the resolved folder path is too long"));
        }
        buf.resize(len_usize.saturating_add(1), 0);
    }
    Err(refuse(path, "the resolved folder path kept changing"))
}

/// Requires `path` to have both a disk or verbatim-disk prefix and `RootDir`, and queries `GetDriveTypeW` with an
/// explicit normal `<DRIVE>:\` root string.
pub(super) fn require_fixed_drive(path: &Path) -> Result<(), FsxError> {
    let mut components = path.components();
    let parsed_letter = match (components.next(), components.next()) {
        (Some(Component::Prefix(prefix)), Some(Component::RootDir)) => match prefix.kind() {
            Prefix::Disk(letter) | Prefix::VerbatimDisk(letter) => {
                Some(letter.to_ascii_uppercase())
            }
            _ => None,
        },
        _ => None,
    };
    let units: Vec<u16> = path.as_os_str().encode_wide().collect();
    let root = windows_drive_root_units(&units)
        .filter(|root| parsed_letter.is_some_and(|letter| root[0] == u16::from(letter)));
    let Some(root) = root else {
        return Err(refuse(
            path,
            "the private check location is not on a fixed local drive",
        ));
    };
    // SAFETY: `root` is a 4-unit NUL-terminated `<DRIVE>:\` UTF-16 buffer alive for the call.
    if unsafe { GetDriveTypeW(root.as_ptr()) } != DRIVE_FIXED {
        return Err(refuse(
            path,
            "the private check location is not on a fixed local drive",
        ));
    }
    Ok(())
}

/// Marks an already-verified handle opened with `DELETE` access for deletion on close.
pub(super) fn mark_delete_on_close(handle: &File) -> io::Result<()> {
    let info = FILE_DISPOSITION_INFO { DeleteFile: true };
    let size = u32::try_from(size_of::<FILE_DISPOSITION_INFO>()).unwrap_or(u32::MAX);
    // SAFETY: `handle` is live with `DELETE` access and `info` is an aligned `FILE_DISPOSITION_INFO`.
    if unsafe {
        SetFileInformationByHandle(
            handle.as_raw_handle(),
            FileDispositionInfo,
            (&raw const info).cast(),
            size,
        )
    } == 0
    {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

pub(super) fn is_plain_dir(attributes: u64) -> bool {
    let dir = u64::from(FILE_ATTRIBUTE_DIRECTORY);
    let reparse = u64::from(FILE_ATTRIBUTE_REPARSE_POINT);
    attributes & dir != 0 && attributes & reparse == 0
}

pub(super) fn is_plain_file(attributes: u64) -> bool {
    let dir = u64::from(FILE_ATTRIBUTE_DIRECTORY);
    let reparse = u64::from(FILE_ATTRIBUTE_REPARSE_POINT);
    attributes & dir == 0 && attributes & reparse == 0
}

pub(super) fn retry_removal(mut remove: impl FnMut() -> io::Result<()>) -> io::Result<()> {
    let deadline = Instant::now() + REMOVE_FOR;
    loop {
        match remove() {
            // ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION and ERROR_DIR_NOT_EMPTY.
            Err(error)
                if matches!(error.raw_os_error(), Some(5 | 32 | 33 | 145))
                    && Instant::now() < deadline =>
            {
                std::thread::sleep(Duration::from_millis(50));
            }
            other => return other,
        }
    }
}
