use super::{ObjectId, PublishError, os};
use crate::review::identity::portable_component_key;
use std::ffi::{OsStr, OsString};
use std::fs::{File, TryLockError};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

type Result<T> = std::result::Result<T, PublishError>;

#[derive(Debug, Clone, Copy)]
pub(super) enum FileAccess {
    Read,
    /// Reads a file other programs keep using: nobody is kept from writing, renaming or removing it meanwhile.
    ReadShared,
    Create,
    Lock,
    Remove,
}

/// One try at the writer lock of a file, in the protocol of `fsx::FileLock`.
pub(super) enum LockAttempt {
    Held(File),
    Busy,
    /// The lock file was replaced between opening and locking; try again at once.
    #[cfg_attr(windows, expect(dead_code))]
    Retry,
}

/// What a name in a folder is, as the listing reports it. A later open still checks the object itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Kind {
    Dir,
    File,
    /// A symbolic link, a junction or another reparse point, a device, a socket or a pipe.
    Other,
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

    /// Up to `limit` names with their kinds, and whether the folder holds more.
    pub(super) fn entries(&self, limit: usize) -> Result<(Vec<(OsString, Kind)>, bool)> {
        os::entries(self, limit)
    }

    /// The identities of this folder and of every folder above it, as one digest.
    pub(super) fn stamp(&self) -> Result<String> {
        let ids: Vec<&ObjectId> = self.ancestors().map(Dir::id).collect();
        let data = serde_json::to_vec(&ids).map_err(|_| PublishError::UnsafePath)?;
        Ok(crate::fsx::Fingerprint::of(&data).to_hex())
    }

    /// A new folder with the permissions an ordinary project folder gets. Never replaces a name.
    pub(super) fn create_shared_dir(&self, name: &str) -> Result<Self> {
        self.check()?;
        os::mkdir_shared(self, name)?;
        self.sync()?;
        self.child(name)
    }

    /// Writes a complete new file under `name` and never replaces one: the bytes go to a staged sibling that is
    /// flushed and then given the name without replacement, the way [`BoundFolder::publish_new`] does it.
    /// `Occupied` when the name is taken.
    pub(super) fn publish_new(&self, name: &str, data: &[u8]) -> Result<()> {
        use std::collections::hash_map::RandomState;
        use std::hash::BuildHasher;

        self.check()?;
        // Not built from `name`: a copied file's own name can be as long as a name may be.
        let staged = format!(
            ".oah-{:016x}.tmp",
            RandomState::new().hash_one(std::process::id())
        );
        let mut file = os::stage(self, &staged)?;
        let landed = file
            .write_all(data)
            .and_then(|()| file.sync_all())
            .map_err(PublishError::from)
            .and_then(|()| install_shared(self, &staged, &file, name));
        if let Err(err) = landed {
            let _ = os::remove_file(self, &staged, &file);
            return Err(err);
        }
        drop(file);
        self.sync()?;
        self.check()
    }
}

/// How long an open retries while another process has the file open in a way that excludes this one.
const SHARING_WAIT: Duration = Duration::from_secs(2);
const SHARING_POLL: Duration = Duration::from_millis(50);

fn leaf(name: &str) -> Result<()> {
    if name.is_empty() || matches!(name, "." | "..") || name.contains(['/', '\\', '\0']) {
        return Err(PublishError::UnsafePath);
    }
    Ok(())
}

fn retry_busy<T>(mut operation: impl FnMut() -> Result<T>) -> Result<T> {
    let deadline = Instant::now() + SHARING_WAIT;
    loop {
        match operation() {
            Err(PublishError::Busy) if Instant::now() < deadline => {
                std::thread::sleep(SHARING_POLL);
            }
            other => return other,
        }
    }
}

/// [`os::install_file`] for a file other programs may notice at once. On Windows a scanner or an indexer can hold
/// a new file open for a moment without sharing its name, so a refused rename is tried again for [`SHARING_WAIT`].
fn install_shared(dir: &Dir, staged: &str, file: &File, name: &str) -> Result<()> {
    let deadline = Instant::now() + SHARING_WAIT;
    loop {
        match os::install_file(dir, staged, file, name) {
            Err(PublishError::Io(err))
                if cfg!(windows)
                    && matches!(err.raw_os_error(), Some(5 | 32))
                    && Instant::now() < deadline =>
            {
                std::thread::sleep(SHARING_POLL);
            }
            outcome => return outcome,
        }
    }
}

