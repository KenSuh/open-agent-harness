// Windows 접근 목록 도우미: 현재 사용자 SID, 이 사용자만 허용하는 보호된 보안 설명자, 핸들로 읽은 소유자·접근 목록을 다룬다.
//! This user's SID, the owner-only descriptor of a private folder, and a folder's security read through its handle.

use super::super::io_error;
use super::super::rules::{ALLOW_ACE_TYPE, Entry, Security, Who};
use crate::fsx::error::FsxError;
use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::path::Path;
use std::ptr::null_mut;
use windows_sys::Win32::Foundation::LocalFree;
use windows_sys::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW, GetSecurityInfo,
    SDDL_REVISION_1, SE_FILE_OBJECT,
};
use windows_sys::Win32::Security::{
    ACCESS_ALLOWED_ACE, ACE_HEADER, ACL, DACL_SECURITY_INFORMATION, EqualSid, GetAce,
    GetSecurityDescriptorControl, GetTokenInformation, INHERIT_ONLY_ACE, IsWellKnownSid,
    OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID, SE_DACL_PRESENT, SE_DACL_PROTECTED,
    SECURITY_ATTRIBUTES, TOKEN_QUERY, TOKEN_USER, TokenUser, WinBuiltinAdministratorsSid,
    WinLocalSystemSid,
};
use windows_sys::Win32::Storage::FileSystem::CreateDirectoryW;
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

/// This process's user, as the `TOKEN_USER` its token returned; the SID lies inside the buffer.
pub(super) struct User {
    buffer: Vec<u64>,
}

impl User {
    pub(super) fn current() -> io::Result<User> {
        let mut raw = null_mut();
        // SAFETY: GetCurrentProcess returns a pseudo-handle; `raw` is writable.
        if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw) } == 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: the successful call handed over a token handle this code now owns.
        let token = unsafe { OwnedHandle::from_raw_handle(raw) };
        let mut buffer = vec![0_u64; 64];
        let (size, mut returned) = (u32::try_from(buffer.len() * 8).unwrap_or(u32::MAX), 0);
        // SAFETY: the buffer is writable, 8-byte aligned and `size` bytes long.
        let read = unsafe {
            GetTokenInformation(
                token.as_raw_handle(),
                TokenUser,
                buffer.as_mut_ptr().cast(),
                size,
                &mut returned,
            )
        };
        if read == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(User { buffer })
    }

    fn sid(&self) -> PSID {
        // SAFETY: GetTokenInformation wrote a TOKEN_USER at the start of the aligned buffer.
        unsafe { (*self.buffer.as_ptr().cast::<TOKEN_USER>()).User.Sid }
    }

    fn who(&self, sid: PSID) -> Who {
        // SAFETY: both SIDs are valid for the duration of the calls.
        unsafe {
            if EqualSid(sid, self.sid()) != 0 {
                Who::User
            } else if IsWellKnownSid(sid, WinLocalSystemSid) != 0
                || IsWellKnownSid(sid, WinBuiltinAdministratorsSid) != 0
            {
                Who::Trusted
            } else {
                Who::Other
            }
        }
    }
}

/// A security descriptor allocated by Windows and freed on drop.
pub(super) struct Descriptor(PSECURITY_DESCRIPTOR);

impl Descriptor {
    /// Owner `user`, and a protected access list whose one entry allows `user` everything, inherited by files and
    /// folders created inside.
    pub(super) fn only(user: &User) -> io::Result<Descriptor> {
        let mut text = null_mut();
        // SAFETY: the SID is valid; Windows allocates the string, freed below.
        if unsafe { ConvertSidToStringSidW(user.sid(), &mut text) } == 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: a successful conversion returns a NUL-terminated string.
        let sid = OsString::from_wide(unsafe { until_nul(text) });
        // SAFETY: frees the string Windows allocated, exactly once.
        unsafe { LocalFree(text.cast()) };
        let mut sddl = OsString::from("O:");
        for part in [
            sid.as_os_str(),
            OsStr::new("D:P(A;OICI;FA;;;"),
            sid.as_os_str(),
            OsStr::new(")"),
        ] {
            sddl.push(part);
        }
        let wide: Vec<u16> = sddl.encode_wide().chain(Some(0)).collect();
        let mut descriptor = null_mut();
        // SAFETY: `wide` is NUL-terminated and `descriptor` is writable; Windows allocates the descriptor.
        let made = unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                wide.as_ptr(),
                SDDL_REVISION_1,
                &mut descriptor,
                null_mut(),
            )
        };
        if made == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Descriptor(descriptor))
    }

    /// Creates the folder `path` with this descriptor; an existing name is `ERROR_ALREADY_EXISTS`.
    pub(super) fn create_directory(&self, path: &Path) -> io::Result<()> {
        let attributes = SECURITY_ATTRIBUTES {
            nLength: u32::try_from(size_of::<SECURITY_ATTRIBUTES>()).unwrap_or(u32::MAX),
            lpSecurityDescriptor: self.0,
            bInheritHandle: 0,
        };
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        // SAFETY: the path is NUL-terminated, and the attributes and descriptor live until the call returns.
        if unsafe { CreateDirectoryW(wide.as_ptr(), &attributes) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }
}

