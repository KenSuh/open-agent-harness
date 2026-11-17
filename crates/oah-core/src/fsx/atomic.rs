// 원자적 바이트 쓰기: 같은 폴더의 임시 파일에 쓰고 디스크에 내린 뒤 바꿔 넣고, 되읽어 같은지 확인한다.
use super::error::{FsxError, FsxErrorKind};
use sha2::{Digest, Sha256};
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// How long a replace or read retries a transient Windows sharing failure (another process has the file open).
const RETRY_FOR: Duration = Duration::from_secs(2);
const RETRY_SLEEP: Duration = Duration::from_millis(50);

/// The SHA-256 of a file's bytes.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Fingerprint([u8; 32]);

impl Fingerprint {
    pub fn of(bytes: &[u8]) -> Self {
        let digest = Sha256::digest(bytes);
        let mut out = [0u8; 32];
        out.copy_from_slice(&digest);
        Fingerprint(out)
    }

    pub fn as_bytes(&self) -> &[u8; 32] {
        &self.0
    }

    /// Lowercase hexadecimal, 64 characters.
    pub fn to_hex(&self) -> String {
        hex(&self.0)
    }
}

impl fmt::Debug for Fingerprint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Fingerprint({})", self.to_hex())
    }
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

/// Proof that a write landed: the path and the fingerprint of the bytes read back from it.
///
/// Only [`write_atomic`] and [`publish_new`] create one, after the read-back matched, so holding a receipt means
/// the write was verified.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteReceipt {
    path: PathBuf,
    fingerprint: Fingerprint,
    len: u64,
}

impl WriteReceipt {
    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn fingerprint(&self) -> &Fingerprint {
        &self.fingerprint
    }

    pub fn len(&self) -> u64 {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
}

/// Reads a whole file; a file that does not exist is `None`. Never creates anything.
pub fn read_optional(path: &Path) -> Result<Option<Vec<u8>>, FsxError> {
    match retry_sharing(|| fs::read(path)) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(FsxError::io(FsxErrorKind::Io, path, "cannot read", error)),
    }
}

/// Replaces `path` with `bytes` atomically and verifies the result.
///
/// The bytes go to a new temporary file in the same folder, which is flushed to disk and renamed over `path`.
/// On Unix the folder is then synced so the rename itself is durable; Windows cannot sync a folder, so there the
/// rename relies on the file system's own journal. Finally the file is read back and compared.
///
/// The folder must exist. On a failure before the rename the temporary file is removed and the destination is
/// untouched ([`FsxErrorKind::Io`]). A failure after the rename is [`FsxErrorKind::Unverified`]: the new bytes may
/// or may not be in place, and nothing is rolled back.
pub fn write_atomic(path: &Path, bytes: &[u8]) -> Result<WriteReceipt, FsxError> {
    let folder = parent_of(path)?;
    let temp = write_temp(&folder, path, bytes)?;
    if let Err(error) = retry_sharing(|| fs::rename(&temp, path)) {
        let _ = fs::remove_file(&temp);
        return Err(FsxError::io(
            FsxErrorKind::Io,
            path,
            "cannot replace",
            error,
        ));
    }
    sync_folder(&folder).map_err(|error| {
        FsxError::io(
            FsxErrorKind::Unverified,
            path,
            "replaced, but the folder could not be synced",
            error,
        )
    })?;
    verify(path, bytes)
}

/// Writes a file that must not exist yet, never replacing one that does.
///
/// The bytes are written and flushed to a temporary sibling, then hard-linked into place, which fails rather
/// than overwrite. On a file system without hard links the file is created exclusively and written in place;
/// an interruption there can leave a partial file, which a later [`publish_new`] reports as existing.
pub fn publish_new(path: &Path, bytes: &[u8]) -> Result<WriteReceipt, FsxError> {
    let folder = parent_of(path)?;
    if fs::symlink_metadata(path).is_ok() {
        return Err(FsxError::new(
            FsxErrorKind::Exists,
            path,
            "already exists and is never overwritten",
        ));
    }
    let temp = write_temp(&folder, path, bytes)?;
    let linked = fs::hard_link(&temp, path);
    let _ = fs::remove_file(&temp);
    match linked {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            return Err(FsxError::new(
                FsxErrorKind::Exists,
                path,
                "already exists and is never overwritten",
            ));
        }
        Err(_) => write_exclusive(path, bytes)?,
    }
    sync_folder(&folder).map_err(|error| {
        FsxError::io(
            FsxErrorKind::Unverified,
            path,
            "written, but the folder could not be synced",
            error,
        )
    })?;
    verify(path, bytes)
}