fn read_all(file: &mut File) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    file.seek(SeekFrom::Start(0))?;
    file.read_to_end(&mut bytes)?;
    Ok(bytes)
}

/// A regular file read through a handle of a [`BoundFolder`], with the bytes that handle gave.
#[derive(Debug)]
pub struct BoundFile {
    file: File,
    bytes: Vec<u8>,
}

impl BoundFile {
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }
}

/// The writer lock of one file in a [`BoundFolder`]: the lock file and protocol of `fsx::FileLock`, taken through
/// the pinned folder instead of a path.
#[derive(Debug)]
pub struct BoundLock {
    dir: Dir,
    name: String,
    file: Option<File>,
}

impl BoundLock {
    /// Releases the lock and removes the lock file.
    pub fn release(mut self) -> Result<()> {
        match self.file.take() {
            Some(file) => os::unlock(&self.dir, &self.name, file),
            None => Ok(()),
        }
    }
}

impl Drop for BoundLock {
    fn drop(&mut self) {
        if let Some(file) = self.file.take() {
            let _ = os::unlock(&self.dir, &self.name, file);
        }
    }
}

/// One folder directly inside a selected root, with every folder from the volume root down held open.
///
/// Reads, the lock and both ways of publishing a file go through those handles: no step resolves the folder's
/// path again, so swapping the root, an ancestor or the folder for a link after it was opened cannot move a
/// write somewhere else. The root must be an absolute path without links on a volume [`ProjectRoot`]
/// accepts (`Unsupported` otherwise).
///
/// [`ProjectRoot`]: super::ProjectRoot
#[derive(Debug)]
pub struct BoundFolder {
    root: Dir,
    name: String,
    dir: Option<Dir>,
    created: Option<ObjectId>,
}

impl BoundFolder {
    /// Pins `root` and, when it is there, its folder `name`. Writes nothing.
    pub fn open(root: &Path, name: &str) -> Result<Self> {
        leaf(name)?;
        let root = Dir::open(root)?;
        let dir = root.directory(name, false)?;
        Ok(Self {
            root,
            name: name.to_owned(),
            dir,
            created: None,
        })
    }

    /// The identity of the pinned root folder on its volume: the same text for the same folder as long as it
    /// exists, and another text for any other folder, also one later put at the same path.
    pub fn root_identity(&self) -> String {
        let id = self.root.id();
        format!(
            "{:x}-{:x}-{}-{}",
            id.volume, id.index, id.birth_seconds, id.birth_nanos
        )
    }

    /// Checks that every pinned name still names the folder that was opened.
    pub fn check(&self) -> Result<()> {
        self.dir.as_ref().unwrap_or(&self.root).check()
    }

    /// Makes the folder when it is absent. `true` when this call made it.
    pub fn create(&mut self) -> Result<bool> {
        if self.dir.is_some() {
            return Ok(false);
        }
        self.root.check()?;
        let made = if self.root.contains(&self.name)? {
            false
        } else {
            match os::mkdir_shared(&self.root, &self.name) {
                Ok(()) => true,
                Err(PublishError::Occupied) => false,
                Err(err) => return Err(err),
            }
        };
        self.root.sync()?;
        let dir = self.root.child(&self.name)?;
        if made {
            self.created = Some(dir.id().clone());
        }
        self.dir = Some(dir);
        Ok(made)
    }

    /// Removes the folder again when this value made it and it is still empty. Release its lock first.
    pub fn discard(&mut self) -> Result<()> {
        let Some(id) = self.created.take() else {
            return Ok(());
        };
        self.dir = None;
        self.root.remove_empty(&self.name, &id)
    }

    /// Reads the regular file `name`; `None` when the folder or the file is absent.
    pub fn read(&self, name: &str) -> Result<Option<BoundFile>> {
        self.open_file(name, false)
    }

    /// [`BoundFolder::read`] for a file that [`BoundFolder::replace`] will replace. On Windows no other process can
    /// write the file in place or replace it with a classic rename while the returned value lives.
    pub fn hold(&self, name: &str) -> Result<Option<BoundFile>> {
        self.open_file(name, true)
    }

