use super::{ObjectId, PublishError, os};
use crate::review::identity::portable_component_key;
use std::ffi::{OsStr, OsString};
use std::fs::{File, TryLockError};
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

type Result<T> = std::result::Result<T, PublishError>;

#[derive(Debug, Clone, Copy)]
pub(super) enum FileAccess {
    Read,
    Create,
    Lock,
    Remove,
}

#[derive(Debug, Clone)]
pub(super) struct Dir(Arc<Pinned>);

#[derive(Debug)]
struct Pinned {
    parent: Option<Dir>,
    path: PathBuf,
    file: File,
    id: ObjectId,
}

impl Dir {
    pub(super) fn project(path: &Path) -> Result<Self> {
        let root = os::root(path)?;
        let mut dir = Self::pin(None, root.clone(), os::open_root(&root)?)?;
        for component in path
            .strip_prefix(&root)
            .map_err(|_| PublishError::UnsafePath)?
            .components()
        {
            let Component::Normal(name) = component else {
                return Err(PublishError::UnsafePath);
            };
            dir = dir.open_child(name, false)?;
        }
        os::qualify(dir.file())?;
        dir.outside_home_skills(&std::env::home_dir().ok_or(PublishError::Unsupported)?)?;
        dir.check()?;
        Ok(dir)
    }

    pub(super) fn outside_home_skills(&self, home: &Path) -> Result<()> {
        let home = home.canonicalize()?;
        #[cfg(windows)]
        let home_id = os::inspect_directory_identity(&home)?;
        #[cfg(target_os = "macos")]
        let home_id = os::identity(&os::open_root(&home)?)?;
        let mut branch: Vec<&OsStr> = Vec::new();
        let mut current = Some(self);
        while let Some(dir) = current {
            if *dir.id() == home_id {
                let mut relative = branch
                    .iter()
                    .rev()
                    .map(|s| s.to_str().map(portable_component_key));
                if branch.is_empty()
                    || (matches!(
                        relative.next().flatten().as_deref(),
                        Some(".claude" | ".agents")
                    ) && relative.next().flatten().as_deref() == Some("skills"))
                {
                    return Err(PublishError::UnsafePath);
                }
                break;
            }
            if let Some(name) = dir.path().file_name() {
                branch.push(name);
            }
            current = dir.0.parent.as_ref();
        }
        Ok(())
    }

    fn pin(parent: Option<Dir>, path: PathBuf, file: File) -> Result<Self> {
        let id = os::identity(&file)?;
        Ok(Self(Arc::new(Pinned {
            parent,
            path,
            file,
            id,
        })))
    }

    pub(super) fn path(&self) -> &Path {
        &self.0.path
    }
    pub(super) fn file(&self) -> &File {
        &self.0.file
    }
    pub(super) fn id(&self) -> &ObjectId {
        &self.0.id
    }

    pub(super) fn lineage(&self, root: &Self) -> Result<Vec<ObjectId>> {
        let mut ids = Vec::new();
        let mut next = Some(self);
        while let Some(dir) = next {
            ids.push(dir.id().clone());
            if Arc::ptr_eq(&root.0, &dir.0) {
                return Ok(ids);
            }
            next = dir.0.parent.as_ref();
        }
        Err(PublishError::UnsafePath)
    }

    pub(super) fn check(&self) -> Result<()> {
        let mut dir = self;
        while let Some(parent) = &dir.0.parent {
            os::check_named(parent, dir)?;
            dir = parent;
        }
        Ok(())
    }

    pub(super) fn names(&self) -> Result<Vec<OsString>> {
        os::names(self)
    }

    pub(super) fn contains(&self, name: &str) -> Result<bool> {
        let key = portable_component_key(name);
        let mut exact = false;
        for sibling in self.names()? {
            if sibling == OsStr::new(name) {
                exact = true;
            } else if sibling
                .to_str()
                .is_some_and(|s| portable_component_key(s) == key)
            {
                return Err(PublishError::UnsafePath);
            }
        }
        Ok(exact)
    }

