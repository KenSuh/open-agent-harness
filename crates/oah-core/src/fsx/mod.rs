// 파일 시스템 기본 동작: 원자적 쓰기와 되읽기, 파일별 짧은 잠금, 링크·정션 관리, 자기 쓰기 기록을 한곳에 둔다.
//! File-system primitives every OAH writer shares.
//!
//! * [`write_atomic`] replaces a file through a temporary sibling, flushes it to disk and reads it back;
//!   [`publish_new`] does the same for a file that must not exist yet and is never overwritten;
//!   [`publish_executable`] runs a caller's check on the temporary executable before it may replace the file.
//! * `validate_private_copy` runs a caller's check on a private copy of executable bytes in a new folder of
//!   its own inside the operating system's per-user location, never under `OAH_HOME` (CONTRACTS C7).
//! * [`FileLock`] is the short per-file writer lock on `<file>.lock`. Readers never lock.
//! * [`links`] creates, inspects and removes managed links: symlinks, Windows directory junctions and the copy
//!   fallback.
//! * [`SelfWrites`] remembers OAH's own verified writes so a later watcher can tell them from external edits.
//!
//! Every failure is a typed [`FsxError`]; nothing here retries forever or claims a durability it cannot prove.

mod atomic;
mod error;
mod identity;
pub mod links;
mod lock;
mod ownership;
mod selfwrite;
mod staging;
mod validation;

pub(crate) use atomic::read_regular_optional;
pub use atomic::{
    Fingerprint, WriteReceipt, publish_executable, publish_new, read_optional, write_atomic,
};
pub use error::{FsxError, FsxErrorKind};
pub use lock::{FileLock, LockOptions};
pub use selfwrite::{SelfWriteMatch, SelfWrites};
pub(crate) use validation::validate_private_copy;