    fn open_file(&self, name: &str, hold: bool) -> Result<Option<BoundFile>> {
        leaf(name)?;
        let Some(dir) = &self.dir else {
            self.root.check()?;
            return Ok(None);
        };
        dir.check()?;
        if !dir.contains(name)? {
            return Ok(None);
        }
        let mut file = match retry_busy(|| os::open_file(dir, name, hold)) {
            Ok(file) => file,
            Err(PublishError::Io(err)) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(err) => return Err(err),
        };
        if os::identity(&file)?.volume != dir.id().volume {
            return Err(PublishError::Unsupported);
        }
        let bytes = read_all(&mut file)?;
        Ok(Some(BoundFile { file, bytes }))
    }

    /// Takes the writer lock `name` (for `config.toml` that is `config.toml.lock`), waiting at most `wait`.
    /// `Busy` when another writer held it for the whole wait.
    pub fn lock(&self, name: &str, wait: Duration, poll: Duration) -> Result<BoundLock> {
        leaf(name)?;
        let dir = self.dir.as_ref().ok_or(PublishError::Changed)?;
        let deadline = Instant::now() + wait;
        loop {
            dir.check()?;
            match os::try_lock(dir, name)? {
                LockAttempt::Held(file) => {
                    return Ok(BoundLock {
                        dir: dir.clone(),
                        name: name.to_owned(),
                        file: Some(file),
                    });
                }
                LockAttempt::Retry if Instant::now() < deadline => {}
                LockAttempt::Busy if Instant::now() < deadline => std::thread::sleep(poll),
                LockAttempt::Retry | LockAttempt::Busy => return Err(PublishError::Busy),
            }
        }
    }

    /// Writes `data` to a new sibling, flushes it and gives it the vacant name `name` without replacing
    /// anything: `Occupied` when the name is taken, and then nothing is left behind.
    pub fn publish_new(&self, name: &str, data: &[u8]) -> Result<()> {
        leaf(name)?;
        let dir = self.dir.as_ref().ok_or(PublishError::Changed)?;
        dir.check()?;
        if dir.contains(name)? {
            return Err(PublishError::Occupied);
        }
        let (staged, file) = stage(dir, name, data)?;
        if let Err(err) = os::install_file(dir, &staged, &file, name) {
            let _ = os::remove_file(dir, &staged, &file);
            return Err(err);
        }
        let id = os::identity(&file)?;
        drop(file);
        dir.sync()?;
        verify(dir, name, &id, data)
    }

    /// Replaces `name` with `data` in one atomic step, only while it is still the file `before` was read from with
    /// the same bytes. A reader finds the old file or the new one, never a missing name, also when the process
    /// stops half-way; at worst a staged sibling `.<name>.<pid>.<n>.tmp` is left.
    ///
    /// `Changed` when another program replaced or edited the file since; the name then holds what that program
    /// left. `MovedUnverified` names an entry to inspect when the step could not be checked afterwards.
    ///
    /// Limit (Windows): the name is compared with `before` immediately before the rename, not in the same step,
    /// so another program's own atomic rename or delete of the name in that instant is replaced unseen. Classic
    /// renames and writes in place are refused by Windows while `before` is open.
    pub fn replace(&self, name: &str, before: BoundFile, data: &[u8]) -> Result<()> {
        leaf(name)?;
        let dir = self.dir.as_ref().ok_or(PublishError::Changed)?;
        dir.check()?;
        let BoundFile {
            file: mut old,
            bytes: expected,
        } = before;
        let (staged, new) = stage(dir, name, data)?;
        let aside = match os::exchange_file(dir, &staged, &new, name, &old) {
            Ok(aside) => aside,
            Err(err) => {
                let _ = os::remove_file(dir, &staged, &new);
                return Err(err);
            }
        };
        if aside {
            // The replaced file now sits under the staged name. Without a deny-write open (macOS) an edit in
            // place could have landed just before the exchange, so its bytes are compared once more.
            if !read_all(&mut old).is_ok_and(|bytes| bytes == expected) {
                os::revert_file(dir, &staged, &new, name, &old)?;
                let _ = os::remove_file(dir, &staged, &new);
                return Err(PublishError::Changed);
            }
            if os::remove_file(dir, &staged, &old).is_err() {
                return Err(PublishError::MovedUnverified {
                    path: dir.path().join(staged),
                });
            }
        }
        // The identity is read from the open handle after the exchange, never before it: NTFS gives a file
        // renamed onto a name the creation time of the file that had the name (tunnelling), so an identity read
        // before the rename no longer names the same file after it.
        let id = os::identity(&new).map_err(|_| PublishError::MovedUnverified {
            path: dir.path().join(name),
        })?;
        // On Windows a reader cannot open the name while this handle, which may delete, is open.
        drop((new, old));
        dir.sync()?;
        verify(dir, name, &id, data)
    }
}

