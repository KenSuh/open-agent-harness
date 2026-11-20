// 비공개 확인: 실행 파일 바이트를 OS 가 정한 사용자별 위치의 새 비공개 폴더에만 쓰고, 그 사본에서만 호출자의 확인을 돌린 뒤 지운다.
//! A private copy of executable bytes for a caller's check (CONTRACTS C7).
//!
//! [`validate_private_copy`] runs nothing under `OAH_HOME`. It makes a new folder of its own inside a fixed
//! per-user location the operating system provides, writes the bytes there and runs the caller's check on that copy
//! only. The session environment never chooses the location:
//!
//! * macOS: the per-user temporary folder the system assigns under `/var/folders` (`_CS_DARWIN_USER_TEMP_DIR`).
//! * Linux: `/run/user/<effective uid>` when it exists, or else `/tmp`.
//! * Windows: `Temp` in the user's local application data folder (`FOLDERID_LocalAppData`).
//!
//! The location must not be `OAH_HOME` or inside it, and its authority is bound to opened handles before anything is
//! written:
//!
//! * Unix: every folder from `/` down to the location is opened without following a link and must belong to root
//!   or this user, must not be writable by others unless it is sticky (as `/tmp` is), and must have no access
//!   control list granting more (extended POSIX ACLs on Linux, allowing entries on macOS). The copy then runs by
//!   that same path, and only root or this user could change it meanwhile.
//! * Windows: the local application data folder and its `Temp`, as the shell names them and before anything
//!   resolves them, must each be a folder that is not a reparse point, owned by this user, SYSTEM or
//!   Administrators, with an access list that lets nobody else use the folder itself. Both the named path and the
//!   final canonical path resolved directly from the opened handles must lie on a fixed local drive (`DRIVE_FIXED`).
//!   The verified `Temp` handle and its handle-resolved canonical path are retained directly into private folder
//!   creation without a detached canonicalization. The folders above the local application data folder belong to
//!   the operating system's profile, and the supported account model is assumed to keep other users who are not
//!   administrators from changing them. That is not inspected.
//!
//! The new folder gets an unused name and must then be a real folder owned by this user that nobody else may use:
//! mode `0700` with no access control list granting more on Unix. On Windows both the private folder and the
//! exclusive copy are created with explicit current-user ownership and a protected access list allowing only this
//! user (never relying on the ambient token default owner); the folder is kept open without delete sharing during
//! the check, and the copy's handle identity and owner are verified before writing and closing it. Cleanup after
//! partial initialization or ordinary drop removes an entry only after proving its handle, type, owner and intact
//! identity; unverified entries are left untouched.
//! A verified removal that still fails after a short retry leaves the entry private to this user. Code running as
//! the same user, administrators and root are outside the guarantee.

use super::error::{FsxError, FsxErrorKind};
use std::ffi::OsStr;
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

#[cfg(test)]
mod rule_tests;
mod rules;
#[cfg(test)]
mod tests;
#[cfg(unix)]
mod unix;
#[cfg(windows)]
mod windows;
#[cfg(any(windows, test))]
mod windows_rules;

#[cfg(unix)]
use unix as platform;
#[cfg(windows)]
use windows as platform;

/// How many fresh names are tried when a private folder name is already taken.
const NAME_ATTEMPTS: usize = 16;

/// Runs `check` on a private copy of `bytes` named `name`, in a new private folder inside the per-user location.
///
/// A location or private folder whose authority cannot be established is an error ([`FsxErrorKind::NotOwned`]
/// when refused, [`FsxErrorKind::Io`] when the system fails), and `check` never runs. Otherwise the check's own
/// verdict is returned. Nothing outside the private folder is created or changed.
pub(crate) fn validate_private_copy<T, E>(
    oah_home: &Path,
    name: &OsStr,
    bytes: &[u8],
    check: impl FnOnce(&Path) -> Result<T, E>,
) -> Result<Result<T, E>, FsxError> {
    validate_selected(platform::location()?, oah_home, name, bytes, check)
}

/// [`validate_private_copy`] in an explicit test location `base`.
#[cfg(test)]
fn validate_in<T, E>(
    base: &Path,
    oah_home: &Path,
    name: &OsStr,
    bytes: &[u8],
    check: impl FnOnce(&Path) -> Result<T, E>,
) -> Result<Result<T, E>, FsxError> {
    validate_selected(
        platform::open_base_for_test(base)?,
        oah_home,
        name,
        bytes,
        check,
    )
}

/// Runs `check` inside an already-verified, handle-anchored `base` location.
fn validate_selected<T, E>(
    base: platform::Location,
    oah_home: &Path,
    name: &OsStr,
    bytes: &[u8],
    check: impl FnOnce(&Path) -> Result<T, E>,
) -> Result<Result<T, E>, FsxError> {
    if !base.path().is_absolute() {
        return Err(refuse(
            base.path(),
            "the private check location is not an absolute path",
        ));
    }
    match std::fs::canonicalize(oah_home) {
        Ok(home) if base.path().starts_with(&home) => {
            return Err(refuse(
                base.path(),
                "the private check location is inside OAH_HOME",
            ));
        }
        Err(error) if error.kind() != io::ErrorKind::NotFound => {
            return Err(io_error(oah_home, "cannot resolve OAH_HOME", error));
        }
        _ => {}
    }
    let mut private = platform::Private::create(base)?;
    let copy = private.write(name, bytes)?;
    Ok(check(&copy))
}

/// A folder name nobody else is expected to hold: the process id, a counter and a per-process random value.
fn unique_name() -> String {
    use std::hash::BuildHasher;
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let count = COUNTER.fetch_add(1, Ordering::Relaxed);
    let random = std::collections::hash_map::RandomState::new().hash_one(count);
    format!("oah-validate.{}.{count}.{random:016x}", std::process::id())
}

fn refuse(path: &Path, why: &str) -> FsxError {
    FsxError::new(FsxErrorKind::NotOwned, path, why)
}

fn io_error(path: &Path, what: &str, error: io::Error) -> FsxError {
    FsxError::io(FsxErrorKind::Io, path, what, error)
}
