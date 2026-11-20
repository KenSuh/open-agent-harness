// Windows FFI 메모리·문자열·토큰 SID·보안 설명자 경계: 핸들·크기·정렬·유효성을 검증하고 소유 값으로 복사한다.
//! Checked Windows FFI boundary for Win32 allocations, `FOLDERID_LocalAppData`, process user SID, and descriptors.

use super::super::io_error;
use super::super::rules::Who;
use super::super::windows_rules::{
    MAX_WIDE_UNITS, SID_HEADER_BYTES, validate_sid_layout, validate_token_sid_bounds,
};
use crate::fsx::error::FsxError;
use std::ffi::{OsStr, OsString, c_void};
use std::io;
use std::mem::size_of;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::path::{Path, PathBuf};
use std::ptr::{NonNull, null_mut};
use windows_sys::Win32::Foundation::{INVALID_HANDLE_VALUE, LocalFree};
use windows_sys::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
};
use windows_sys::Win32::Security::{
    EqualSid, GetLengthSid, GetTokenInformation, IsValidSid, IsWellKnownSid, PSID,
    SECURITY_ATTRIBUTES, TOKEN_QUERY, TOKEN_USER, TokenUser, WinBuiltinAdministratorsSid,
    WinLocalSystemSid,
};
use windows_sys::Win32::Storage::FileSystem::CreateDirectoryW;
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
use windows_sys::Win32::UI::Shell::{FOLDERID_LocalAppData, KF_FLAG_DEFAULT, SHGetKnownFolderPath};

