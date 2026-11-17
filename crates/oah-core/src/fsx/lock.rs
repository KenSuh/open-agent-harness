// 파일별 짧은 쓰기 잠금: `<파일>.lock` 을 운영체제 잠금으로 잡고, 놓을 때 지워서 프로젝트에 흔적을 남기지 않는다.
use super::error::{FsxError, FsxErrorKind};
use std::ffi::OsString;
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// How long [`FileLock::acquire`] waits for another writer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LockOptions {
    /// The whole bounded wait; after it the lock is reported busy.
    pub wait: Duration,
    /// The pause between attempts.
    pub poll: Duration,
}

impl Default for LockOptions {
    /// Ten seconds in steps of 50 ms, as the v0.4.0 state lock waited.
    fn default() -> Self {
        LockOptions {
            wait: Duration::from_secs(10),
            poll: Duration::from_millis(50),
        }
    }
}

/// The short advisory writer lock of one file, held on `<file>.lock` for one read-validate-modify-publish cycle.
///
/// The operating system releases the lock when the holder exits, so a crash never leaves a held lock. The lock
/// file is removed when the lock is released; one left by a crash is simply locked again by the next writer.
/// Readers never take it. Only OAH writers that use this lock exclude each other; any other program can still
/// change the file, which is why saves re-check the bytes they started from.
#[derive(Debug)]
pub struct FileLock {
    target: PathBuf,
    lock_path: PathBuf,
    file: Option<File>,
}

impl FileLock {
    /// Takes the writer lock of `target`, retrying until `options.wait` has passed.
    ///
    /// Fails with [`FsxErrorKind::Busy`] when another writer held it for the whole wait, and with
    /// [`FsxErrorKind::Io`] when the lock file cannot be opened (for example, the folder is missing or read-only).
    pub fn acquire(target: &Path, options: LockOptions) -> Result<FileLock, FsxError> {
        let lock_path = lock_path_of(target)?;
        let deadline = Instant::now() + options.wait;
        loop {
            match sys::try_acquire(&lock_path) {
                Ok(Attempt::Held(file)) => {
                    return Ok(FileLock {
                        target: target.to_path_buf(),
                        lock_path,
                        file: Some(file),
                    });
                }
                Ok(Attempt::Retry) => continue,
                Ok(Attempt::Contended(last)) => {
                    if Instant::now() >= deadline {
                        return Err(match last {
                            Some(error) => FsxError::io(
                                FsxErrorKind::Io,
                                target,
                                "cannot open the lock file",
                                error,
                            ),
                            None => FsxError::new(
                                FsxErrorKind::Busy,
                                target,
                                format!(
                                    "another writer held the lock for {} ms; try again",
                                    options.wait.as_millis()
                                ),
                            ),
                        });
                    }
                    std::thread::sleep(options.poll);
                }
                Err(error) => {
                    return Err(FsxError::io(
                        FsxErrorKind::Io,
                        target,
                        "cannot take the lock",
                        error,
                    ));
                }
            }
        }
    }

    /// The file this lock protects.
    pub fn target(&self) -> &Path {
        &self.target
    }

    /// The `<file>.lock` path the lock is held on.
    pub fn lock_path(&self) -> &Path {
        &self.lock_path
    }

    /// Releases the lock and removes the lock file, reporting a failure to remove it.
    pub fn release(mut self) -> Result<(), FsxError> {
        match self.file.take() {
            Some(file) => sys::release(&self.lock_path, file).map_err(|error| {
                FsxError::io(
                    FsxErrorKind::Io,
                    &self.target,
                    "released, but the lock file could not be removed",
                    error,
                )
            }),
            None => Ok(()),
        }
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        if let Some(file) = self.file.take() {
            let _ = sys::release(&self.lock_path, file);
        }
    }
}

fn lock_path_of(target: &Path) -> Result<PathBuf, FsxError> {
    match (target.parent(), target.file_name()) {
        (Some(parent), Some(name)) if !parent.as_os_str().is_empty() => {
            let mut lock_name = OsString::from(name);
            lock_name.push(".lock");
            Ok(parent.join(lock_name))
        }
        _ => Err(FsxError::new(
            FsxErrorKind::Io,
            target,
            "has no parent folder; use an absolute path",
        )),
    }
}

