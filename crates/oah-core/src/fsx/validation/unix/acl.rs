// Unix 플랫폼별 ACL 검사와 macOS 사용자 임시 위치 조회를 담당한다.
//! Platform-specific Unix ACL checks and macOS per-user temp lookup.

#[cfg(not(any(target_os = "linux", target_vendor = "apple")))]
use super::super::refuse;
#[cfg(target_vendor = "apple")]
use super::super::{io_error, refuse};
use crate::fsx::error::FsxError;
use rustix::fd::OwnedFd;
use std::path::{Path, PathBuf};

/// The operating system's per-user location path before walking and verifying ancestors.
pub(super) fn raw_location() -> Result<PathBuf, FsxError> {
    #[cfg(target_vendor = "apple")]
    {
        darwin_user_temp()
    }
    #[cfg(not(target_vendor = "apple"))]
    {
        let user = rustix::process::geteuid().as_raw();
        let runtime = PathBuf::from(format!("/run/user/{user}"));
        let exists = std::fs::symlink_metadata(&runtime).is_ok_and(|metadata| metadata.is_dir());
        Ok(if exists {
            runtime
        } else {
            PathBuf::from("/tmp")
        })
    }
}

/// `confstr(_CS_DARWIN_USER_TEMP_DIR)`: the folder under `/var/folders` that the system made for this user.
#[cfg(target_vendor = "apple")]
fn darwin_user_temp() -> Result<PathBuf, FsxError> {
    use std::ffi::{CStr, OsStr, c_char, c_int};
    use std::os::unix::ffi::OsStrExt;
    const CS_DARWIN_USER_TEMP_DIR: c_int = 65537;
    unsafe extern "C" {
        fn confstr(name: c_int, buf: *mut c_char, len: usize) -> usize;
    }
    let mut buf = [0_u8; 1024];
    // SAFETY: the buffer is writable for its whole length, which is passed; confstr writes at most that much.
    let needed = unsafe { confstr(CS_DARWIN_USER_TEMP_DIR, buf.as_mut_ptr().cast(), buf.len()) };
    let failed = std::io::Error::last_os_error();
    match CStr::from_bytes_until_nul(&buf) {
        Ok(text) if needed > 0 && needed <= buf.len() => {
            Ok(PathBuf::from(OsStr::from_bytes(text.to_bytes())))
        }
        _ => Err(io_error(
            Path::new("/var/folders"),
            "cannot find the per-user temporary folder",
            failed,
        )),
    }
}

/// Linux keeps extended POSIX ACLs in extended attributes; any of them is refused.
#[cfg(target_os = "linux")]
pub(super) fn require_no_grants(fd: &OwnedFd, path: &Path) -> Result<(), FsxError> {
    crate::fsx::staging::protected::mode_only(fd, path, true)
}

/// Refuses an access control list with any entry that allows something; entries that only deny are harmless.
#[cfg(target_vendor = "apple")]
pub(super) fn require_no_grants(fd: &OwnedFd, path: &Path) -> Result<(), FsxError> {
    use rustix::fd::AsRawFd;
    use std::ffi::{c_int, c_void};
    use std::io;
    use std::ptr::null_mut;

    const ACL_TYPE_EXTENDED: c_int = 0x0000_0100;
    const ACL_FIRST_ENTRY: c_int = 0;
    const ACL_NEXT_ENTRY: c_int = -1;
    const ACL_EXTENDED_DENY: c_int = 2;
    /// ENOENT (no list), ENOTSUP and EOPNOTSUPP (the volume has none).
    const NO_LIST: [i32; 3] = [2, 45, 102];

    unsafe extern "C" {
        fn acl_get_fd_np(fd: c_int, kind: c_int) -> *mut c_void;
        fn acl_get_entry(acl: *mut c_void, entry_id: c_int, entry: *mut *mut c_void) -> c_int;
        fn acl_get_tag_type(entry: *mut c_void, tag: *mut c_int) -> c_int;
        fn acl_free(object: *mut c_void) -> c_int;
    }

    // SAFETY: the descriptor stays open for the call; a returned list is ours and freed below.
    let acl = unsafe { acl_get_fd_np(fd.as_raw_fd(), ACL_TYPE_EXTENDED) };
    if acl.is_null() {
        let error = io::Error::last_os_error();
        return match error.raw_os_error() {
            Some(code) if NO_LIST.contains(&code) => Ok(()),
            _ => Err(io_error(path, "cannot read the access control list", error)),
        };
    }
    let (mut grants, mut entry, mut id) = (false, null_mut(), ACL_FIRST_ENTRY);
    // SAFETY: `acl` stays allocated until acl_free below, and its entries stay valid while it does.
    while unsafe { acl_get_entry(acl, id, &raw mut entry) } == 0 {
        let mut tag = 0;
        // SAFETY: `entry` came from acl_get_entry on the live list; `tag` is writable.
        let read = unsafe { acl_get_tag_type(entry, &raw mut tag) };
        if read != 0 || tag != ACL_EXTENDED_DENY {
            grants = true;
            break;
        }
        id = ACL_NEXT_ENTRY;
    }
    // SAFETY: frees the list acl_get_fd_np returned, exactly once.
    unsafe { acl_free(acl) };
    if grants {
        Err(refuse(path, "an access control list allows access to it"))
    } else {
        Ok(())
    }
}

#[cfg(not(any(target_os = "linux", target_vendor = "apple")))]
pub(super) fn require_no_grants(_fd: &OwnedFd, path: &Path) -> Result<(), FsxError> {
    Err(refuse(
        path,
        "access control lists cannot be checked on this platform",
    ))
}