impl Drop for Descriptor {
    fn drop(&mut self) {
        // SAFETY: frees the descriptor Windows allocated, exactly once.
        unsafe { LocalFree(self.0) };
    }
}

/// The owner and access list of the folder open as `folder`, read through that handle.
pub(super) fn read_security(folder: &File, path: &Path, user: &User) -> Result<Security, FsxError> {
    let (mut owner, mut dacl, mut descriptor) = (null_mut(), null_mut(), null_mut());
    // SAFETY: the handle is live and has READ_CONTROL; every output is writable. Windows allocates the descriptor,
    // and `owner` and `dacl` point into it.
    let status = unsafe {
        GetSecurityInfo(
            folder.as_raw_handle(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            &mut dacl,
            null_mut(),
            &mut descriptor,
        )
    };
    if status != 0 {
        let error = io::Error::from_raw_os_error(i32::try_from(status).unwrap_or(i32::MAX));
        return Err(io_error(
            path,
            "cannot read the folder's owner and access list",
            error,
        ));
    }
    // SAFETY: the pointers come from the successful call above and stay valid until LocalFree.
    let security = unsafe { describe(owner, dacl, descriptor, user) };
    // SAFETY: frees the descriptor GetSecurityInfo allocated, exactly once.
    unsafe { LocalFree(descriptor) };
    security.ok_or_else(|| {
        let error = io::Error::last_os_error();
        io_error(path, "cannot read the folder's access list", error)
    })
}

/// # Safety
/// `owner`, `dacl` and `descriptor` come from one successful `GetSecurityInfo` call whose descriptor is still
/// allocated.
unsafe fn describe(
    owner: PSID,
    dacl: *mut ACL,
    descriptor: PSECURITY_DESCRIPTOR,
    user: &User,
) -> Option<Security> {
    let (mut control, mut revision) = (0_u16, 0_u32);
    // SAFETY: the descriptor is live; both outputs are writable.
    if unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) } == 0 {
        return None;
    }
    // SAFETY: a non-null access list is an ACL inside the live descriptor.
    let count = if dacl.is_null() {
        0
    } else {
        unsafe { (*dacl).AceCount }
    };
    let mut entries = Vec::with_capacity(usize::from(count));
    for index in 0..u32::from(count) {
        let mut ace = null_mut();
        // SAFETY: `index` is below the entry count; `ace` is writable.
        if unsafe { GetAce(dacl, index, &mut ace) } == 0 {
            return None;
        }
        // SAFETY: every entry GetAce returns starts with an ACE_HEADER.
        let header = unsafe { *ace.cast::<ACE_HEADER>() };
        let who = if header.AceType == ALLOW_ACE_TYPE {
            // SAFETY: a plain allowing entry is an ACCESS_ALLOWED_ACE whose SID starts at `SidStart`.
            user.who(unsafe { (&raw mut (*ace.cast::<ACCESS_ALLOWED_ACE>()).SidStart).cast() })
        } else {
            Who::Other
        };
        let inherit_only = u32::from(header.AceFlags) & INHERIT_ONLY_ACE != 0;
        entries.push(Entry {
            kind: header.AceType,
            inherit_only,
            who,
        });
    }
    Some(Security {
        owner: if owner.is_null() {
            Who::Other
        } else {
            user.who(owner)
        },
        dacl_present: !dacl.is_null() && control & SE_DACL_PRESENT != 0,
        protected: control & SE_DACL_PROTECTED != 0,
        entries,
    })
}

/// The UTF-16 code units before the terminating NUL.
///
/// # Safety
/// `wide` points to a live NUL-terminated UTF-16 string.
pub(super) unsafe fn until_nul<'a>(wide: *const u16) -> &'a [u16] {
    let mut len = 0;
    // SAFETY: the string is NUL-terminated, so every unit up to the terminator is readable.
    while unsafe { wide.add(len).read() } != 0 {
        len += 1;
    }
    // SAFETY: the `len` units before the terminator are initialized.
    unsafe { std::slice::from_raw_parts(wide, len) }
}