/// Creates a uniquely named sibling of `name` holding `data`, flushed to disk.
fn stage(dir: &Dir, name: &str, data: &[u8]) -> Result<(String, File)> {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    for _ in 0..16 {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.subsec_nanos());
        let staged = format!(
            ".{name}.{}.{nanos:08x}{:04x}.tmp",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed) & 0xffff
        );
        let mut file = match os::stage(dir, &staged) {
            Ok(file) => file,
            Err(PublishError::Occupied) => continue,
            Err(err) => return Err(err),
        };
        let written = file
            .write_all(data)
            .and_then(|()| file.sync_all())
            .map_err(PublishError::from)
            .and_then(|()| match os::identity(&file)?.volume == dir.id().volume {
                true => Ok(()),
                false => Err(PublishError::Unsupported),
            });
        if let Err(err) = written {
            let _ = os::remove_file(dir, &staged, &file);
            return Err(err);
        }
        return Ok((staged, file));
    }
    Err(PublishError::Occupied)
}

/// Reads `name` back through the folder: it must be the file with identity `id` and hold `data`.
fn verify(dir: &Dir, name: &str, id: &ObjectId, data: &[u8]) -> Result<()> {
    let read = || -> Result<bool> {
        let mut file = retry_busy(|| os::open_file(dir, name, false))?;
        Ok(os::identity(&file)? == *id && read_all(&mut file)? == data)
    };
    if !matches!(read(), Ok(true)) || dir.check().is_err() {
        return Err(PublishError::MovedUnverified {
            path: dir.path().join(name),
        });
    }
    Ok(())
}

#[cfg(test)]
mod bound_tests {
    use super::*;
    use std::fs;

    const NAME: &str = "config.toml";

    struct Project {
        _dir: tempfile::TempDir,
        root: PathBuf,
    }

