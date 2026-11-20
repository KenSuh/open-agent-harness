// Windows 핸들 보안 읽기: GetSecurityInfo·GetAclInformation·GetAce 로 소유자와 접근 목록을 경계 검증 후 파싱한다.
//! Handle-bound Windows owner and DACL reader using checked API-owned ACL metadata.

use super::super::io_error;
use super::super::rules::{ALLOW_ACE_TYPE, Entry, Security, Who};
use super::super::windows_rules::{
    validate_ace_extent, validate_ace_header_bounds, validate_acl_size,
    validate_allow_ace_sid_offset, validate_sid_layout,
};
use super::ffi::{LocalMem, User, invalid_data};
use crate::fsx::error::FsxError;
use std::fs::File;
use std::io;
use std::mem::{offset_of, size_of};
use std::os::windows::io::AsRawHandle;
use std::path::Path;
use std::ptr::null_mut;
use windows_sys::Win32::Security::Authorization::{GetSecurityInfo, SE_FILE_OBJECT};
use windows_sys::Win32::Security::{
    ACCESS_ALLOWED_ACE, ACE_HEADER, ACL, ACL_SIZE_INFORMATION, AclSizeInformation,
    DACL_SECURITY_INFORMATION, GetAce, GetAclInformation, GetSecurityDescriptorControl,
    INHERIT_ONLY_ACE, OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID, SE_DACL_PRESENT,
    SE_DACL_PROTECTED,
};