pub(super) fn invalid_data(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// RAII owner of a `LocalAlloc` / `LocalFree` Win32 allocation. Dropping frees after any error is captured.
pub(super) struct LocalMem(NonNull<c_void>);

impl LocalMem {
    pub(super) fn new(ptr: *mut c_void) -> Option<Self> {
        NonNull::new(ptr).map(Self)
    }

    pub(super) fn as_ptr(&self) -> *mut c_void {
        self.0.as_ptr()
    }
}

impl Drop for LocalMem {
    fn drop(&mut self) {
        // SAFETY: `self.0` was allocated by a Win32 API that transfers ownership to `LocalFree`.
        unsafe { LocalFree(self.0.as_ptr()) };
    }
}

/// RAII owner of a `CoTaskMem` Win32 allocation.
struct CoTaskMem(*mut u16);

impl Drop for CoTaskMem {
    fn drop(&mut self) {
        if !self.0.is_null() {
            // SAFETY: `self.0` came from `SHGetKnownFolderPath` and is freed once via `CoTaskMemFree`.
            unsafe { CoTaskMemFree(self.0.cast_const().cast()) };
        }
    }
}

/// Copies a NUL-terminated UTF-16 string allocated by Win32 into an owned `OsString` after checking non-null,
/// alignment, and a bounded length.
///
/// # Safety
/// `wide` must be null or point to a live NUL-terminated UTF-16 allocation for the duration of this call.
unsafe fn copy_wide_z(wide: *const u16) -> io::Result<OsString> {
    if wide.is_null() || !wide.is_aligned() {
        return Err(invalid_data(
            "Win32 returned a null or unaligned wide string",
        ));
    }
    let mut len = 0;
    while len < MAX_WIDE_UNITS {
        // SAFETY: `wide` is aligned and points into a live NUL-terminated UTF-16 string.
        if unsafe { wide.add(len).read() } == 0 {
            // SAFETY: all `len` code units before the terminator are initialized and readable.
            return Ok(OsString::from_wide(unsafe {
                std::slice::from_raw_parts(wide, len)
            }));
        }
        len += 1;
    }
    Err(invalid_data("Win32 wide string exceeded maximum length"))
}

/// Queries `FOLDERID_LocalAppData` and copies it into an owned `PathBuf` before freeing the shell buffer.
pub(super) fn local_app_data() -> Result<PathBuf, FsxError> {
    let mut wide = null_mut();
    // SAFETY: `wide` is writable; any returned allocation is owned by `_guard` and freed on scope exit.
    let status = unsafe {
        SHGetKnownFolderPath(
            &FOLDERID_LocalAppData,
            KF_FLAG_DEFAULT as u32,
            null_mut(),
            &mut wide,
        )
    };
    let _guard = CoTaskMem(wide);
    if status < 0 {
        let error = io::Error::other(format!("SHGetKnownFolderPath returned 0x{status:08x}"));
        return Err(io_error(
            Path::new("FOLDERID_LocalAppData"),
            "cannot find the local application data folder",
            error,
        ));
    }
    // SAFETY: `status >= 0` guarantees `wide` is either null (rejected) or a live NUL-terminated UTF-16 string.
    let owned = unsafe { copy_wide_z(wide) }.map_err(|error| {
        io_error(
            Path::new("FOLDERID_LocalAppData"),
            "cannot find the local application data folder",
            error,
        )
    })?;
    Ok(PathBuf::from(owned))
}

/// This process's validated user SID, copied into owned 4-byte-aligned storage.
pub(super) struct User {
    sid: Vec<u32>,
}

impl User {
    pub(super) fn current() -> io::Result<User> {
        let mut raw = null_mut();
        // SAFETY: `GetCurrentProcess` returns a pseudo-handle; `raw` is writable.
        if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw) } == 0 {
            return Err(io::Error::last_os_error());
        }
        if raw.is_null() || raw == INVALID_HANDLE_VALUE {
            return Err(invalid_data("OpenProcessToken returned an invalid handle"));
        }
        // SAFETY: non-null, non-invalid token handle returned by a successful `OpenProcessToken`.
        let token = unsafe { OwnedHandle::from_raw_handle(raw) };
        let mut buffer = vec![0_u64; 64];
        let buf_len = buffer.len() * size_of::<u64>();
        let (size, mut returned) = (u32::try_from(buf_len).unwrap_or(u32::MAX), 0_u32);
        // SAFETY: `buffer` is writable, 8-byte aligned and `size` bytes long.
        if unsafe {
            GetTokenInformation(
                token.as_raw_handle(),
                TokenUser,
                buffer.as_mut_ptr().cast(),
                size,
                &mut returned,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let returned = usize::try_from(returned)
            .map_err(|_| invalid_data("GetTokenInformation returned an invalid size"))?;
        let base = buffer.as_ptr().cast::<u8>();
        if returned < size_of::<TOKEN_USER>() || returned > buf_len {
            return Err(invalid_data(
                "GetTokenInformation returned an out-of-range buffer size",
            ));
        }
        // SAFETY: `buffer` is 8-byte aligned and `returned >= size_of::<TOKEN_USER>()`.
        let sid_ptr = unsafe { (*buffer.as_ptr().cast::<TOKEN_USER>()).User.Sid };
        let sid_offset = validate_token_sid_bounds(
            base.addr(),
            buf_len,
            returned,
            size_of::<TOKEN_USER>(),
            sid_ptr.addr(),
            sid_ptr.cast::<u32>().is_aligned(),
        )
        .map_err(invalid_data)?;
        // SAFETY: `sid_offset + SID_HEADER_BYTES <= returned <= buf_len`, derived from `base`.
        let checked = unsafe { base.add(sid_offset) };
        let (revision, sub_auth_count) = unsafe { (*checked, *checked.add(1)) };
        let expected_len = validate_sid_layout(sid_offset, returned, revision, sub_auth_count)
            .map_err(invalid_data)?;
        let sid_psid: PSID = checked.cast_mut().cast();
        // SAFETY: `sid_psid` is aligned and bounded for its full Win32 SID layout inside `buffer`.
        if unsafe { IsValidSid(sid_psid) } == 0
            || usize::try_from(unsafe { GetLengthSid(sid_psid) }).ok() != Some(expected_len)
        {
            return Err(invalid_data("GetTokenInformation returned an invalid SID"));
        }
        let mut sid = vec![0_u32; expected_len / 4];
        // SAFETY: `checked` has `expected_len` initialized bytes and `sid` has exact writable capacity.
        unsafe {
            std::ptr::copy_nonoverlapping(checked, sid.as_mut_ptr().cast::<u8>(), expected_len);
        }
        Ok(User { sid })
    }

    fn sid(&self) -> PSID {
        self.sid.as_ptr().cast_mut().cast()
    }

    /// Validates `sid` (non-null, 4-byte aligned, `IsValidSid`, optional exact length match) before classifying it.
    ///
    /// # Safety
    /// If non-null, `sid` must point to a live Win32 allocation containing a complete SID.
    pub(super) unsafe fn classify_sid(
        &self,
        sid: PSID,
        expected_len: Option<usize>,
    ) -> io::Result<Who> {
        if sid.is_null() || !sid.cast::<u32>().is_aligned() {
            return Err(invalid_data("null or unaligned SID"));
        }
        // SAFETY: caller guarantees `sid` points to a live allocation; alignment checked above.
        if unsafe { IsValidSid(sid) } == 0 {
            return Err(invalid_data("invalid SID"));
        }
        // SAFETY: `IsValidSid(sid)` succeeded above.
        let actual_len = usize::try_from(unsafe { GetLengthSid(sid) })
            .map_err(|_| invalid_data("invalid SID length"))?;
        if actual_len < SID_HEADER_BYTES || expected_len.is_some_and(|want| actual_len != want) {
            return Err(invalid_data("SID length mismatch"));
        }
        // SAFETY: both `sid` and `self.sid()` passed `IsValidSid`.
        unsafe {
            if EqualSid(sid, self.sid()) != 0 {
                Ok(Who::User)
            } else if IsWellKnownSid(sid, WinLocalSystemSid) != 0
                || IsWellKnownSid(sid, WinBuiltinAdministratorsSid) != 0
            {
                Ok(Who::Trusted)
            } else {
                Ok(Who::Other)
            }
        }
    }
}

/// A security descriptor allocated by Windows and freed on drop.
pub(super) struct Descriptor(LocalMem);

impl Descriptor {
    /// Owner `user`, and a protected access list whose one entry allows `user` everything, inherited by files and
    /// folders created inside.
    pub(super) fn only(user: &User) -> io::Result<Descriptor> {
        let mut text = null_mut();
        // SAFETY: `user.sid()` is a validated owned SID; Windows allocates `text`, owned by `_guard`.
        if unsafe { ConvertSidToStringSidW(user.sid(), &mut text) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let _text_guard = LocalMem::new(text.cast())
            .ok_or_else(|| invalid_data("ConvertSidToStringSidW returned a null string"))?;
        // SAFETY: successful `ConvertSidToStringSidW` returned a live NUL-terminated UTF-16 string.
        let sid = unsafe { copy_wide_z(text)? };
        drop(_text_guard);
        if sid.is_empty() {
            return Err(invalid_data("ConvertSidToStringSidW returned an empty SID"));
        }
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
        Ok(Descriptor(mem))
    }

    /// Creates the folder `path` with this descriptor; an existing name is `ERROR_ALREADY_EXISTS`.
    pub(super) fn create_directory(&self, path: &Path) -> io::Result<()> {
        let mut wide: Vec<u16> = path.as_os_str().encode_wide().collect();
        if wide.contains(&0) {
            return Err(invalid_data("directory path contains an embedded NUL"));
        }
        wide.push(0);
        let attributes = SECURITY_ATTRIBUTES {
            nLength: u32::try_from(size_of::<SECURITY_ATTRIBUTES>()).unwrap_or(u32::MAX),
            lpSecurityDescriptor: self.0.as_ptr(),
            bInheritHandle: 0,
        };
        // SAFETY: `wide` is NUL-terminated, and `attributes` and `self.0` live until the call returns.
        if unsafe { CreateDirectoryW(wide.as_ptr(), &attributes) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }
}
