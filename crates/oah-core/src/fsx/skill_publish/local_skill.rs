use super::PublishError;
use std::path::{Path, PathBuf};

#[cfg(any(target_os = "macos", windows))]
use super::{Fingerprint, ObjectId, SkillLeaf, native};

pub struct LocalSkill {
    path: PathBuf,
    #[cfg(any(target_os = "macos", windows))]
    parent: native::Dir,
    #[cfg(any(target_os = "macos", windows))]
    leaf: SkillLeaf,
    #[cfg(any(target_os = "macos", windows))]
    pub(super) identity: ObjectId,
}

#[cfg(any(target_os = "macos", windows))]
impl LocalSkill {
    pub fn open(path: &Path) -> Result<Self, PublishError> {
        let name = path
            .file_name()
            .and_then(std::ffi::OsStr::to_str)
            .filter(|name| !name.starts_with('.'))
            .ok_or(PublishError::UnsafePath)?;
        let leaf = SkillLeaf::new(name)?;
        let (_, parent) = native::Selection::open(path.parent().ok_or(PublishError::UnsafePath)?)?;
        let folder = parent.child(&leaf.0)?;
        folder.regular("SKILL.md", native::FileAccess::Read)?;
        folder.check()?;
        let identity = folder.id().clone();
        let path = parent.path().join(&leaf.0);
        Ok(Self {
            path,
            parent,
            leaf,
            identity,
        })
    }

    pub fn stamp(&self) -> Result<String, PublishError> {
        self.parent.check()?;
        if *self.parent.child(&self.leaf.0)?.id() != self.identity {
            return Err(PublishError::Changed);
        }
        let data = serde_json::to_vec(&(&self.path, self.parent.id(), &self.identity))
            .map_err(|_| PublishError::UnsafePath)?;
        Ok(Fingerprint::of(&data).to_hex())
    }

    pub fn archive(self) -> Result<PathBuf, PublishError> {
        use std::collections::hash_map::RandomState;
        use std::hash::BuildHasher;

        self.parent.check()?;
        let archive = self
            .parent
            .directory(".oah-removed-skills", true)?
            .ok_or(PublishError::Changed)?;
        archive.with_lock(|| {
            super::transaction::ignore_private_state(&archive)?;
            let id = format!("{:016x}", RandomState::new().hash_one(&self.identity.index));
            let destination = archive.create_dir(&id)?;
            let path = destination.path().join(&self.leaf.0);
            if let Err(error) =
                self.parent
                    .move_new(&self.leaf.0, &destination, &self.leaf.0, &self.identity)
            {
                if !matches!(destination.contains(&self.leaf.0), Ok(false)) {
                    return Err(PublishError::MovedUnverified { path });
                }
                let id_on_disk = destination.id().clone();
                drop(destination);
                let _ = archive.remove_empty(&id, &id_on_disk);
                return Err(error);
            }
            Ok(path)
        })
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
impl LocalSkill {
    pub fn open(_: &Path) -> Result<Self, PublishError> {
        Err(PublishError::Unsupported)
    }

    pub fn stamp(&self) -> Result<String, PublishError> {
        Err(PublishError::Unsupported)
    }

    pub fn archive(self) -> Result<PathBuf, PublishError> {
        Err(PublishError::Unsupported)
    }
}

impl LocalSkill {
    pub fn path(&self) -> &Path {
        &self.path
    }
}
