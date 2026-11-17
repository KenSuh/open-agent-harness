// Windows 시험 프로세스의 실제 토큰, 완화 정책과 시스템 로캘을 읽는다.
use std::ffi::c_void;
use std::io;
use std::mem::{offset_of, size_of};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle, RawHandle};
use std::ptr::{NonNull, null_mut};

const TOKEN_QUERY: u32 = 0x0008;
const TOKEN_USER: i32 = 1;
const TOKEN_ELEVATION: i32 = 20;
const TOKEN_INTEGRITY_LEVEL: i32 = 25;
const PROCESS_REDIRECTION_TRUST_POLICY: i32 = 16;
const ENFORCE_REDIRECTION_TRUST: u32 = 1;
const LOCALE_NAME_MAX_LENGTH: i32 = 85;
const SID_HEADER_BYTES: usize = 8;
const SID_MAX_SUB_AUTHORITIES: usize = 15;
const SID_STORAGE_WORDS: usize = 2 + SID_MAX_SUB_AUTHORITIES;

#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetCurrentProcess() -> RawHandle;
    fn GetProcessMitigationPolicy(
        process: RawHandle,
        policy: i32,
        buffer: *mut c_void,
        len: usize,
    ) -> i32;
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
    fn GetACP() -> u32;
    fn GetOEMCP() -> u32;
    fn GetSystemDefaultLocaleName(buffer: *mut u16, len: i32) -> i32;
}

#[link(name = "advapi32")]
unsafe extern "system" {
    fn OpenProcessToken(process: RawHandle, access: u32, token: *mut RawHandle) -> i32;
    fn GetTokenInformation(
        token: RawHandle,
        class: i32,
        buffer: *mut c_void,
        len: u32,
        returned: *mut u32,
    ) -> i32;
    fn IsValidSid(sid: *mut c_void) -> i32;
    fn ConvertSidToStringSidW(sid: *mut c_void, string: *mut *mut u16) -> i32;
}

// TOKEN_USER and TOKEN_MANDATORY_LABEL each contain one SID_AND_ATTRIBUTES.
#[repr(C)]
struct SidAndAttributes {
    sid: *mut c_void,
    attributes: u32,
}

// The SDK's TOKEN_USER_MAX_SIZE and TOKEN_INTEGRITY_LEVEL_MAX_SIZE allow
// a header followed by SECURITY_MAX_SID_SIZE bytes (8 + 15 DWORDs).
#[repr(C)]
struct TokenSidBuffer {
    entry: SidAndAttributes,
    sid_storage: [u32; SID_STORAGE_WORDS],
}

const TOKEN_SID_BUFFER_BYTES: u32 = size_of::<TokenSidBuffer>() as u32;
const DWORD_BYTES: u32 = size_of::<u32>() as u32;

