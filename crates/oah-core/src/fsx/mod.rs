// 파일 시스템 기본 동작: 원자적 쓰기와 되읽기, 파일별 짧은 잠금, 링크·정션 관리, 자기 쓰기 기록을 한곳에 둔다.
//! File-system primitives every OAH writer shares.
//!
//! * [`write_atomic`] replaces a file through a temporary sibling, flushes it to disk and reads it back;
//!   [`publish_new`] does the same for a file that must not exist yet and is never overwritten.
//! * [`FileLock`] is the short per-file writer lock on `<file>.lock`. Readers never lock.
//! * [`links`] creates, inspects and removes managed links: symlinks, Windows directory junctions and the copy
//!   fallback.
//! * [`SelfWrites`] remembers OAH's own verified writes so a later watcher can tell them from external edits.
//!
//! Every failure is a typed [`FsxError`]; nothing here retries forever or claims a durability it cannot prove.

mod atomic;
mod error;
pub mod links;
mod lock;
mod selfwrite;

pub use atomic::{Fingerprint, WriteReceipt, publish_new, read_optional, write_atomic};
pub use error::{FsxError, FsxErrorKind};
pub use lock::{FileLock, LockOptions};
pub use selfwrite::{SelfWriteMatch, SelfWrites};
