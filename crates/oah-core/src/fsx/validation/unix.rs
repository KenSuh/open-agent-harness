// Unix 비공개 확인 폴더: 사용자별 위치를 고르고 확인한 뒤, 이 사용자만 쓸 수 있는 새 폴더와 실행 사본을 만들고 신원·소유자가 확인된 경우에만 지운다.
//! The Unix side of [`super::validate_private_copy`].

mod acl;

use super::rules::{CleanupCheck, location_rule, may_cleanup};
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

/// Device and inode identity of a created Unix entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct UnixId {
    dev: i128,
    ino: u128,
}

impl UnixId {
    fn of(stat: &Stat) -> Self {
        Self {
            dev: i128::from(stat.st_dev),
            ino: u128::from(stat.st_ino),
        }
    }
}

/// Verified per-user location: holds the opened directory handle walked from `/` with `O_NOFOLLOW`.
pub(super) struct Location {
    fd: OwnedFd,
    path: PathBuf,
    user: u32,
}

impl Location {
    pub(super) fn path(&self) -> &Path {
        &self.path
    }
}

/// The per-user location, walked and verified from `/` down: the system's per-user temporary folder on macOS;
/// elsewhere `/run/user/<effective uid>` when it exists, or else `/tmp`.
pub(super) fn location() -> Result<Location, FsxError> {
    open_location(&acl::raw_location()?)
}

#[cfg(test)]
pub(super) fn open_base_for_test(base: &Path) -> Result<Location, FsxError> {
    open_location(base)
}

/// Opens `raw` after canonicalization, one folder at a time from `/` with `O_NOFOLLOW`, checking every folder.
fn open_location(raw: &Path) -> Result<Location, FsxError> {
    if !raw.is_absolute() {
        return Err(refuse(
            raw,
            "the private check location is not an absolute path",
        ));
    }
    let canonical = std::fs::canonicalize(raw)
        .map_err(|error| io_error(raw, "cannot resolve the private check location", error))?;
    let user = rustix::process::geteuid().as_raw();
    let mut walked = PathBuf::from("/");
    let mut base =
        rx::open(&walked, DIRECTORY, Mode::empty()).map_err(|err| open_error(&walked, err))?;
    verify_location(&base, &walked, user)?;
    for component in canonical.components() {
        match component {
            Component::RootDir => {}
            Component::Normal(name) => {
                walked.push(name);
                base = rx::openat(&base, name, DIRECTORY, Mode::empty())
                    .map_err(|err| open_error(&walked, err))?;
                verify_location(&base, &walked, user)?;
            }
            _ => return Err(refuse(&canonical, "the location is not a canonical path")),
        }
    }
    Ok(Location {
        fd: base,
        path: canonical,
        user,
    })
}

struct VerifiedCopy {
    name: OsString,
    id: UnixId,
}

/// The verified location and a new private folder in it, with the copy once written. Dropping it removes entries
/// only when their verified type, owner and identity remain intact.
pub(super) struct Private {
    base: Location,
    name: String,
    folder: OwnedFd,
    path: PathBuf,
    dir_id: UnixId,
    copy: Option<VerifiedCopy>,
}

impl Private {
    /// Creates a new private folder inside the already-verified `base`: `mkdirat` refuses a name that exists,
    /// then the folder is opened with `O_NOFOLLOW` and verified as this user's plain directory with mode `0700`
    /// and no granting ACL. If the post-create open or ownership check fails, the entry is left untouched.
    pub(super) fn create(base: Location) -> Result<Private, FsxError> {
        for _ in 0..NAME_ATTEMPTS {
            let name = unique_name();
            let path = base.path.join(&name);
            match rx::mkdirat(&base.fd, name.as_str(), Mode::RWXU) {
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
            // If the first post-create open fails, identity and owner are unproven: leave untouched.
            let folder = rx::openat(&base.fd, name.as_str(), DIRECTORY, Mode::empty())
                .map_err(|err| open_error(&path, err))?;
            let stat = fstat(&folder, &path)?;
            if !is_folder(&stat) || stat.st_uid != base.user {
                return Err(refuse(&path, "the new private folder is not this user's"));
            }
            let dir_id = UnixId::of(&stat);
            let entry =
                rx::statat(&base.fd, name.as_str(), AtFlags::SYMLINK_NOFOLLOW).map_err(|err| {
                    io_error(&path, "cannot inspect the new private folder", err.into())
                })?;
            if !is_folder(&entry) || entry.st_uid != base.user || UnixId::of(&entry) != dir_id {
                return Err(refuse(
                    &path,
                    "the new private folder changed after creation",
                ));
            }
            let private = Private {
                base,
                name,
                folder,
                path,
                dir_id,
                copy: None,
            };
            private.make_private()?;
            return Ok(private);
        }
        Err(refuse(
            &base.path,
            "no unused private folder name was found",
        ))
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

    /// Creates the copy exclusively, records its verified identity, writes it with mode `0700` and closes it.
    pub(super) fn write(&mut self, name: &OsStr, bytes: &[u8]) -> Result<PathBuf, FsxError> {
        let path = self.path.join(name);
        let flags =
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let fd = rx::openat(&self.folder, name, flags, Mode::RWXU)
            .map_err(|err| io_error(&path, "cannot create the private copy", err.into()))?;
        let stat = fstat(&fd, &path)?;
        if !is_regular(&stat) || stat.st_uid != self.base.user {
            return Err(refuse(
                &path,
                "the new private copy is not this user's regular file",
            ));
        }
        self.copy = Some(VerifiedCopy {
            name: name.to_os_string(),
            id: UnixId::of(&stat),
        });
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
        if let Some(copy) = self.copy.take() {
            let check = rx::statat(
                &self.folder,
                copy.name.as_os_str(),
                AtFlags::SYMLINK_NOFOLLOW,
            )
            .ok()
            .map_or_else(CleanupCheck::unverified, |st| CleanupCheck {
                opened: true,
                expected_type: is_regular(&st),
                owner_is_user: st.st_uid == self.base.user,
                identity_intact: UnixId::of(&st) == copy.id,
            });
            if may_cleanup(check) {
                let _ = rx::unlinkat(&self.folder, copy.name.as_os_str(), AtFlags::empty());
            }
        }
        // Hold both `self.folder` and `self.base.fd` open through verification and removal.
        let check = match (
            rx::fstat(&self.folder),
            rx::statat(&self.base.fd, self.name.as_str(), AtFlags::SYMLINK_NOFOLLOW),
        ) {
            (Ok(fd_st), Ok(at_st)) => CleanupCheck {
                opened: true,
                expected_type: is_folder(&fd_st) && is_folder(&at_st),
                owner_is_user: fd_st.st_uid == self.base.user && at_st.st_uid == self.base.user,
                identity_intact: UnixId::of(&fd_st) == self.dir_id
                    && UnixId::of(&at_st) == self.dir_id,
            },
            _ => CleanupCheck::unverified(),
        };
        if may_cleanup(check) {
            let _ = rx::unlinkat(&self.base.fd, self.name.as_str(), AtFlags::REMOVEDIR);
        }
    }
}

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

fn is_regular(stat: &Stat) -> bool {
    FileType::from_raw_mode(stat.st_mode) == FileType::RegularFile
}

fn open_error(path: &Path, err: Errno) -> FsxError {
    match err {
        Errno::LOOP | Errno::NOTDIR => refuse(path, "it is a link or not a folder"),
        _ => io_error(path, "cannot open", err.into()),
    }
}
