// Windows 비공개 확인 폴더: 셸이 정한 로컬 앱 데이터 Temp 의 핸들에서 최종 경로·고정 볼륨·소유자·접근 목록을 확인해 유지하고, 신원이 확인된 항목만 핸들로 정리한다.
//! The Windows side of [`super::validate_private_copy`].

mod acl;
mod descriptor;
mod ffi;
mod handle;

use super::rules::{CleanupCheck, Who, may_cleanup, private_folder_rule, trusted_location_rule};
use super::{NAME_ATTEMPTS, io_error, refuse, unique_name};
use crate::fsx::error::FsxError;
use descriptor::Descriptor;
use ffi::User;
use handle::{
    WinId, final_path_by_handle, is_plain_dir, is_plain_file, mark_delete_on_close, open_folder,
    require_fixed_drive, retry_removal,
};
use std::ffi::OsStr;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::os::windows::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use windows_sys::Win32::Foundation::ERROR_ALREADY_EXISTS;
use windows_sys::Win32::Storage::FileSystem::{
    DELETE, FILE_FLAG_OPEN_REPARSE_POINT, FILE_LIST_DIRECTORY, FILE_READ_ATTRIBUTES,
    FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, READ_CONTROL, SYNCHRONIZE,
};

/// Verified Windows location: retains the opened `Temp` handle (and `LocalAppData` handle when selected via the
/// shell), the handle-resolved canonical final path on a fixed local drive, its volume serial, and the current user.
pub(super) struct Location {
    path: PathBuf,
    _handle: File,
    _parent: Option<File>,
    volume: u64,
    user: User,
}

impl Location {
    pub(super) fn path(&self) -> &Path {
        &self.path
    }
}

/// `Temp` in the user's local application data folder, as the shell's known-folder list names it. Both folders are
/// opened as named with `FILE_FLAG_OPEN_REPARSE_POINT`, checked for owner and DACL, resolved via
/// `GetFinalPathNameByHandleW`, and confirmed to lie on a fixed local drive. The verified handles are kept open.
pub(super) fn location() -> Result<Location, FsxError> {
    let local = ffi::local_app_data()?;
    require_fixed_drive(&local)?;
    let user = User::current().map_err(|error| io_error(&local, "cannot read this user", error))?;
    let temp = local.join("Temp");
    let (local_handle, _) = open_folder(&local, READ_CONTROL, FILE_SHARE_DELETE)?;
    let local_security = acl::read_security(&local_handle, &local, &user)?;
    trusted_location_rule(&local_security).map_err(|why| refuse(&local, why))?;
    let local_final = final_path_by_handle(&local_handle, &local)?;
    require_fixed_drive(&local_final)?;

    let (temp_handle, temp_id) = open_folder(&temp, READ_CONTROL, FILE_SHARE_DELETE)?;
    let temp_security = acl::read_security(&temp_handle, &temp, &user)?;
    trusted_location_rule(&temp_security).map_err(|why| refuse(&temp, why))?;
    let temp_final = final_path_by_handle(&temp_handle, &temp)?;
    require_fixed_drive(&temp_final)?;
    Ok(Location {
        path: temp_final,
        _handle: temp_handle,
        _parent: Some(local_handle),
        volume: temp_id.volume,
        user,
    })
}

/// Opens an explicit test directory without following reparse points, binds its handle-resolved canonical path,
/// and checks that the resolved volume is a fixed local drive.
#[cfg(test)]
pub(super) fn open_base_for_test(base: &Path) -> Result<Location, FsxError> {
    if !base.is_absolute() {
        return Err(refuse(
            base,
            "the private check location is not an absolute path",
        ));
    }
    let user = User::current().map_err(|error| io_error(base, "cannot read this user", error))?;
    let (handle, id) = open_folder(base, READ_CONTROL, FILE_SHARE_DELETE)?;
    let final_path = final_path_by_handle(&handle, base)?;
    require_fixed_drive(&final_path)?;
    Ok(Location {
        path: final_path,
        _handle: handle,
        _parent: None,
        volume: id.volume,
        user,
    })
}

struct VerifiedCopy {
    path: PathBuf,
    id: WinId,
}

/// A new private folder this start created, held open with `DELETE` and without delete sharing, along with the
/// anchoring location handle and the copy once written. Dropping it removes only entries whose verified type,
/// owner and identity remain intact.
pub(super) struct Private {
    path: PathBuf,
    folder: Option<File>,
    base: Location,
    dir_id: WinId,
    copy: Option<VerifiedCopy>,
}