    fn open_child(&self, name: &OsStr, same_volume: bool) -> Result<Self> {
        let file = os::open_dir(self, name)?;
        let dir = Self::pin(Some(self.clone()), self.path().join(name), file)?;
        if same_volume && self.id().volume != dir.id().volume {
            return Err(PublishError::Unsupported);
        }
        Ok(dir)
    }

    pub(super) fn child(&self, name: &str) -> Result<Self> {
        self.open_child(name.as_ref(), true)
    }

    pub(super) fn directory(&self, name: &str, create: bool) -> Result<Option<Self>> {
        if !self.contains(name)? {
            if !create {
                return Ok(None);
            }
            self.check()?;
            match os::mkdir(self, name) {
                Ok(()) => self.sync()?,
                Err(PublishError::Occupied) if self.contains(name)? => {}
                Err(err) => return Err(err),
            }
        }
        self.child(name).map(Some)
    }

    pub(super) fn create_dir(&self, name: &str) -> Result<Self> {
        self.check()?;
        os::mkdir(self, name)?;
        self.sync()?;
        self.child(name)
    }

    pub(super) fn regular(&self, name: &str, access: FileAccess) -> Result<File> {
        let file = os::regular(self, name, access)?;
        if os::identity(&file)?.volume != self.id().volume {
            return Err(PublishError::Unsupported);
        }
        Ok(file)
    }

    pub(super) fn write_new(&self, name: &str, data: &[u8]) -> Result<File> {
        self.check()?;
        let mut file = self.regular(name, FileAccess::Create)?;
        file.write_all(data)?;
        file.sync_all()?;
        self.sync()?;
        Ok(file)
    }

    pub(super) fn lock(&self) -> Result<File> {
        self.check()?;
        // A persistent, never-unlinked inode keeps every cooperating process on the same OS lock.
        self.contains("writer.lock")?;
        let file = self.regular("writer.lock", FileAccess::Lock)?;
        if file.metadata()?.len() != 0 {
            return Err(PublishError::UnsafePath);
        }
        file.try_lock().map_err(|err| match err {
            TryLockError::WouldBlock => PublishError::Busy,
            TryLockError::Error(err) => PublishError::from(err),
        })?;
        if !self.contains("writer.lock")?
            || os::identity(&file)?
                != os::identity(&self.regular("writer.lock", FileAccess::Lock)?)?
        {
            return Err(PublishError::UnsafePath);
        }
        Ok(file)
    }

    pub(super) fn with_lock<T>(&self, operation: impl FnOnce() -> Result<T>) -> Result<T> {
        let file = self.lock()?;
        let outcome = operation();
        let release = file.unlock();
        let value = outcome?;
        release?;
        Ok(value)
    }

    pub(super) fn move_new(
        &self,
        from: &str,
        dest: &Dir,
        to: &str,
        expected: &ObjectId,
    ) -> Result<()> {
        self.check()?;
        dest.check()?;
        if !self.contains(from)? {
            return Err(PublishError::Changed);
        }
        if dest.contains(to)? {
            return Err(PublishError::Occupied);
        }
        os::rename_new(self, from, dest, to, expected)?;
        let landed = dest.contains(to).and_then(|present| {
            if present {
                Ok(dest.child(to)?.id() == expected)
            } else {
                Ok(false)
            }
        });
        if !matches!(landed, Ok(true)) {
            return Err(PublishError::MovedUnverified {
                path: dest.path().join(to),
            });
        }
        dest.sync()?;
        self.sync()?;
        dest.check()
    }

    pub(super) fn sync(&self) -> Result<()> {
        os::sync_dir(self)
    }

    pub(super) fn remove_empty(&self, name: &str, expected: &ObjectId) -> Result<()> {
        self.check()?;
        os::remove_empty(self, name, expected)?;
        self.sync()
    }
}