fn write_exclusive(path: &Path, bytes: &[u8]) -> Result<(), FsxError> {
    let mut file = match OpenOptions::new().write(true).create_new(true).open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            return Err(FsxError::new(
                FsxErrorKind::Exists,
                path,
                "already exists and is never overwritten",
            ));
        }
        Err(error) => return Err(FsxError::io(FsxErrorKind::Io, path, "cannot create", error)),
    };
    let written = file.write_all(bytes).and_then(|()| file.sync_all());
    if let Err(error) = written {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(FsxError::io(FsxErrorKind::Io, path, "cannot write", error));
    }
    Ok(())
}

fn parent_of(path: &Path) -> Result<PathBuf, FsxError> {
    match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => Ok(parent.to_path_buf()),
        _ => Err(FsxError::new(
            FsxErrorKind::Io,
            path,
            "has no parent folder; use an absolute path",
        )),
    }
}

/// Creates a uniquely named temporary file beside `path`, writes `bytes`, flushes them to disk and returns its
/// path. The temporary file is removed again on any failure.
fn write_temp(folder: &Path, path: &Path, bytes: &[u8]) -> Result<PathBuf, FsxError> {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut attempt = 0;
    loop {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|elapsed| elapsed.subsec_nanos())
            .unwrap_or(0);
        let temp = folder.join(format!(
            ".{name}.{}.{nanos:08x}{:04x}.tmp",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed) & 0xffff
        ));
        match OpenOptions::new().write(true).create_new(true).open(&temp) {
            Ok(mut file) => {
                let written = file.write_all(bytes).and_then(|()| file.sync_all());
                drop(file);
                return match written {
                    Ok(()) => Ok(temp),
                    Err(error) => {
                        let _ = fs::remove_file(&temp);
                        Err(FsxError::io(
                            FsxErrorKind::Io,
                            path,
                            "cannot write the temporary file",
                            error,
                        ))
                    }
                };
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists && attempt < 16 => {
                attempt += 1;
            }
            Err(error) => {
                return Err(FsxError::io(
                    FsxErrorKind::Io,
                    path,
                    "cannot create a temporary file beside it",
                    error,
                ));
            }
        }
    }
}

fn verify(path: &Path, expected: &[u8]) -> Result<WriteReceipt, FsxError> {
    let actual = retry_sharing(|| fs::read(path)).map_err(|error| {
        FsxError::io(
            FsxErrorKind::Unverified,
            path,
            "written, but it cannot be read back",
            error,
        )
    })?;
    if actual != expected {
        return Err(FsxError::new(
            FsxErrorKind::Unverified,
            path,
            "written, but the bytes read back differ from the bytes written",
        ));
    }
    Ok(WriteReceipt {
        path: path.to_path_buf(),
        fingerprint: Fingerprint::of(&actual),
        len: actual.len() as u64,
    })
}

#[cfg(unix)]
fn sync_folder(folder: &Path) -> io::Result<()> {
    File::open(folder)?.sync_all()
}

#[cfg(not(unix))]
fn sync_folder(_folder: &Path) -> io::Result<()> {
    // Windows cannot open a folder for syncing through the standard library; NTFS journals the rename.
    Ok(())
}

/// Retries an operation for a short while when Windows reports a sharing violation (another process has the
/// file open), which clears by itself. Elsewhere it runs once.
fn retry_sharing<T>(mut operation: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    let deadline = Instant::now() + RETRY_FOR;
    loop {
        match operation() {
            Err(error) if cfg!(windows) && is_transient_sharing(&error) => {
                if Instant::now() >= deadline {
                    return Err(error);
                }
                std::thread::sleep(RETRY_SLEEP);
            }
            other => return other,
        }
    }
}

fn is_transient_sharing(error: &io::Error) -> bool {
    // ERROR_ACCESS_DENIED (5), ERROR_SHARING_VIOLATION (32) and ERROR_LOCK_VIOLATION (33).
    error.kind() == io::ErrorKind::PermissionDenied || matches!(error.raw_os_error(), Some(32 | 33))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_hex_is_the_sha256_of_the_bytes() {
        assert_eq!(
            Fingerprint::of(b"abc").to_hex(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            Fingerprint::of(b"").to_hex(),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn a_path_without_a_parent_is_refused_before_any_write() {
        let error = write_atomic(Path::new("relative-name"), b"x").unwrap_err();
        assert_eq!(error.kind(), FsxErrorKind::Io);
        assert!(!Path::new("relative-name").exists());
    }
}