impl Private {
    /// Creates a new folder inside the verified `base` whose owner is this user and whose protected access list
    /// allows only this user, then reads both back through the handle it keeps. Unopened or unverified entries are
    /// left untouched; an entry whose owner and directory identity are proven but whose ACL policy is insufficient
    /// is deleted through its verified handle.
    pub(super) fn create(base: Location) -> Result<Private, FsxError> {
        let descriptor = Descriptor::for_directory(&base.user)
            .map_err(|error| io_error(base.path(), "cannot describe a private folder", error))?;
        for _ in 0..NAME_ATTEMPTS {
            let path = base.path().join(unique_name());
            match descriptor.create_directory(&path) {
                Ok(()) => {}
                Err(error) if error.raw_os_error() == Some(ERROR_ALREADY_EXISTS as i32) => continue,
                Err(error) => {
                    return Err(io_error(&path, "cannot create the private folder", error));
                }
            }
            // If the post-create open fails, entry type and owner are unproven: leave untouched.
            // Listing access counts as read access for sharing, so omitting FILE_SHARE_DELETE prevents others from
            // renaming or removing the folder while we hold `DELETE` for verified handle-based cleanup.
            let (folder, dir_id) =
                open_folder(&path, FILE_LIST_DIRECTORY | READ_CONTROL | DELETE, 0)?;
            let resolved = final_path_by_handle(&folder, &path)?;
            require_fixed_drive(&resolved)?;
            if dir_id.volume != base.volume || resolved.parent() != Some(base.path()) {
                return Err(refuse(
                    &path,
                    "the new private folder resolved outside the verified location",
                ));
            }
            let security = acl::read_security(&folder, &path, &base.user)?;
            if let Err(why) = private_folder_rule(&security) {
                if security.owner == Who::User {
                    cleanup_verified_dir(&folder, &path, dir_id, &base.user);
                }
                return Err(refuse(&path, why));
            }
            return Ok(Private {
                path,
                folder: Some(folder),
                base,
                dir_id,
                copy: None,
            });
        }
        Err(refuse(
            base.path(),
            "no unused private folder name was found",
        ))
    }

    /// Exclusively creates the copy with explicit current-user ownership and open-final-entry semantics, verifies
    /// its plain-file identity and owner through that same handle before writing, then writes and closes it.
    pub(super) fn write(&mut self, name: &OsStr, bytes: &[u8]) -> Result<PathBuf, FsxError> {
        let path = self.path.join(name);
        let descriptor = Descriptor::for_file(&self.base.user)
            .map_err(|error| io_error(&path, "cannot describe the private copy", error))?;
        let mut file = descriptor
            .create_file(&path)
            .map_err(|error| io_error(&path, "cannot create the private copy", error))?;
        let info = winapi_util::file::information(&file)
            .map_err(|error| io_error(&path, "cannot inspect the private copy", error))?;
        let id = WinId::of(&info);
        if !is_plain_file(info.file_attributes()) || id.volume != self.dir_id.volume {
            return Err(refuse(
                &path,
                "the new private copy is not a plain file on the verified volume",
            ));
        }
        if acl::read_owner(&file, &path, &self.base.user)? != Who::User {
            return Err(refuse(
                &path,
                "the new private copy is not owned by this user",
            ));
        }
        self.copy = Some(VerifiedCopy {
            path: path.clone(),
            id,
        });
        file.write_all(bytes)
            .map_err(|error| io_error(&path, "cannot write the private copy", error))?;
        Ok(path)
    }
}

/// Verifies on Windows that the staged folder and copy are owned by the current user with protected user-only
/// access, read through opened no-reparse handles.
#[cfg(test)]
pub(super) fn assert_private_copy_owner(folder: &Path, copy: &Path) {
    let user = User::current().unwrap();
    let (dir, _) = open_folder(folder, READ_CONTROL, FILE_SHARE_DELETE).unwrap();
    private_folder_rule(&acl::read_security(&dir, folder, &user).unwrap()).unwrap();
    let file = OpenOptions::new()
        .access_mode(READ_CONTROL | FILE_READ_ATTRIBUTES | SYNCHRONIZE)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(copy)
        .unwrap();
    let info = winapi_util::file::information(&file).unwrap();
    assert!(is_plain_file(info.file_attributes()));
    private_folder_rule(&acl::read_security(&file, copy, &user).unwrap()).unwrap();
}

impl Drop for Private {
    fn drop(&mut self) {
        if let Some(copy) = self.copy.take() {
            let _ = retry_removal(|| remove_verified_copy(&copy, &self.base.user));
        }
        if let Some(folder) = self.folder.take() {
            cleanup_verified_dir(&folder, &self.path, self.dir_id, &self.base.user);
        }
    }
}

/// Opens `copy.path` without following reparse points, verifies it is still the same plain file owned by `user`,
/// and marks that handle for deletion on close. Unverified entries are left untouched.
fn remove_verified_copy(copy: &VerifiedCopy, user: &User) -> io::Result<()> {
    let file = match OpenOptions::new()
        .access_mode(DELETE | READ_CONTROL | FILE_READ_ATTRIBUTES | SYNCHRONIZE)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(&copy.path)
    {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let check = match (
        winapi_util::file::information(&file),
        acl::read_owner(&file, &copy.path, user),
    ) {
        (Ok(info), Ok(owner)) => CleanupCheck {
            opened: true,
            expected_type: is_plain_file(info.file_attributes()),
            owner_is_user: owner == Who::User,
            identity_intact: WinId::of(&info) == copy.id,
        },
        _ => CleanupCheck::unverified(),
    };
    if may_cleanup(check) {
        mark_delete_on_close(&file)
    } else {
        Ok(())
    }
}

/// Re-verifies the already-open directory handle (`plain directory`, `owner == Who::User`, `WinId == expected`)
/// and marks that exact handle for deletion on close. Unverified handles are left untouched.
fn cleanup_verified_dir(folder: &File, path: &Path, expected: WinId, user: &User) {
    let check = match (
        winapi_util::file::information(folder),
        acl::read_owner(folder, path, user),
    ) {
        (Ok(info), Ok(owner)) => CleanupCheck {
            opened: true,
            expected_type: is_plain_dir(info.file_attributes()),
            owner_is_user: owner == Who::User,
            identity_intact: WinId::of(&info) == expected,
        },
        _ => CleanupCheck::unverified(),
    };
    if may_cleanup(check) {
        let _ = retry_removal(|| mark_delete_on_close(folder));
    }
}
