// Unix 비공개 확인 폴더: 사용자별 위치를 고르고 확인한 뒤, 이 사용자만 쓸 수 있는 새 폴더와 실행 사본을 만들고 끝나면 지운다.
//! The Unix side of [`super::validate_private_copy`].

use super::rules::location_rule;
use super::{NAME_ATTEMPTS, io_error, refuse, unique_name};
use crate::fsx::error::FsxError;
use rustix::fd::OwnedFd;
use rustix::fs::{self as rx, AtFlags, FileType, Mode, OFlags, Stat};
use rustix::io::Errno;
use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io::Write;
use std::path::{Component, Path, PathBuf};

const DIRECTORY: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

/// The per-user location: the system's per-user temporary folder on macOS; elsewhere `/run/user/<effective uid>`
/// when it exists, or else `/tmp`.
pub(super) fn location() -> Result<PathBuf, FsxError> {
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
    use std::ffi::{CStr, c_char, c_int};
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

/// The verified location and a new private folder in it, with the copy once written. Dropping it removes both.
pub(super) struct Private {
    base: OwnedFd,
    name: String,
    folder: OwnedFd,
    path: PathBuf,
    copy: Option<OsString>,
}

impl Private {
    /// Opens the location `base_path` (canonical) one folder at a time from `/`, never following a link, and checks
    /// every folder on the way. Then makes a new private folder in it: `mkdirat` refuses a name that exists, and the
    /// folder must then be this user's, with mode `0700` and no access control list granting more.
    pub(super) fn create(base_path: &Path) -> Result<Private, FsxError> {
        let user = rustix::process::geteuid().as_raw();
        let mut walked = PathBuf::from("/");
        let mut base =
            rx::open(&walked, DIRECTORY, Mode::empty()).map_err(|err| open_error(&walked, err))?;
        verify_location(&base, &walked, user)?;
        for component in base_path.components() {
            match component {
                Component::RootDir => {}
                Component::Normal(name) => {
                    walked.push(name);
                    base = rx::openat(&base, name, DIRECTORY, Mode::empty())
                        .map_err(|err| open_error(&walked, err))?;
                    verify_location(&base, &walked, user)?;
                }
                _ => return Err(refuse(base_path, "the location is not a canonical path")),
            }
        }
        for _ in 0..NAME_ATTEMPTS {
            let name = unique_name();
            let path = base_path.join(&name);
            match rx::mkdirat(&base, name.as_str(), Mode::RWXU) {
                Ok(()) => {}
                Err(Errno::EXIST) => continue,
                Err(err) => {
                    return Err(io_error(
                        &path,
                        "cannot create the private folder",
                        err.into(),
                    ));
                }
            }
            let folder = match rx::openat(&base, name.as_str(), DIRECTORY, Mode::empty()) {
                Ok(folder) => folder,
                Err(err) => {
                    let _ = rx::unlinkat(&base, name.as_str(), AtFlags::REMOVEDIR);
                    return Err(open_error(&path, err));
                }
            };
            let stat = fstat(&folder, &path)?;
            if !is_folder(&stat) || stat.st_uid != user {
                // Not the folder this call made, so it is left alone.
                return Err(refuse(&path, "the new private folder is not this user's"));
            }
            let private = Private {
                base,
                name,
                folder,
                path,
                copy: None,
            };
            private.make_private()?;
            return Ok(private);
        }
        Err(refuse(base_path, "no unused private folder name was found"))
    }

    /// Sets mode `0700` (the umask or a set-group-ID parent may have changed it) and verifies it, with no access
    /// control list granting more.
    fn make_private(&self) -> Result<(), FsxError> {
        rx::fchmod(&self.folder, Mode::RWXU)
            .map_err(|err| io_error(&self.path, "cannot make the folder private", err.into()))?;
        if Mode::from_raw_mode(fstat(&self.folder, &self.path)?.st_mode) != Mode::RWXU {
            return Err(refuse(&self.path, "the private folder is not mode 0700"));
        }
        acl::require_no_grants(&self.folder, &self.path)
    }

    /// Creates the copy exclusively, writes it with mode `0700` and closes it before anything runs.
    pub(super) fn write(&mut self, name: &OsStr, bytes: &[u8]) -> Result<PathBuf, FsxError> {
        let path = self.path.join(name);
        let flags =
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let fd = rx::openat(&self.folder, name, flags, Mode::RWXU)
            .map_err(|err| io_error(&path, "cannot create the private copy", err.into()))?;
        self.copy = Some(name.to_os_string());
        let mut file = File::from(fd);
        file.write_all(bytes)
            .map_err(|error| io_error(&path, "cannot write the private copy", error))?;
        rx::fchmod(&file, Mode::RWXU).map_err(|err| {
            io_error(&path, "cannot make the private copy executable", err.into())
        })?;
        Ok(path)
    }
}

impl Drop for Private {
    fn drop(&mut self) {
        if let Some(name) = self.copy.take() {
            let _ = rx::unlinkat(&self.folder, name.as_os_str(), AtFlags::empty());
        }
        let _ = rx::unlinkat(&self.base, self.name.as_str(), AtFlags::REMOVEDIR);
    }
}

/// One folder on the way to the private folder: see [`location_rule`], and no access control list granting more.
fn verify_location(fd: &OwnedFd, path: &Path, user: u32) -> Result<(), FsxError> {
    let stat = fstat(fd, path)?;
    let mode = Mode::from_raw_mode(stat.st_mode);
    let others_write = mode.intersects(Mode::WGRP | Mode::WOTH);
    location_rule(
        is_folder(&stat),
        stat.st_uid,
        user,
        others_write,
        mode.contains(Mode::SVTX),
    )
    .map_err(|why| refuse(path, why))?;
    acl::require_no_grants(fd, path)
}

fn fstat(fd: &OwnedFd, path: &Path) -> Result<Stat, FsxError> {
    rx::fstat(fd).map_err(|err| io_error(path, "cannot inspect", err.into()))
}

fn is_folder(stat: &Stat) -> bool {
    FileType::from_raw_mode(stat.st_mode) == FileType::Directory
}

fn open_error(path: &Path, err: Errno) -> FsxError {
    match err {
        Errno::LOOP | Errno::NOTDIR => refuse(path, "it is a link or not a folder"),
        _ => io_error(path, "cannot open", err.into()),
    }
}

#[cfg(target_os = "linux")]
mod acl {
    use crate::fsx::error::FsxError;
    use rustix::fd::OwnedFd;
    use std::path::Path;

    /// Linux keeps extended POSIX ACLs in extended attributes; any of them is refused.
    pub(super) fn require_no_grants(fd: &OwnedFd, path: &Path) -> Result<(), FsxError> {
        crate::fsx::staging::protected::mode_only(fd, path, true)
    }
}

#[cfg(target_vendor = "apple")]
mod acl {
    use super::super::{io_error, refuse};
    use crate::fsx::error::FsxError;
    use rustix::fd::{AsRawFd, OwnedFd};
    use std::ffi::{c_int, c_void};
    use std::io;
    use std::path::Path;
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

    /// Refuses an access control list with any entry that allows something; entries that only deny are harmless.
    pub(super) fn require_no_grants(fd: &OwnedFd, path: &Path) -> Result<(), FsxError> {
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
}

#[cfg(not(any(target_os = "linux", target_vendor = "apple")))]
mod acl {
    use super::super::refuse;
    use crate::fsx::error::FsxError;
    use rustix::fd::OwnedFd;
    use std::path::Path;

    pub(super) fn require_no_grants(_fd: &OwnedFd, path: &Path) -> Result<(), FsxError> {
        Err(refuse(
            path,
            "access control lists cannot be checked on this platform",
        ))
    }
}
