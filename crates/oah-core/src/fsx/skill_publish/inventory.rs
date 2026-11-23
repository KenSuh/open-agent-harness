use super::native::{Dir, FileAccess};
use super::{
    Acquisition, Fingerprint, InstalledDirectory, InstalledFile, PublishError, ReviewBinding, os,
};
use crate::review::identity::portable_paths;
use crate::review::snapshot::{FILE_COUNT_LIMIT, FILE_LIMIT, SNAPSHOT_LIMIT};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;

type Result<T> = std::result::Result<T, PublishError>;
pub(super) type Payload = BTreeMap<String, Vec<u8>>;

impl ReviewBinding {
    fn valid(&self) -> bool {
        let digest = |id: &str| {
            id.strip_prefix("sha256:")
                .and_then(Fingerprint::from_hex)
                .is_some()
        };
        !self.repo.is_empty()
            && !self.path.is_empty()
            && digest(&self.review_id)
            && digest(&self.snapshot_id)
    }

    pub(super) fn same_source(&self, other: &Self) -> bool {
        self.repo == other.repo && self.path == other.path
    }
}

fn valid_names<'a>(paths: impl Iterator<Item = &'a String>) -> bool {
    let paths: Vec<_> = paths.cloned().collect();
    paths.len() <= FILE_COUNT_LIMIT
        && paths.iter().any(|p| p == "SKILL.md")
        && portable_paths(&paths).is_ok()
}

pub(super) fn validate(binding: &ReviewBinding, files: &Payload) -> Result<()> {
    if !binding.valid() || !valid_names(files.keys()) {
        return Err(PublishError::InvalidPayload);
    }
    let mut total = 0;
    for data in files.values() {
        let len = u64::try_from(data.len()).map_err(|_| PublishError::InvalidPayload)?;
        if len > FILE_LIMIT {
            return Err(PublishError::InvalidPayload);
        }
        total += len;
    }
    if total > SNAPSHOT_LIMIT {
        return Err(PublishError::InvalidPayload);
    }
    Ok(())
}

fn split(path: &str) -> (&str, &str) {
    path.rsplit_once('/').unwrap_or(("", path))
}

fn layout<'a>(paths: impl Iterator<Item = &'a String>) -> BTreeMap<&'a str, BTreeSet<&'a str>> {
    let mut folders = BTreeMap::<_, BTreeSet<_>>::new();
    for path in paths {
        let mut entry = path.as_str();
        loop {
            let (parent, name) = split(entry);
            if !folders.entry(parent).or_default().insert(name) || parent.is_empty() {
                break;
            }
            entry = parent;
        }
    }
    folders
}

fn descend(root: &Dir, path: &str) -> Result<Dir> {
    let mut dir = root.clone();
    if !path.is_empty() {
        for part in path.split('/') {
            dir = dir.child(part)?;
        }
    }
    Ok(dir)
}

fn relative(parent: &str, name: &str) -> String {
    if parent.is_empty() {
        name.to_owned()
    } else {
        format!("{parent}/{name}")
    }
}

fn file_receipt(file: &File, bytes: &[u8]) -> Result<InstalledFile> {
    Ok(InstalledFile {
        identity: os::identity(file)?,
        length: u64::try_from(bytes.len()).map_err(|_| PublishError::InvalidPayload)?,
        sha256: Fingerprint::of(bytes).to_hex(),
        permissions: os::permissions(file)?,
    })
}

impl Acquisition {
    pub(super) fn stage(root: &Dir, binding: &ReviewBinding, payload: &Payload) -> Result<Self> {
        let mut acquisition = Self {
            binding: binding.clone(),
            directory: root.id().clone(),
            root_permissions: os::permissions(root.file())?,
            directories: BTreeMap::new(),
            files: BTreeMap::new(),
        };
        for (parent, entries) in layout(payload.keys()) {
            let dir = if parent.is_empty() {
                root.clone()
            } else {
                let (ancestor, name) = split(parent);
                let dir = descend(root, ancestor)?.create_dir(name)?;
                acquisition.directories.insert(
                    parent.to_owned(),
                    InstalledDirectory {
                        identity: dir.id().clone(),
                        permissions: os::permissions(dir.file())?,
                    },
                );
                dir
            };
            for name in entries {
                let path = relative(parent, name);
                if let Some(bytes) = payload.get(&path) {
                    let file = dir.write_new(name, bytes)?;
                    acquisition.files.insert(path, file_receipt(&file, bytes)?);
                }
            }
        }
        acquisition.verify(root)?;
        Ok(acquisition)
    }

