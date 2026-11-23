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

#[derive(Clone, Debug)]
pub(super) struct Selection {
    #[cfg(target_os = "macos")]
    dir: Dir,
    #[cfg(windows)]
    path: PathBuf,
    #[cfg(windows)]
    identities: Vec<ObjectId>,
}

impl Selection {
    pub(super) fn open(path: &Path) -> Result<(Self, Dir)> {
        let dir = Dir::open(path)?;
        let selection = Self {
            #[cfg(target_os = "macos")]
            dir: dir.clone(),
            #[cfg(windows)]
            path: path.to_owned(),
            #[cfg(windows)]
            identities: dir.ancestors().map(|part| part.id().clone()).collect(),
        };
        Ok((selection, dir))
    }

    #[cfg(windows)]
    pub(super) fn pin(&self) -> Result<Dir> {
        let dir = Dir::open(&self.path)?;
        if dir.ancestors().map(Dir::id).ne(&self.identities) {
            return Err(PublishError::UnsafePath);
        }
        Ok(dir)
    }

    #[cfg(target_os = "macos")]
    pub(super) fn pin(&self) -> Result<Dir> {
        self.dir.check()?;
        Ok(self.dir.clone())
    }
}

impl Dir {
    fn open(path: &Path) -> Result<Self> {
        let dir = Self::walk(path)?;
        #[cfg(windows)]
        let dir = {
            let canonical = path.canonicalize()?;
            if canonical == dir.path() {
                dir
            } else {
                let physical = Self::walk(&canonical)?;
                if physical
                    .ancestors()
                    .map(Dir::id)
                    .ne(dir.ancestors().map(Dir::id))
                {
                    return Err(PublishError::UnsafePath);
                }
                physical
            }
        };
        os::qualify(dir.file())?;
        dir.check()?;
        Ok(dir)
    }

    fn walk(path: &Path) -> Result<Self> {
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
        Ok(dir)
    }

    pub(super) fn outside_home_metadata(&self, home: &Dir) -> Result<()> {
        if self.in_metadata_tree_of(home) || home.in_metadata_tree_of(self) {
            return Err(PublishError::UnsafePath);
        }
        Ok(())
    }

    fn in_metadata_tree_of(&self, root: &Dir) -> bool {
        let mut child: Option<&Dir> = None;
        for dir in self.ancestors() {
            if dir.id() == root.id() {
                return child.is_none_or(|part| {
                    matches!(
                        part.path()
                            .file_name()
                            .and_then(OsStr::to_str)
                            .map(portable_component_key)
                            .as_deref(),
                        Some(".claude" | ".agents" | ".oah")
                    )
                });
            }
            child = Some(dir);
        }
        false
    }

    fn ancestors(&self) -> impl Iterator<Item = &Dir> {
        std::iter::successors(Some(self), |dir| dir.0.parent.as_ref())
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
        for dir in self.ancestors() {
            ids.push(dir.id().clone());
            if Arc::ptr_eq(&root.0, &dir.0) {
                return Ok(ids);
            }
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