/// The owner of the file or folder open as `handle`, read through that handle.
pub(super) fn read_owner(handle: &File, path: &Path, user: &User) -> Result<Who, FsxError> {
    let (mut owner, mut descriptor) = (null_mut(), null_mut());
    // SAFETY: `handle` is live with `READ_CONTROL`; outputs are writable.
    let status = unsafe {
        GetSecurityInfo(
            handle.as_raw_handle(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            null_mut(),
            null_mut(),
            &mut descriptor,
        )
    };
    if status != 0 {
        let error = io::Error::from_raw_os_error(i32::try_from(status).unwrap_or(i32::MAX));
        return Err(io_error(path, "cannot read the entry's owner", error));
    }
    let _guard = LocalMem::new(descriptor).ok_or_else(|| {
        io_error(
            path,
            "cannot read the entry's owner",
            invalid_data("GetSecurityInfo returned a null security descriptor"),
        )
    })?;
    if owner.is_null() {
        return Ok(Who::Other);
    }
    // SAFETY: `owner` points into `_guard`, which stays alive until this function returns.
    unsafe { user.classify_sid(owner, None) }
        .map_err(|error| io_error(path, "cannot read the entry's owner", error))
}

/// The owner and access list of the folder open as `folder`, read through that handle.
pub(super) fn read_security(folder: &File, path: &Path, user: &User) -> Result<Security, FsxError> {
    let (mut owner, mut dacl, mut descriptor) = (null_mut(), null_mut(), null_mut());
    // SAFETY: `folder` is live with `READ_CONTROL`; outputs are writable. `guard` owns `descriptor`.
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
    let guard = LocalMem::new(descriptor).ok_or_else(|| {
        io_error(
            path,
            "cannot read the folder's owner and access list",
            invalid_data("GetSecurityInfo returned a null security descriptor"),
        )
    })?;
    // SAFETY: `owner`, `dacl` and `guard` come from one successful `GetSecurityInfo`; `guard` drops only after
    // `describe` has already captured any `io::Error::last_os_error()`.
    unsafe { describe(owner, dacl, guard.as_ptr(), user) }
        .map_err(|error| io_error(path, "cannot read the folder's access list", error))
}

/// # Safety
/// `owner`, `dacl` and `descriptor` come from one successful `GetSecurityInfo` call whose descriptor stays live.
unsafe fn describe(
    owner: PSID,
    dacl: *mut ACL,
    descriptor: PSECURITY_DESCRIPTOR,
    user: &User,
) -> io::Result<Security> {
    let (mut control, mut revision) = (0_u16, 0_u32);
    // SAFETY: `descriptor` is non-null and live; both outputs are writable.
    if unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let owner_who = if owner.is_null() {
        Who::Other
    } else {
        // SAFETY: `owner` points into the live descriptor.
        unsafe { user.classify_sid(owner, None)? }
    };
    let protected = control & SE_DACL_PROTECTED != 0;
    if control & SE_DACL_PRESENT == 0 || dacl.is_null() {
        return Ok(Security {
            owner: owner_who,
            dacl_present: false,
            protected,
            entries: Vec::new(),
        });
    }
    if !dacl.cast::<u16>().is_aligned() {
        return Err(invalid_data("unaligned DACL pointer"));
    }
    let mut acl_info = ACL_SIZE_INFORMATION::default();
    let info_len = u32::try_from(size_of::<ACL_SIZE_INFORMATION>()).unwrap_or(u32::MAX);
    // SAFETY: `dacl` is a live aligned ACL inside `descriptor` and `acl_info` is writable.
    if unsafe {
        GetAclInformation(
            dacl.cast_const(),
            (&raw mut acl_info).cast(),
            info_len,
            AclSizeInformation,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    let acl_bytes = validate_acl_size(
        acl_info.AceCount,
        acl_info.AclBytesInUse,
        size_of::<ACL>(),
        size_of::<ACE_HEADER>(),
    )
    .map_err(invalid_data)?;
    let acl_start = dacl.addr();
    let count =
        usize::try_from(acl_info.AceCount).map_err(|_| invalid_data("ACE count overflow"))?;
    let mut entries = Vec::with_capacity(count);
    for index in 0..acl_info.AceCount {
        let mut ace = null_mut();
        // SAFETY: `index < acl_info.AceCount` on the live ACL; `ace` is writable.
        if unsafe { GetAce(dacl.cast_const(), index, &mut ace) } == 0 {
            return Err(io::Error::last_os_error());
        }
        if ace.is_null() {
            return Err(invalid_data("GetAce returned a null ACE pointer"));
        }
        validate_ace_header_bounds(
            acl_start,
            acl_bytes,
            size_of::<ACL>(),
            size_of::<ACE_HEADER>(),
            ace.addr(),
        )
        .map_err(invalid_data)?;
        // SAFETY: `ace` is bounded within `[acl_start + size_of::<ACL>(), acl_start + acl_bytes)`.
        let header = unsafe { ace.cast::<ACE_HEADER>().read_unaligned() };
        let ace_size = validate_ace_extent(
            acl_start,
            acl_bytes,
            ace.addr(),
            size_of::<ACE_HEADER>(),
            header.AceSize,
        )
        .map_err(invalid_data)?;
        let who = if header.AceType == ALLOW_ACE_TYPE {
            let sid_offset = offset_of!(ACCESS_ALLOWED_ACE, SidStart);
            let sid_aligned = ace
                .cast::<u8>()
                .wrapping_add(sid_offset)
                .cast::<u32>()
                .is_aligned();
            validate_allow_ace_sid_offset(
                ace_size,
                sid_offset,
                size_of::<ACCESS_ALLOWED_ACE>(),
                sid_aligned,
            )
            .map_err(invalid_data)?;
            // SAFETY: `validate_allow_ace_sid_offset` proved `sid_offset + SID_HEADER_BYTES <= ace_size`.
            let sid_ptr = unsafe { ace.cast::<u8>().add(sid_offset) };
            // SAFETY: `sid_ptr` is within `ace_size` with at least `SID_HEADER_BYTES` readable.
            let (sid_rev, sub_auth_count) = unsafe { (*sid_ptr, *sid_ptr.add(1)) };
            let sid_len = validate_sid_layout(sid_offset, ace_size, sid_rev, sub_auth_count)
                .map_err(invalid_data)?;
            // SAFETY: `sid_ptr` is aligned and bounded to `sid_len` bytes inside the live ACL.
            unsafe { user.classify_sid(sid_ptr.cast(), Some(sid_len))? }
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
    Ok(Security {
        owner: owner_who,
        dacl_present: true,
        protected,
        entries,
    })
}