enum Attempt {
    /// The lock is held through this open file.
    Held(File),
    /// Someone else holds it. On Windows the last open error is kept when it was not a plain sharing conflict.
    Contended(Option<io::Error>),
    /// The lock file was replaced between opening and locking; try again at once.
    Retry,
}

#[cfg(unix)]
mod sys {
    use super::Attempt;
    use std::fs::{self, File, OpenOptions, TryLockError};
    use std::io;
    use std::os::unix::fs::MetadataExt;
    use std::path::Path;

    pub(super) fn try_acquire(lock_path: &Path) -> io::Result<Attempt> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(lock_path)?;
        match file.try_lock() {
            Ok(()) => {
                // A releasing holder removes the path before unlocking, so the file we locked may already be
                // unlinked; only a lock on the file the path still names counts.
                if names_this_file(lock_path, &file)? {
                    Ok(Attempt::Held(file))
                } else {
                    Ok(Attempt::Retry)
                }
            }
            Err(TryLockError::WouldBlock) => Ok(Attempt::Contended(None)),
            Err(TryLockError::Error(error)) => Err(error),
        }
    }

    pub(super) fn release(lock_path: &Path, file: File) -> io::Result<()> {
        // Remove the path while still holding the lock, and only when it still names our file.
        let removed = match names_this_file(lock_path, &file) {
            Ok(true) => fs::remove_file(lock_path),
            Ok(false) => Ok(()),
            Err(error) => Err(error),
        };
        drop(file);
        removed
    }

    fn names_this_file(lock_path: &Path, file: &File) -> io::Result<bool> {
        let held = file.metadata()?;
        match fs::metadata(lock_path) {
            Ok(named) => Ok(named.dev() == held.dev() && named.ino() == held.ino()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error),
        }
    }
}

#[cfg(windows)]
mod sys {
    use super::Attempt;
    use std::fs::{File, OpenOptions};
    use std::io;
    use std::os::windows::fs::OpenOptionsExt;
    use std::path::Path;

    const GENERIC_READ: u32 = 0x8000_0000;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    const DELETE: u32 = 0x0001_0000;
    const FILE_FLAG_DELETE_ON_CLOSE: u32 = 0x0400_0000;
    const ERROR_ACCESS_DENIED: i32 = 5;
    const ERROR_SHARING_VIOLATION: i32 = 32;

    pub(super) fn try_acquire(lock_path: &Path) -> io::Result<Attempt> {
        // An exclusive open (no sharing) is the lock; Windows deletes the file when the handle closes, also
        // when the process dies.
        let opened = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .access_mode(GENERIC_READ | GENERIC_WRITE | DELETE)
            .share_mode(0)
            .custom_flags(FILE_FLAG_DELETE_ON_CLOSE)
            .open(lock_path);
        match opened {
            Ok(file) => Ok(Attempt::Held(file)),
            Err(error) if error.raw_os_error() == Some(ERROR_SHARING_VIOLATION) => {
                Ok(Attempt::Contended(None))
            }
            // A lock file whose holder just closed it is "delete pending" for a moment and refuses opens with
            // access denied; a lasting access denial is a real permission problem and is reported as such.
            Err(error) if error.raw_os_error() == Some(ERROR_ACCESS_DENIED) => {
                Ok(Attempt::Contended(Some(error)))
            }
            Err(error) => Err(error),
        }
    }

    pub(super) fn release(_lock_path: &Path, file: File) -> io::Result<()> {
        drop(file);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_lock_file_sits_beside_the_target_with_a_lock_suffix() {
        let path = lock_path_of(Path::new("/project/.oah/config.toml")).unwrap();
        assert_eq!(path, Path::new("/project/.oah/config.toml.lock"));
    }

    #[test]
    fn a_target_without_a_folder_is_refused() {
        let error = lock_path_of(Path::new("config.toml")).unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::Io);
    }
}