fn invalid_data(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn current_token(process: RawHandle) -> io::Result<OwnedHandle> {
    let mut token = null_mut();
    // SAFETY: process is the current-process pseudo-handle and token is a writable HANDLE.
    if unsafe { OpenProcessToken(process, TOKEN_QUERY, &raw mut token) } == 0 {
        return Err(io::Error::last_os_error());
    }
    if token.is_null() {
        return Err(invalid_data("OpenProcessToken returned a null handle"));
    }
    // SAFETY: successful OpenProcessToken transfers ownership of a real handle.
    Ok(unsafe { OwnedHandle::from_raw_handle(token) })
}

fn token_sid(token: &OwnedHandle, class: i32) -> io::Result<String> {
    let mut buffer = TokenSidBuffer {
        entry: SidAndAttributes {
            sid: null_mut(),
            attributes: 0,
        },
        sid_storage: [0; SID_STORAGE_WORDS],
    };
    let base = (&raw mut buffer).cast::<u8>();
    let mut returned = 0;
    // SAFETY: the handle is live and the output is writable, aligned, and large
    // enough for either of the two SID information classes used here.
    if unsafe {
        GetTokenInformation(
            token.as_raw_handle(),
            class,
            base.cast(),
            TOKEN_SID_BUFFER_BYTES,
            &raw mut returned,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    if returned > TOKEN_SID_BUFFER_BYTES {
        return Err(invalid_data(
            "GetTokenInformation returned an oversized SID buffer",
        ));
    }
    let storage_start = offset_of!(TokenSidBuffer, sid_storage);
    let storage_end = storage_start + size_of::<[u32; SID_STORAGE_WORDS]>();
    let end = usize::try_from(returned)
        .map_err(|_| invalid_data("GetTokenInformation returned an invalid SID buffer size"))?
        .min(storage_end);
    let address = buffer.entry.sid.addr();
    let start = address
        .checked_sub(base.addr())
        .filter(|&offset| offset >= storage_start && offset <= end.saturating_sub(SID_HEADER_BYTES))
        .filter(|_| buffer.entry.sid.cast::<u32>().is_aligned())
        .ok_or_else(|| invalid_data("GetTokenInformation returned a SID outside its buffer"))?;
    // SAFETY: the checked header is within the returned part of the initialized
    // backing storage. SubAuthorityCount is the second byte of a Win32 SID.
    let count = usize::from(unsafe { base.add(start + 1).read() });
    if count > SID_MAX_SUB_AUTHORITIES || SID_HEADER_BYTES + count * 4 > end - start {
        return Err(invalid_data("GetTokenInformation returned a truncated SID"));
    }
    // SAFETY: derive the aligned and fully bounded SID from its live backing allocation.
    unsafe { sid_text(base.add(start).cast()) }
}

unsafe fn sid_text(sid: *mut c_void) -> io::Result<String> {
    // SAFETY: token_sid checked alignment and the full SID extent.
    if unsafe { IsValidSid(sid) } == 0 {
        return Err(invalid_data("GetTokenInformation returned an invalid SID"));
    }
    let mut wide = null_mut();
    // SAFETY: the SID is valid and wide is writable. Win32 allocates the result.
    if unsafe { ConvertSidToStringSidW(sid, &raw mut wide) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let wide = NonNull::new(wide)
        .ok_or_else(|| invalid_data("ConvertSidToStringSidW returned a null string"))?;
    // SAFETY: successful conversion returns a live NUL-terminated UTF-16 string.
    let text = unsafe {
        let mut len = 0;
        while wide.as_ptr().add(len).read() != 0 {
            len += 1;
        }
        String::from_utf16(std::slice::from_raw_parts(wide.as_ptr(), len))
    };
    // SAFETY: release the Win32 allocation exactly once, including on decode error.
    if !unsafe { LocalFree(wide.as_ptr().cast()) }.is_null() {
        return Err(io::Error::last_os_error());
    }
    text.map_err(|_| invalid_data("ConvertSidToStringSidW returned invalid UTF-16"))
}

fn token_elevation(token: RawHandle) -> io::Result<u32> {
    let (mut elevated, mut returned) = (0_u32, 0_u32);
    // SAFETY: both outputs are writable DWORDs. Win32 reports invalid handles
    // through its normal error path.
    if unsafe {
        GetTokenInformation(
            token,
            TOKEN_ELEVATION,
            (&raw mut elevated).cast(),
            DWORD_BYTES,
            &raw mut returned,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    if returned != DWORD_BYTES {
        return Err(invalid_data(
            "GetTokenInformation returned a short elevation record",
        ));
    }
    Ok(elevated)
}

pub(super) struct ProcessContext {
    pub pid: u32,
    pub user_sid: String,
    pub integrity: String,
    pub elevated: u32,
    pub redirection_guard: u32,
}

impl ProcessContext {
    pub(super) fn read() -> io::Result<Self> {
        // SAFETY: this returns a borrowed pseudo-handle and has no preconditions.
        let process = unsafe { GetCurrentProcess() };
        let token = current_token(process)?;
        let mut flags = 0_u32;
        // SAFETY: the current-process pseudo-handle is live and the selected
        // PROCESS_MITIGATION_REDIRECTION_TRUST_POLICY output is one DWORD.
        if unsafe {
            GetProcessMitigationPolicy(
                process,
                PROCESS_REDIRECTION_TRUST_POLICY,
                (&raw mut flags).cast(),
                size_of::<u32>(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            pid: std::process::id(),
            user_sid: token_sid(&token, TOKEN_USER)?,
            integrity: token_sid(&token, TOKEN_INTEGRITY_LEVEL)?,
            elevated: token_elevation(token.as_raw_handle())?,
            redirection_guard: flags & ENFORCE_REDIRECTION_TRUST,
        })
    }

    pub(super) fn require_medium(&self, guard: u32) -> io::Result<()> {
        if self.integrity != "S-1-16-8192" || self.elevated != 0 || self.redirection_guard != guard
        {
            return Err(io::Error::other(format!(
                "required medium, elevated=0, redirection_guard={guard}; observed pid={} user_sid={} integrity={} elevated={} redirection_guard={}",
                self.pid, self.user_sid, self.integrity, self.elevated, self.redirection_guard
            )));
        }
        Ok(())
    }
}

#[allow(dead_code, reason = "Used by the standalone Windows evidence probe")]
pub(super) struct SystemLocale {
    pub ansi_codepage: u32,
    pub oem_codepage: u32,
    pub name: String,
}

#[allow(dead_code, reason = "Used by the standalone Windows evidence probe")]
impl SystemLocale {
    pub(super) fn read() -> io::Result<Self> {
        let mut name = [0_u16; LOCALE_NAME_MAX_LENGTH as usize];
        // SAFETY: the writable UTF-16 buffer has the supplied capacity in code units.
        let len = unsafe { GetSystemDefaultLocaleName(name.as_mut_ptr(), LOCALE_NAME_MAX_LENGTH) };
        if len == 0 {
            return Err(io::Error::last_os_error());
        }
        let len = usize::try_from(len).map_err(|_| invalid_data("invalid locale length"))?;
        let name = match name.get(..len).and_then(|name| name.split_last()) {
            Some((&0, name)) if !name.is_empty() && !name.contains(&0) => name,
            _ => return Err(invalid_data("invalid system locale name")),
        };
        // SAFETY: these process/system code page queries have no preconditions.
        let (ansi_codepage, oem_codepage) = unsafe { (GetACP(), GetOEMCP()) };
        if !(1..=65535).contains(&ansi_codepage) || !(1..=65535).contains(&oem_codepage) {
            return Err(invalid_data("invalid observed ANSI or OEM code page"));
        }
        Ok(Self {
            ansi_codepage,
            oem_codepage,
            name: String::from_utf16(name)
                .map_err(|_| invalid_data("invalid UTF-16 system locale name"))?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn medium_guard_enforces_integrity_elevation_and_policy_independently() {
        for (integrity, elevated, actual, required, accepts) in [
            ("S-1-16-8192", 0, 0, 0, true),
            ("S-1-16-8192", 0, 1, 1, true),
            ("S-1-16-12288", 0, 0, 0, false),
            ("S-1-16-8192", 1, 0, 0, false),
            ("S-1-16-8192", 0, 0, 1, false),
            ("S-1-16-8192", 0, 1, 0, false),
        ] {
            let context = ProcessContext {
                pid: 1,
                user_sid: "S-1-5-21-1-2-3-1000".into(),
                integrity: integrity.into(),
                elevated,
                redirection_guard: actual,
            };
            assert_eq!(
                context.require_medium(required).is_ok(),
                accepts,
                "integrity={integrity} elevated={elevated} actual guard={actual} required={required}"
            );
        }
    }

    #[test]
    fn failed_token_query_propagates_win32_error() {
        assert_eq!(
            token_elevation(null_mut()).unwrap_err().raw_os_error(),
            Some(6)
        );
    }
}
