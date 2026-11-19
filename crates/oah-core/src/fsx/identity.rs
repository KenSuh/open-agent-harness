// 항목 신원 값: 링크를 따라가지 않고 장치·항목 번호를 잠깐 읽어 값으로만 남기며, 열린 핸들을 보관하지 않는다.
//! Plain entry identities for legacy link records.
//!
//! An [`EntryIdentity`] stores platform-tagged evidence without holding an open handle.

#[cfg(all(test, windows))]
use std::fs::File;
#[cfg(test)]
use std::io;
#[cfg(test)]
use std::path::Path;

/// Historical identity of a managed entry itself, without following a link or keeping an OS handle open.
///
/// The caller retains the platform tag and all numbers unchanged with its record. This is trusted caller
/// evidence, not authentication. Only the current platform's nonzero inode/file index is supported. Filesystems
/// FAT directory evidence also retains its first allocated cluster: its directory-slot index can change on
/// rename and be reused while the original still exists. Allocation changes refuse ownership. Numbers may be
/// reused after deletion, so this cannot detect that reuse or close a concurrent check-to-path-use window.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryIdentity {
    Unix {
        device: u64,
        inode: u64,
    },
    Windows {
        volume_serial: u64,
        file_index: u64,
    },
    WindowsFatDirectory {
        volume_serial: u64,
        file_index: u64,
        first_cluster: u64,
    },
}

/// Which entry a path named when the value was read.
#[cfg(test)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct EntryId {
    volume: u64,
    index: u64,
    first_cluster: Option<u64>,
}

#[cfg(test)]
impl EntryId {
    pub(super) fn evidence(self) -> EntryIdentity {
        if cfg!(windows) {
            match self.first_cluster {
                Some(first_cluster) => EntryIdentity::WindowsFatDirectory {
                    volume_serial: self.volume,
                    file_index: self.index,
                    first_cluster,
                },
                None => EntryIdentity::Windows {
                    volume_serial: self.volume,
                    file_index: self.index,
                },
            }
        } else {
            EntryIdentity::Unix {
                device: self.volume,
                inode: self.index,
            }
        }
    }

    /// The entry at `path` itself, never what a link there points to.
    #[cfg(unix)]
    pub(super) fn of(path: &Path) -> io::Result<EntryId> {
        use std::os::unix::fs::MetadataExt;
        let metadata = std::fs::symlink_metadata(path)?;
        Ok(EntryId {
            volume: metadata.dev(),
            index: metadata.ino(),
            first_cluster: None,
        })
    }

    /// The entry at `path` itself, never what a link there points to.
    #[cfg(windows)]
    pub(super) fn of(path: &Path) -> io::Result<EntryId> {
        let file = windows::open(path)?;
        EntryId::of_file(&file)
    }

    #[cfg(not(any(unix, windows)))]
    pub(super) fn of(_path: &Path) -> io::Result<EntryId> {
        Err(io::ErrorKind::Unsupported.into())
    }

    /// The entry an open file refers to, read through that file without opening anything else.
    #[cfg(windows)]
    pub(super) fn of_file(file: &File) -> io::Result<EntryId> {
        let information = winapi_util::file::information(file)?;
        Ok(EntryId {
            volume: information.volume_serial_number(),
            index: information.file_index(),
            first_cluster: windows::directory_cluster(file, information.file_attributes())?,
        })
    }
}

/// One operation's proof across its own move. Windows retains one attribute handle only until completion.
#[cfg(test)]
#[derive(Debug)]
pub(super) struct MoveProof {
    before: EntryId,
    #[cfg(windows)]
    file: File,
}

#[cfg(test)]
impl MoveProof {
    pub(super) fn acquire(path: &Path, before: EntryId) -> io::Result<Option<Self>> {
        #[cfg(windows)]
        {
            let file = windows::open(path)?;
            Ok((EntryId::of_file(&file)? == before).then_some(Self { before, file }))
        }
        #[cfg(not(windows))]
        {
            Ok((now(path, before)? == Now::Same).then_some(Self { before }))
        }
    }

    pub(super) fn after(&self, destination: &Path) -> io::Result<Option<EntryId>> {
        #[cfg(windows)]
        let after = EntryId::of_file(&self.file)?;
        #[cfg(not(windows))]
        let after = self.before;
        if after.volume != self.before.volume || after.first_cluster != self.before.first_cluster {
            return Ok(None);
        }
        Ok((now(destination, after)? == Now::Same).then_some(after))
    }
}

/// What a path names now, compared with an entry recorded earlier in the same operation.
#[cfg(test)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Now {
    Same,
    Other,
    Missing,
}

/// Compares `path` with a recorded entry. Only a missing entry is an answer; any other failure to read the
/// identity stays an error and is never taken for a replacement.
#[cfg(test)]
pub(super) fn now(path: &Path, recorded: EntryId) -> io::Result<Now> {
    match EntryId::of(path) {
        Ok(found) if found == recorded => Ok(Now::Same),
        Ok(_) => Ok(Now::Other),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(Now::Missing),
        Err(error) => Err(error),
    }
}

/// Renames `from` to `to` without ever replacing what is at `to`; an occupied `to` is `AlreadyExists`.
#[cfg(test)]
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

#[cfg(test)]
mod move_tests;
#[cfg(all(test, windows))]
mod windows;
#[cfg(all(test, windows))]
mod tests {
    use super::rename_new;
    use std::fs;
    use std::path::Path;

    #[test]
    fn windows_nonreplacing_rename_contract() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let make = |path: &Path, folder: bool, bytes: &[u8]| {
            if folder {
                fs::create_dir(path).unwrap();
                fs::write(path.join("keep.txt"), bytes).unwrap();
            } else {
                fs::write(path, bytes).unwrap();
            }
        };
        let read = |path: &Path, folder: bool| {
            fs::read(if folder {
                path.join("keep.txt")
            } else {
                path.to_path_buf()
            })
            .unwrap()
        };
        for folder in [false, true] {
            for occupied in [None, Some(false), Some(true)] {
                let from = root.join(format!("원본-{folder}-{occupied:?}"));
                let to = root.join(format!("대상-{folder}-{occupied:?}"));
                make(&from, folder, b"original");
                match occupied {
                    Some(target_folder) => {
                        make(&to, target_folder, b"user entry");
                        rename_new(&from, &to).unwrap_err();
                        assert_eq!(read(&from, folder), b"original");
                        assert_eq!(read(&to, target_folder), b"user entry");
                    }
                    None => {
                        rename_new(&from, &to).unwrap();
                        assert!(!from.exists());
                        assert_eq!(read(&to, folder), b"original");
                    }
                }
            }
        }
        let mut long = root.to_path_buf();
        for _ in 0..4 {
            long.push("d".repeat(80));
        }
        fs::create_dir_all(&long).unwrap();
        let from = long.join("긴-원본.txt");
        let to = long.join("긴-대상.txt");
        fs::write(&from, b"long path").unwrap();
        rename_new(&from, &to).unwrap();
        assert_eq!(fs::read(&to).unwrap(), b"long path");
        rename_new(&to, Path::new("")).unwrap_err();
        assert_eq!(fs::read(&to).unwrap(), b"long path");
    }
}
