// Windows 비공개 확인 폴더: 셸이 정한 로컬 앱 데이터 Temp 를 확인하고, 이 사용자만 허용하는 보호된 접근 목록으로 새 폴더와 사본을 만들고 끝나면 지운다.
//! The Windows side of [`super::validate_private_copy`].

mod acl;

use super::rules::{private_folder_rule, trusted_location_rule};
use super::{NAME_ATTEMPTS, io_error, refuse, unique_name};
use crate::fsx::error::FsxError;
use acl::{Descriptor, User};
use std::ffi::{OsStr, OsString};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf, Prefix};
use std::ptr::null_mut;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::ERROR_ALREADY_EXISTS;
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
    FILE_FLAG_OPEN_REPARSE_POINT, FILE_LIST_DIRECTORY, FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE,
    FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_TRAVERSE, GetDriveTypeW, READ_CONTROL, SYNCHRONIZE,
};
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::UI::Shell::{FOLDERID_LocalAppData, KF_FLAG_DEFAULT, SHGetKnownFolderPath};

/// How long a removal retries while Windows still reports the copy busy or the folder not yet empty.
const REMOVE_FOR: Duration = Duration::from_secs(2);
/// What `GetDriveTypeW` returns for a fixed local drive, as opposed to a removable, network, optical or RAM drive.
const DRIVE_FIXED: u32 = 3;

/// `Temp` in the user's local application data folder, as the shell's known-folder list names it. Both folders are
/// checked as named, before anything resolves them: a folder on a fixed local drive, not a reparse point, owned by
/// this user, SYSTEM or Administrators, and usable by nobody else.
pub(super) fn location() -> Result<PathBuf, FsxError> {
    let mut wide = null_mut();
    // SAFETY: `wide` is writable; the shell allocates the path, which is freed below whatever the result.
    let status = unsafe {
        SHGetKnownFolderPath(
            &FOLDERID_LocalAppData,
            KF_FLAG_DEFAULT as u32,
            null_mut(),
            &mut wide,
        )
    };
    let local = (status >= 0 && !wide.is_null()).then(|| {
        // SAFETY: a successful call returns a NUL-terminated string.
        PathBuf::from(OsString::from_wide(unsafe { acl::until_nul(wide) }))
    });
    // SAFETY: frees what the shell allocated (null is allowed), exactly once.
    unsafe { CoTaskMemFree(wide.cast_const().cast()) };
    let local = local.ok_or_else(|| {
        let error = io::Error::other(format!("SHGetKnownFolderPath returned 0x{status:08x}"));
        let known = Path::new("FOLDERID_LocalAppData");
        io_error(
            known,
            "cannot find the local application data folder",
            error,
        )
    })?;
    let drive_letter = matches!(local.components().next(), Some(Component::Prefix(prefix))
        if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_)));
    let root: PathBuf = local.components().take(2).collect();
    let wide: Vec<u16> = root.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: the root path is NUL-terminated and lives until the call returns.
    if !drive_letter || unsafe { GetDriveTypeW(wide.as_ptr()) } != DRIVE_FIXED {
        let why = "the local application data folder is not on a fixed local drive";
        return Err(refuse(&local, why));
    }
    let user = User::current().map_err(|error| io_error(&local, "cannot read this user", error))?;
    let temp = local.join("Temp");
    for folder in [&local, &temp] {
        let held = open_folder(folder, READ_CONTROL, FILE_SHARE_DELETE)?;
        let security = acl::read_security(&held, folder, &user)?;
        trusted_location_rule(&security).map_err(|why| refuse(folder, why))?;
    }
    Ok(temp)
}

/// A new private folder this start created, held open without delete sharing, and the copy once written. Dropping
/// it removes both.
pub(super) struct Private {
    path: PathBuf,
    folder: Option<File>,
    copy: Option<PathBuf>,
}

impl Private {
    /// Creates a new folder in `base` whose owner is this user and whose protected access list allows only this
    /// user, then reads both back through the handle it keeps.
    pub(super) fn create(base: &Path) -> Result<Private, FsxError> {
        let user =
            User::current().map_err(|error| io_error(base, "cannot read this user", error))?;
        let descriptor = Descriptor::only(&user)
            .map_err(|error| io_error(base, "cannot describe a private folder", error))?;
        for _ in 0..NAME_ATTEMPTS {
            let path = base.join(unique_name());
            match descriptor.create_directory(&path) {
                Ok(()) => {}
                Err(error) if error.raw_os_error() == Some(ERROR_ALREADY_EXISTS as i32) => continue,
                Err(error) => {
                    return Err(io_error(&path, "cannot create the private folder", error));
                }
            }
            // Listing access counts as read access for sharing, so the missing delete sharing takes effect: while
            // the handle is open, nobody can rename or remove the folder.
            let folder = match open_folder(&path, FILE_LIST_DIRECTORY | READ_CONTROL, 0) {
                Ok(folder) => folder,
                Err(error) => {
                    let _ = fs::remove_dir(&path);
                    return Err(error);
                }
            };
            // A folder that fails here is not the one this call made, so it is left alone.
            let security = acl::read_security(&folder, &path, &user)?;
            private_folder_rule(&security).map_err(|why| refuse(&path, why))?;
            return Ok(Private {
                path,
                folder: Some(folder),
                copy: None,
            });
        }
        Err(refuse(base, "no unused private folder name was found"))
    }

    /// Creates the copy exclusively, writes it and closes it before anything runs. It inherits the folder's access.
    pub(super) fn write(&mut self, name: &OsStr, bytes: &[u8]) -> Result<PathBuf, FsxError> {
        let path = self.path.join(name);
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|error| io_error(&path, "cannot create the private copy", error))?;
        self.copy = Some(path.clone());
        file.write_all(bytes)
            .map_err(|error| io_error(&path, "cannot write the private copy", error))?;
        Ok(path)
    }
}

impl Drop for Private {
    fn drop(&mut self) {
        if let Some(copy) = self.copy.take() {
            // A program that just exited can keep its image mapped for a moment.
            let _ = retry_removal(|| fs::remove_file(&copy));
        }
        self.folder = None;
        let _ = retry_removal(|| fs::remove_dir(&self.path));
    }
}

/// Opens the folder entry itself, never a reparse point's target, sharing read, write and `share`, and requires a
/// plain folder.
fn open_folder(path: &Path, access: u32, share: u32) -> Result<File, FsxError> {
    let folder = OpenOptions::new()
        .access_mode(access | FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | share)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|error| io_error(path, "cannot open the folder", error))?;
    let attributes = winapi_util::file::information(&folder)
        .map_err(|error| io_error(path, "cannot inspect", error))?
        .file_attributes();
    let directory = u64::from(FILE_ATTRIBUTE_DIRECTORY);
    let reparse = u64::from(FILE_ATTRIBUTE_REPARSE_POINT);
    if attributes & directory == 0 || attributes & reparse != 0 {
        return Err(refuse(path, "it is a reparse point or not a folder"));
    }
    Ok(folder)
}

fn retry_removal(mut remove: impl FnMut() -> io::Result<()>) -> io::Result<()> {
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
