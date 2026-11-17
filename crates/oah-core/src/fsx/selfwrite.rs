// 자기 쓰기 기록: 검증된 쓰기의 경로와 내용 지문만 남겨, 나중의 파일 변경 알림이 OAH 자신의 것인지 가린다.
use super::atomic::{Fingerprint, WriteReceipt};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// How many recent own writes of one path are remembered; older ones are superseded anyway.
const KEEP_PER_PATH: usize = 16;

/// What a file-change event turned out to be.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SelfWriteMatch {
    /// The bytes on disk are bytes OAH itself wrote there; the event is OAH's own echo.
    Own,
    /// The bytes differ from every write OAH recorded for that path; the event must be reported.
    External,
}

/// In-memory registry of OAH's own verified writes, keyed by path and content fingerprint.
///
/// Only a [`WriteReceipt`] can be recorded, so only writes that were read back and verified are ever trusted.
/// Paths are compared exactly as given; record and observe one file through the same absolute path.
/// Nothing is persisted: the registry lives as long as the process that wrote.
#[derive(Debug, Default)]
pub struct SelfWrites {
    entries: Mutex<HashMap<PathBuf, Vec<Fingerprint>>>,
}

impl SelfWrites {
    pub fn new() -> Self {
        SelfWrites::default()
    }

    /// Remembers a verified write.
    pub fn record(&self, receipt: &WriteReceipt) {
        let mut entries = self.lock();
        let list = entries.entry(receipt.path().to_path_buf()).or_default();
        list.push(*receipt.fingerprint());
        if list.len() > KEEP_PER_PATH {
            let excess = list.len() - KEEP_PER_PATH;
            list.drain(..excess);
        }
    }

    /// Classifies a change event for `path` whose current content is `bytes`.
    ///
    /// When the bytes equal a recorded write, that record and every older one for the path are cleared and the
    /// event is [`SelfWriteMatch::Own`]. Otherwise the file has moved on past OAH's writes: the records for the
    /// path are cleared and the event is [`SelfWriteMatch::External`], so it is never suppressed.
    pub fn observe(&self, path: &Path, bytes: &[u8]) -> SelfWriteMatch {
        let fingerprint = Fingerprint::of(bytes);
        let mut entries = self.lock();
        let Some(list) = entries.get_mut(path) else {
            return SelfWriteMatch::External;
        };
        let outcome = match list.iter().rposition(|known| *known == fingerprint) {
            Some(index) => {
                list.drain(..=index);
                SelfWriteMatch::Own
            }
            None => {
                list.clear();
                SelfWriteMatch::External
            }
        };
        if list.is_empty() {
            entries.remove(path);
        }
        outcome
    }

    /// How many own writes of `path` are still expected to echo back.
    pub fn pending(&self, path: &Path) -> usize {
        self.lock().get(path).map_or(0, Vec::len)
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<PathBuf, Vec<Fingerprint>>> {
        // A panic while holding the guard cannot leave the map half-updated in a harmful way; keep using it.
        self.entries
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}
