// Windows 명시적 소유자 보안 설명자 경계: 비공개 폴더와 사본 파일을 모두 현재 사용자 SID 소유로 배타 생성한다.
//! Explicit-owner Windows security descriptor and exclusive directory/file creation boundary.

use super::super::windows_rules::{DescriptorTarget, private_sddl_units};
use super::ffi::{LocalMem, User, invalid_data};
use std::fs::File;
use std::io;
use std::mem::size_of;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::FromRawHandle;
use std::path::Path;
use std::ptr::null_mut;
use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
use windows_sys::Win32::Security::Authorization::{
    ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
};
use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
use windows_sys::Win32::Storage::FileSystem::{
    CREATE_NEW, CreateDirectoryW, CreateFileW, FILE_ATTRIBUTE_NORMAL, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_GENERIC_WRITE, FILE_READ_ATTRIBUTES, READ_CONTROL,
};

/// A security descriptor allocated by Windows and freed on drop, bound to one creation target kind.
pub(super) struct Descriptor {
    mem: LocalMem,
    target: DescriptorTarget,
}

impl Descriptor {
    /// Owner `user`, and a protected access list whose one entry allows `user` full access, inherited by children.
    pub(super) fn for_directory(user: &User) -> io::Result<Self> {
        Self::build(user, DescriptorTarget::Directory)
    }

    /// Owner `user`, and a protected access list whose one entry allows `user` full access on the file itself.
    pub(super) fn for_file(user: &User) -> io::Result<Self> {
        Self::build(user, DescriptorTarget::File)
    }

    fn build(user: &User, target: DescriptorTarget) -> io::Result<Self> {
        let sid_units = user.sid_units()?;
        let wide = private_sddl_units(&sid_units, target)
            .ok_or_else(|| invalid_data("cannot format a valid private SDDL descriptor"))?;
        let mut raw = null_mut();
        // SAFETY: `wide` is NUL-terminated and `raw` is writable; Windows allocates the descriptor.
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                wide.as_ptr(),
                SDDL_REVISION_1,
                &mut raw,
                null_mut(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let mem = LocalMem::new(raw).ok_or_else(|| {
            invalid_data("ConvertStringSecurityDescriptorToSecurityDescriptorW returned null")
        })?;
        Ok(Self { mem, target })
    }

    fn security_attributes(&self) -> SECURITY_ATTRIBUTES {
        SECURITY_ATTRIBUTES {
            nLength: u32::try_from(size_of::<SECURITY_ATTRIBUTES>()).unwrap_or(u32::MAX),
            lpSecurityDescriptor: self.mem.as_ptr(),
            bInheritHandle: 0,
        }
    }

    /// Creates the folder `path` with explicit current-user ownership; an existing name is `ERROR_ALREADY_EXISTS`.
    pub(super) fn create_directory(&self, path: &Path) -> io::Result<()> {
        if self.target != DescriptorTarget::Directory {
            return Err(invalid_data(
                "wrong descriptor target for directory creation",
            ));
        }
        let wide = encode_wide_path(path)?;
        let attributes = self.security_attributes();
        // SAFETY: `wide` is NUL-terminated, and `attributes` and `self.mem` live until the call returns.
        if unsafe { CreateDirectoryW(wide.as_ptr(), &attributes) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    /// Exclusively creates the private copy `path` with explicit current-user ownership, open-final-entry semantics,
    /// and handle rights sufficient for writing, identity inspection, and read-only owner verification.
    pub(super) fn create_file(&self, path: &Path) -> io::Result<File> {
        if self.target != DescriptorTarget::File {
            return Err(invalid_data("wrong descriptor target for file creation"));
        }
        let wide = encode_wide_path(path)?;
        let attributes = self.security_attributes();
        let access = FILE_GENERIC_WRITE | FILE_READ_ATTRIBUTES | READ_CONTROL;
        let flags = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT;
        // SAFETY: `wide` is NUL-terminated, and `attributes` and `self.mem` live until the call returns.
        let raw = unsafe {
            CreateFileW(
                wide.as_ptr(),
                access,
                0,
                &attributes,
                CREATE_NEW,
                flags,
                null_mut(),
            )
        };
        if raw == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        if raw.is_null() {
            return Err(invalid_data("CreateFileW returned a null handle"));
        }
        // SAFETY: non-null, non-invalid file handle transferred by a successful `CreateFileW`.
        Ok(unsafe { File::from_raw_handle(raw) })
    }
}

fn encode_wide_path(path: &Path) -> io::Result<Vec<u16>> {
    let mut wide: Vec<u16> = path.as_os_str().encode_wide().collect();
    if wide.contains(&0) {
        return Err(invalid_data("path contains an embedded NUL"));
    }
    wide.push(0);
    Ok(wide)
}