    fn project(config: Option<&[u8]>) -> Project {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        #[cfg(windows)]
        let root = PathBuf::from(
            root.to_string_lossy()
                .trim_start_matches(r"\\?\")
                .to_owned(),
        );
        if let Some(bytes) = config {
            fs::create_dir(root.join(".oah")).unwrap();
            fs::write(root.join(".oah").join(NAME), bytes).unwrap();
        }
        Project { _dir: dir, root }
    }

    fn entries(folder: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(folder)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    #[test]
    fn a_held_file_is_replaced_in_one_step_and_read_back() {
        let project = project(Some(b"old"));
        let oah = project.root.join(".oah");
        let folder = BoundFolder::open(&project.root, ".oah").unwrap();
        let before = folder.hold(NAME).unwrap().unwrap();
        assert_eq!(before.bytes(), b"old");
        folder.replace(NAME, before, b"new bytes").unwrap();
        assert_eq!(fs::read(oah.join(NAME)).unwrap(), b"new bytes");
        assert_eq!(entries(&oah), [NAME], "a staged or replaced file was left");
        assert_eq!(folder.read(NAME).unwrap().unwrap().bytes(), b"new bytes");
        assert!(folder.read("absent.toml").unwrap().is_none());
    }

    #[test]
    fn a_name_replaced_or_freed_a_moment_ago_is_replaced_and_published_again() {
        // NTFS remembers a name's creation time for some seconds and gives it to the next file under that name,
        // so the identity of a staged file changes at its rename. Every step here reuses the name at once.
        let project = project(Some(b"one"));
        let oah = project.root.join(".oah");
        let folder = BoundFolder::open(&project.root, ".oah").unwrap();
        for bytes in [b"two".as_slice(), b"three"] {
            let before = folder.hold(NAME).unwrap().unwrap();
            folder.replace(NAME, before, bytes).unwrap();
            assert_eq!(fs::read(oah.join(NAME)).unwrap(), bytes);
        }
        fs::remove_file(oah.join(NAME)).unwrap();
        folder.publish_new(NAME, b"four").unwrap();
        assert_eq!(fs::read(oah.join(NAME)).unwrap(), b"four");
        assert_eq!(entries(&oah), [NAME]);
    }

    #[test]
    fn a_file_another_program_replaced_since_the_read_is_left_as_that_program_wrote_it() {
        let project = project(Some(b"old"));
        let oah = project.root.join(".oah");
        let folder = BoundFolder::open(&project.root, ".oah").unwrap();
        let before = folder.hold(NAME).unwrap().unwrap();
        // An editor saves the way editors do: a new file renamed over the name.
        fs::write(oah.join("editor.tmp"), b"external edit").unwrap();
        fs::rename(oah.join("editor.tmp"), oah.join(NAME)).unwrap();
        let error = folder.replace(NAME, before, b"ours").unwrap_err();
        assert!(matches!(error, PublishError::Changed), "{error}");
        assert_eq!(fs::read(oah.join(NAME)).unwrap(), b"external edit");
        assert_eq!(entries(&oah), [NAME]);
        // The file removed since the read is refused the same way and nothing is created.
        let before = folder.hold(NAME).unwrap().unwrap();
        fs::remove_file(oah.join(NAME)).unwrap();
        let error = folder.replace(NAME, before, b"ours").unwrap_err();
        assert!(matches!(error, PublishError::Changed), "{error}");
        assert!(entries(&oah).is_empty());
    }

    // Windows refuses a write in place while the file is held, so only macOS can reach this path.
    #[cfg(target_os = "macos")]
    #[test]
    fn an_edit_in_place_since_the_read_is_found_after_the_swap_and_put_back() {
        let project = project(Some(b"old"));
        let oah = project.root.join(".oah");
        let folder = BoundFolder::open(&project.root, ".oah").unwrap();
        let before = folder.hold(NAME).unwrap().unwrap();
        fs::write(oah.join(NAME), b"edited in place").unwrap();
        let error = folder.replace(NAME, before, b"ours").unwrap_err();
        assert!(matches!(error, PublishError::Changed), "{error}");
        assert_eq!(fs::read(oah.join(NAME)).unwrap(), b"edited in place");
        assert_eq!(entries(&oah), [NAME]);
    }

    #[test]
    fn a_new_file_never_replaces_an_entry_and_the_folder_it_made_can_be_discarded() {
        let project = project(None);
        let oah = project.root.join(".oah");
        let mut folder = BoundFolder::open(&project.root, ".oah").unwrap();
        assert!(folder.read(NAME).unwrap().is_none());
        assert!(matches!(
            folder.publish_new(NAME, b"x"),
            Err(PublishError::Changed)
        ));
        assert!(!oah.exists(), "reading or a refused write made the folder");
        assert!(folder.create().unwrap());
        assert!(!folder.create().unwrap());
        folder.publish_new(NAME, b"first").unwrap();
        let error = folder.publish_new(NAME, b"second").unwrap_err();
        assert!(matches!(error, PublishError::Occupied), "{error}");
        assert_eq!(fs::read(oah.join(NAME)).unwrap(), b"first");
        assert_eq!(entries(&oah), [NAME]);
        // A folder that holds a file is not removed; an empty one this value made is.
        assert!(folder.discard().is_err());
        assert_eq!(fs::read(oah.join(NAME)).unwrap(), b"first");

        let empty = self::project(None);
        let mut folder = BoundFolder::open(&empty.root, ".oah").unwrap();
        assert!(folder.create().unwrap());
        folder.discard().unwrap();
        assert!(!empty.root.join(".oah").exists());
        // A folder that was already there is never removed.
        let kept = self::project(Some(b"keep"));
        let mut folder = BoundFolder::open(&kept.root, ".oah").unwrap();
        assert!(!folder.create().unwrap());
        folder.discard().unwrap();
        assert_eq!(
            fs::read(kept.root.join(".oah").join(NAME)).unwrap(),
            b"keep"
        );
    }

    #[test]
    fn the_lock_is_the_lock_of_the_path_based_writers() {
        use crate::fsx::{FileLock, LockOptions};
        let project = project(Some(b"old"));
        let oah = project.root.join(".oah");
        let folder = BoundFolder::open(&project.root, ".oah").unwrap();
        let (wait, poll) = (Duration::from_millis(150), Duration::from_millis(10));
        let quick = LockOptions { wait, poll };
        let held = folder.lock("config.toml.lock", wait, poll).unwrap();
        assert!(oah.join("config.toml.lock").exists());
        assert_eq!(
            FileLock::acquire(&oah.join(NAME), quick)
                .unwrap_err()
                .code(),
            "OAH_FILE_BUSY"
        );
        let second = folder.lock("config.toml.lock", wait, poll).unwrap_err();
        assert!(matches!(second, PublishError::Busy), "{second}");
        held.release().unwrap();
        assert_eq!(entries(&oah), [NAME], "the lock file was left");
        let path_lock = FileLock::acquire(&oah.join(NAME), quick).unwrap();
        let busy = folder.lock("config.toml.lock", wait, poll).unwrap_err();
        assert!(matches!(busy, PublishError::Busy), "{busy}");
        path_lock.release().unwrap();
        drop(folder.lock("config.toml.lock", wait, poll).unwrap());
        assert_eq!(entries(&oah), [NAME]);
    }

    // On Windows the open handles refuse the renames these swaps start with, so they cannot be staged there.
    #[cfg(target_os = "macos")]
    #[test]
    fn a_folder_swapped_for_a_link_after_it_was_opened_stops_every_step() {
        use std::os::unix::fs::symlink;
        let outside = tempfile::tempdir().unwrap();
        fs::write(outside.path().join(NAME), b"outside").unwrap();
        let stopped = |folder: &BoundFolder, before: BoundFile| {
            let (wait, poll) = (Duration::from_millis(50), Duration::from_millis(10));
            assert!(folder.read(NAME).is_err());
            assert!(folder.hold(NAME).is_err());
            assert!(folder.lock("config.toml.lock", wait, poll).is_err());
            assert!(
                folder
                    .publish_new("config.toml.v0.4.bak", b"backup")
                    .is_err()
            );
            assert!(folder.replace(NAME, before, b"ours").is_err());
            assert!(folder.check().is_err());
            assert_eq!(fs::read(outside.path().join(NAME)).unwrap(), b"outside");
            assert_eq!(
                entries(outside.path()),
                [NAME],
                "something was written outside"
            );
        };

        // 1. .oah moves away and a link to another folder takes its name.
        let first = project(Some(b"old"));
        let folder = BoundFolder::open(&first.root, ".oah").unwrap();
        let before = folder.hold(NAME).unwrap().unwrap();
        fs::rename(first.root.join(".oah"), first.root.join("moved")).unwrap();
        symlink(outside.path(), first.root.join(".oah")).unwrap();
        stopped(&folder, before);
        assert_eq!(
            fs::read(first.root.join("moved").join(NAME)).unwrap(),
            b"old"
        );
        assert_eq!(entries(&first.root.join("moved")), [NAME]);

        // 2. The project root itself moves away and a link takes its name.
        let parent = tempfile::tempdir().unwrap();
        let parent = parent.path().canonicalize().unwrap();
        let root = parent.join("project");
        fs::create_dir_all(root.join(".oah")).unwrap();
        fs::write(root.join(".oah").join(NAME), b"old").unwrap();
        let mut folder = BoundFolder::open(&root, ".oah").unwrap();
        let before = folder.hold(NAME).unwrap().unwrap();
        fs::rename(&root, parent.join("moved")).unwrap();
        symlink(outside.path(), &root).unwrap();
        stopped(&folder, before);
        assert!(folder.create().is_ok_and(|made| !made));
        assert_eq!(
            fs::read(parent.join("moved/.oah").join(NAME)).unwrap(),
            b"old"
        );

        // 3. A root whose path already has a link in it is not opened at all.
        let alias = parent.join("alias");
        symlink(parent.join("moved"), &alias).unwrap();
        assert!(matches!(
            BoundFolder::open(&alias, ".oah"),
            Err(PublishError::UnsafePath)
        ));
        // 4. Names that are not one plain entry are refused.
        let plain = BoundFolder::open(&parent.join("moved"), ".oah").unwrap();
        for name in ["", ".", "..", "a/b", "a\\b"] {
            assert!(
                matches!(plain.read(name), Err(PublishError::UnsafePath)),
                "{name:?}"
            );
            assert!(matches!(
                BoundFolder::open(&parent, name),
                Err(PublishError::UnsafePath)
            ));
        }
    }
}