    pub(super) fn valid(&self) -> bool {
        self.binding.valid()
            && valid_names(self.files.keys())
            && self.files.values().all(|file| {
                file.length <= FILE_LIMIT && Fingerprint::from_hex(&file.sha256).is_some()
            })
            && self.files.values().map(|file| file.length).sum::<u64>() <= SNAPSHOT_LIMIT
    }

    pub(super) fn matches(&self, binding: &ReviewBinding, files: &Payload) -> bool {
        self.binding == *binding && self.same_bytes(files)
    }

    pub(super) fn same_bytes(&self, files: &Payload) -> bool {
        files.len() == self.files.len()
            && files.iter().all(|(path, data)| {
                self.files
                    .get(path)
                    .is_some_and(|file| file.sha256 == Fingerprint::of(data).to_hex())
            })
    }

    pub(super) fn verify(&self, root: &Dir) -> Result<()> {
        self.visit(root, Visit::Verify)
    }

    pub(super) fn prune(&self, root: &Dir) -> Result<()> {
        self.visit(root, Visit::VerifyRemaining)?;
        self.visit(root, Visit::RemoveRemaining)
    }

    fn visit(&self, root: &Dir, action: Visit) -> Result<()> {
        if self.directory != *root.id() || self.root_permissions != os::permissions(root.file())? {
            return Err(PublishError::Changed);
        }
        let folders = layout(self.files.keys());
        if folders.len() != self.directories.len() + 1 {
            return Err(PublishError::InvalidReceipt);
        }
        for (parent, entries) in folders.into_iter().rev() {
            let dir = match descend(root, parent) {
                Err(PublishError::Io(err))
                    if action != Visit::Verify && err.kind() == std::io::ErrorKind::NotFound =>
                {
                    continue;
                }
                other => other?,
            };
            if !parent.is_empty() {
                let expected = self
                    .directories
                    .get(parent)
                    .ok_or(PublishError::InvalidReceipt)?;
                if &expected.identity != dir.id()
                    || expected.permissions != os::permissions(dir.file())?
                {
                    return Err(PublishError::Changed);
                }
            }
            os::plain_payload(dir.file())?;
            let names = dir.names()?;
            if action == Visit::Verify && names.len() != entries.len() {
                return Err(PublishError::Changed);
            }
            for name in &names {
                let name = name
                    .to_str()
                    .filter(|s| entries.contains(s))
                    .ok_or(PublishError::Changed)?;
                let path = relative(parent, name);
                if let Some(expected) = self.files.get(&path) {
                    let access = if action == Visit::RemoveRemaining {
                        FileAccess::Remove
                    } else {
                        FileAccess::Read
                    };
                    let file = dir.regular(name, access)?;
                    if file.metadata()?.len() != expected.length
                        || os::identity(&file)? != expected.identity
                    {
                        return Err(PublishError::Changed);
                    }
                    let mut bytes = Vec::new();
                    (&file).take(expected.length + 1).read_to_end(&mut bytes)?;
                    if file_receipt(&file, &bytes)? != *expected {
                        return Err(PublishError::Changed);
                    }
                    os::plain_payload(&file)?;
                    if action == Visit::RemoveRemaining {
                        dir.check()?;
                        os::remove_file(&dir, name, &file)?;
                        dir.sync()?;
                    }
                } else if action == Visit::RemoveRemaining {
                    dir.remove_empty(
                        name,
                        &self
                            .directories
                            .get(&path)
                            .ok_or(PublishError::InvalidReceipt)?
                            .identity,
                    )?;
                }
            }
            dir.check()?;
        }
        Ok(())
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Visit {
    Verify,
    VerifyRemaining,
    RemoveRemaining,
}
