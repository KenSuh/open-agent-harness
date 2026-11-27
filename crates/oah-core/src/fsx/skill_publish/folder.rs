// 프로젝트 안 실제 폴더 핸들: 링크·정션을 따라가지 않고 나열·읽기·새 폴더·새 파일 게시만 한다.
//! A real folder inside a selected project, held open.
//!
//! [`ProjectRoot::folder`] gives the project folder; [`Folder::folder`] steps into a child. Every name is one
//! portable component resolved against the open handle, so a symbolic link, a junction, another reparse point, a
//! hard-linked file or a different volume is refused instead of followed. Nothing here replaces, moves or removes
//! a file: the two writers make a new folder or a complete new file and fail when the name is taken.

use super::{ProjectRoot, PublishError};

#[cfg(any(target_os = "macos", windows))]
use super::native::{self, FileAccess, Kind};
#[cfg(any(target_os = "macos", windows))]
use crate::review::identity::{portable_component_key, portable_paths};

type Result<T> = std::result::Result<T, PublishError>;

/// [`Folder::lookup`] reads at most this many names of one folder.
#[cfg(any(target_os = "macos", windows))]
const LOOKUP_ENTRIES_MAX: usize = 200_000;

/// What the listing says a name is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum EntryKind {
    Folder,
    File,
    /// A symbolic link, a junction or another reparse point, a device, or a name that is not Unicode.
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Entry {
    pub name: String,
    pub kind: EntryKind,
}

/// The names of a folder in byte order, and whether it holds more than the limit asked for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Listing {
    pub entries: Vec<Entry>,
    pub truncated: bool,
}

/// A bounded read: the whole file, or only the fact that it is larger than the limit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Content {
    Bytes(Vec<u8>),
    TooLarge,
}

#[derive(Debug, Clone)]
pub struct Folder {
    #[cfg(any(target_os = "macos", windows))]
    dir: native::Dir,
}

/// One portable path component: no separator, no device or reserved name, no trailing dot or space.
#[cfg(any(target_os = "macos", windows))]
fn leaf(name: &str) -> Result<()> {
    if name.contains('/') || portable_paths(std::slice::from_ref(&name.to_owned())).is_err() {
        return Err(PublishError::UnsafePath);
    }
    Ok(())
}

#[cfg(any(target_os = "macos", windows))]
impl ProjectRoot {
    /// The project folder, after checking again that it is the folder that was selected.
    pub fn folder(&self) -> Result<Folder> {
        self.with_project(|dir| Ok(Folder { dir: dir.clone() }))
    }
}

#[cfg(any(target_os = "macos", windows))]
impl Folder {
    /// A digest of the identity of this folder and of every folder above it. It changes when any of them is
    /// replaced, and stays the same across processes while they are not.
    pub fn stamp(&self) -> Result<String> {
        self.dir.stamp()
    }

    /// Fails when this folder or one above it is no longer the object that was opened under its name.
    pub fn check(&self) -> Result<()> {
        self.dir.check()
    }

    pub fn list(&self, limit: usize) -> Result<Listing> {
        let (found, truncated) = self.dir.entries(limit)?;
        let mut entries: Vec<Entry> = found
            .into_iter()
            .map(|(name, kind)| match name.into_string() {
                Ok(name) => Entry {
                    name,
                    kind: match kind {
                        Kind::Dir => EntryKind::Folder,
                        Kind::File => EntryKind::File,
                        Kind::Other => EntryKind::Other,
                    },
                },
                Err(name) => Entry {
                    name: name.to_string_lossy().into_owned(),
                    kind: EntryKind::Other,
                },
            })
            .collect();
        entries.sort();
        Ok(Listing { entries, truncated })
    }

    /// The entry `name` names on a file system that ignores case and Unicode form, with its spelling on disk.
    /// `UnsafePath` when two entries share the name that way.
    pub fn lookup(&self, name: &str) -> Result<Option<Entry>> {
        leaf(name)?;
        let key = portable_component_key(name);
        let mut found: Option<Entry> = None;
        let listing = self.list(LOOKUP_ENTRIES_MAX)?;
        if listing.truncated {
            return Err(PublishError::Io(std::io::Error::other(
                "the folder holds too many entries to look a name up in",
            )));
        }
        for entry in listing.entries {
            if portable_component_key(&entry.name) == key {
                if found.is_some() {
                    return Err(PublishError::UnsafePath);
                }
                found = Some(entry);
            }
        }
        Ok(found)
    }

    /// Opens the real child folder spelled exactly `name`, on the same volume.
    pub fn folder(&self, name: &str) -> Result<Folder> {
        leaf(name)?;
        Ok(Folder {
            dir: self.dir.child(name)?,
        })
    }

    /// Makes the child folder `name`. `Occupied` when the name is taken.
    pub fn create_folder(&self, name: &str) -> Result<Folder> {
        leaf(name)?;
        Ok(Folder {
            dir: self.dir.create_shared_dir(name)?,
        })
    }

    /// Reads the regular file spelled exactly `name` when it holds at most `limit` bytes. Other programs can keep
    /// writing or renaming the file meanwhile; a caller that needs exact bytes compares a digest.
    pub fn read(&self, name: &str, limit: u64) -> Result<Content> {
        use std::io::Read;

        leaf(name)?;
        let file = self.dir.regular(name, FileAccess::ReadShared)?;
        if file.metadata()?.len() > limit {
            return Ok(Content::TooLarge);
        }
        let mut bytes = Vec::new();
        file.take(limit.saturating_add(1)).read_to_end(&mut bytes)?;
        if u64::try_from(bytes.len()).is_ok_and(|len| len <= limit) {
            Ok(Content::Bytes(bytes))
        } else {
            Ok(Content::TooLarge)
        }
    }

    /// Writes the complete new file `name`, through a temporary sibling and a rename that never replaces.
    /// `Occupied` when the name is taken; the existing file is not opened.
    pub fn publish_new(&self, name: &str, bytes: &[u8]) -> Result<()> {
        leaf(name)?;
        self.dir.publish_new(name, bytes)
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
impl ProjectRoot {
    pub fn folder(&self) -> Result<Folder> {
        Err(PublishError::Unsupported)
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
impl Folder {
    pub fn stamp(&self) -> Result<String> {
        Err(PublishError::Unsupported)
    }
    pub fn check(&self) -> Result<()> {
        Err(PublishError::Unsupported)
    }
    pub fn list(&self, _: usize) -> Result<Listing> {
        Err(PublishError::Unsupported)
    }
    pub fn lookup(&self, _: &str) -> Result<Option<Entry>> {
        Err(PublishError::Unsupported)
    }
    pub fn folder(&self, _: &str) -> Result<Folder> {
        Err(PublishError::Unsupported)
    }
    pub fn create_folder(&self, _: &str) -> Result<Folder> {
        Err(PublishError::Unsupported)
    }
    pub fn read(&self, _: &str, _: u64) -> Result<Content> {
        Err(PublishError::Unsupported)
    }
    pub fn publish_new(&self, _: &str, _: &[u8]) -> Result<()> {
        Err(PublishError::Unsupported)
    }
}
