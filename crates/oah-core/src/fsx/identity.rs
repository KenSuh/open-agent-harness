// 항목 신원 값: 링크를 따라가지 않고 장치·항목 번호를 잠깐 읽어 값으로만 남기며, 열린 핸들을 보관하지 않는다.
//! Entry identities for one operation's own bookkeeping, and the rename that never replaces.
//!
//! An [`EntryId`] is a plain value (device and entry number on Unix, volume serial and file index on Windows)
//! read without following links. Holding one keeps nothing open, so an operation may record one per entry it
//! makes without using descriptors. It is never an ownership record: it only lets an operation tell, before it
//! removes or publishes something, that the entry at a path is still the one it made a moment ago.

use std::fs::File;
use std::io;
use std::path::Path;

/// Which entry a path named when the value was read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct EntryId {
    volume: u64,
    index: u64,
}

impl EntryId {
    /// The entry at `path` itself, never what a link there points to.
    #[cfg(unix)]
    pub(super) fn of(path: &Path) -> io::Result<EntryId> {
        use std::os::unix::fs::MetadataExt;
        let metadata = std::fs::symlink_metadata(path)?;
        Ok(EntryId {
            volume: metadata.dev(),
            index: metadata.ino(),
        })
    }

    /// The entry at `path` itself, never what a link there points to.
    #[cfg(windows)]
    pub(super) fn of(path: &Path) -> io::Result<EntryId> {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        // Attribute-only access opens folders and junctions themselves; the handle closes before returning.
        let file = std::fs::OpenOptions::new()
            .access_mode(0)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)?;
        EntryId::of_file(&file)
    }

    #[cfg(not(any(unix, windows)))]
    pub(super) fn of(_path: &Path) -> io::Result<EntryId> {
        Err(io::ErrorKind::Unsupported.into())
    }

    /// The entry an open file refers to, read through that file without opening anything else.
    #[cfg(unix)]
    pub(super) fn of_file(file: &File) -> io::Result<EntryId> {
        use std::os::unix::fs::MetadataExt;
        let metadata = file.metadata()?;
        Ok(EntryId {
            volume: metadata.dev(),
            index: metadata.ino(),
        })
    }

    /// The entry an open file refers to, read through that file without opening anything else.
    #[cfg(windows)]
    pub(super) fn of_file(file: &File) -> io::Result<EntryId> {
        let information = winapi_util::file::information(file)?;
        Ok(EntryId {
            volume: information.volume_serial_number(),
            index: information.file_index(),
        })
    }

    #[cfg(not(any(unix, windows)))]
    pub(super) fn of_file(_file: &File) -> io::Result<EntryId> {
        Err(io::ErrorKind::Unsupported.into())
    }
}

/// What a path names now, compared with an entry recorded earlier in the same operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Now {
    Same,
    Other,
    Missing,
}

/// Compares `path` with a recorded entry. Only a missing entry is an answer; any other failure to read the
/// identity stays an error and is never taken for a replacement.
pub(super) fn now(path: &Path, recorded: EntryId) -> io::Result<Now> {
    match EntryId::of(path) {
        Ok(found) if found == recorded => Ok(Now::Same),
        Ok(_) => Ok(Now::Other),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(Now::Missing),
        Err(error) => Err(error),
    }
}

/// Renames `from` to `to` without ever replacing what is at `to`; an occupied `to` is `AlreadyExists`.
pub(super) fn rename_new(from: &Path, to: &Path) -> io::Result<()> {
    #[cfg(any(target_os = "linux", target_vendor = "apple"))]
    {
        use rustix::fs::{CWD, RenameFlags, renameat_with};
        renameat_with(CWD, from, CWD, to, RenameFlags::NOREPLACE)?;
        Ok(())
    }
    #[cfg(windows)]
    {
        windows::move_new(from, to)
    }
    #[cfg(not(any(target_os = "linux", target_vendor = "apple", windows)))]
    {
        let _ = (from, to);
        Err(io::ErrorKind::Unsupported.into())
    }
}

#[cfg(windows)]
mod windows {
    use std::ffi::OsString;
    use std::io;
    use std::os::windows::ffi::OsStrExt;
    use std::path::{Component, Path, Prefix};

    /// No `MOVEFILE_REPLACE_EXISTING`: an existing target, file or folder, is refused.
    const NO_REPLACE: u32 = 0;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn MoveFileExW(existing: *const u16, new: *const u16, flags: u32) -> i32;
    }

    /// `std::fs::rename` asks Windows to replace an existing target; this never does. The refusal is
    /// ERROR_ALREADY_EXISTS or ERROR_FILE_EXISTS, both `AlreadyExists`.
    pub(super) fn move_new(from: &Path, to: &Path) -> io::Result<()> {
        let from = wide(from);
        let to = wide(to);
        // SAFETY: both arguments are NUL-terminated UTF-16 buffers that live until the call returns.
        if unsafe { MoveFileExW(from.as_ptr(), to.as_ptr(), NO_REPLACE) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    /// The path as a NUL-terminated wide string. A plain drive path takes the verbatim form, which lifts the
    /// 260-character limit; any other form is passed exactly as given.
    fn wide(path: &Path) -> Vec<u16> {
        let mut components = path.components();
        let verbatim = match (components.next(), components.next()) {
            (Some(Component::Prefix(prefix)), Some(Component::RootDir))
                if matches!(prefix.kind(), Prefix::Disk(_)) =>
            {
                let mut text = OsString::from(r"\\?\");
                text.push(prefix.as_os_str());
                let mut plain = true;
                for component in components {
                    if let Component::Normal(part) = component {
                        text.push(r"\");
                        text.push(part);
                    } else {
                        plain = false;
                        break;
                    }
                }
                plain.then_some(text)
            }
            _ => None,
        };
        verbatim
            .as_deref()
            .unwrap_or(path.as_os_str())
            .encode_wide()
            .chain(Some(0))
            .collect()
    }
}
