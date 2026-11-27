//! Installs a reviewed snapshot in one selected project's native skill directory.
//!
//! The caller owns project selection, review, policy and confirmation. The writer owns the
//! directory handles, the cooperative lock, and its acquisition receipts outside the live tree.
//! It supports local APFS and fixed local NTFS. Updates use two non-replacing moves. Between
//! them the old tree is in the receipt's recovery directory. On failure it returns to a vacant
//! live name when verifiable. A verified rollback can resume or accept a different confirmed review.
//! Each successful update retains its predecessor. A later update verifies and prunes that backup
//! before staging the next revision. Superseded generations remain available for inspection.

#[cfg(any(target_os = "macos", windows))]
use super::Fingerprint;
use crate::review::identity::portable_paths;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};

#[cfg(any(target_os = "macos", windows))]
mod inventory;
mod local_skill;
pub use local_skill::LocalSkill;
#[cfg(any(target_os = "macos", windows))]
mod native;
#[cfg(any(target_os = "macos", windows))]
pub(crate) use native::{BoundFile, BoundFolder};
#[cfg(target_os = "macos")]
#[path = "skill_publish/macos.rs"]
mod os;
#[cfg(windows)]
#[path = "skill_publish/windows.rs"]
mod os;
#[cfg(all(test, any(target_os = "macos", windows)))]
mod tests;
#[cfg(any(target_os = "macos", windows))]
mod transaction;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Engine {
    Claude,
    Codex,
}

/// A single portable folder name. Cannot contain a path or a Windows device name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkillLeaf(String);

impl SkillLeaf {
    pub fn new(name: &str) -> Result<Self, PublishError> {
        let name = name.to_owned();
        if name.contains('/') || portable_paths(std::slice::from_ref(&name)).is_err() {
            return Err(PublishError::UnsafePath);
        }
        Ok(Self(name))
    }
}

/// Immutable review and snapshot IDs and the canonical repository and folder supplied by the reviewer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewBinding {
    pub repo: String,
    pub path: String,
    pub review_id: String,
    pub snapshot_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ObjectId {
    volume: u64,
    index: u64,
    birth_seconds: u64,
    birth_nanos: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct InstalledDirectory {
    identity: ObjectId,
    permissions: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstalledFile {
    identity: ObjectId,
    pub length: u64,
    pub sha256: String,
    permissions: u32,
}

/// Durable acquisition of the directory, every nested directory, and every regular file.
/// Deserializing evidence never grants write authority. The writer loads its own bound slot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Acquisition {
    pub binding: ReviewBinding,
    directory: ObjectId,
    root_permissions: u32,
    directories: BTreeMap<String, InstalledDirectory>,
    pub files: BTreeMap<String, InstalledFile>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublishStatus {
    Installed,
    Updated,
    Unchanged,
}

#[derive(Debug)]
pub enum PublishError {
    Unsupported,
    UnsafePath,
    InvalidPayload,
    InvalidReceipt,
    Occupied,
    Changed,
    StaleRevision,
    SourceMismatch,
    SharedConfirmationRequired,
    Busy,
    Pending,
    Recovery { path: PathBuf, cause: Box<Self> },
    MovedUnverified { path: PathBuf },
    Io(io::Error),
}

impl PublishError {
    pub const fn code(&self) -> &'static str {
        match self {
            Self::Unsupported => "OAH_SKILL_FS_UNSUPPORTED",
            Self::UnsafePath => "OAH_SKILL_PATH_UNSAFE",
            Self::InvalidPayload => "OAH_SKILL_PAYLOAD_INVALID",
            Self::InvalidReceipt => "OAH_SKILL_RECEIPT_INVALID",
            Self::Occupied => "OAH_SKILL_DESTINATION_OCCUPIED",
            Self::Changed => "OAH_SKILL_ACQUISITION_CHANGED",
            Self::StaleRevision => "OAH_SKILL_REVISION_STALE",
            Self::SourceMismatch => "OAH_SKILL_SOURCE_MISMATCH",
            Self::SharedConfirmationRequired => "OAH_SKILL_SHARED_CONFIRMATION_REQUIRED",
            Self::Busy => "OAH_SKILL_WRITER_BUSY",
            Self::Pending => "OAH_SKILL_TRANSACTION_PENDING",
            Self::Recovery { .. } => "OAH_SKILL_RECOVERY_REQUIRED",
            Self::MovedUnverified { .. } => "OAH_SKILL_MOVED_UNVERIFIED",
            Self::Io(_) => "OAH_SKILL_FS_IO",
        }
    }
}

impl fmt::Display for PublishError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())?;
        match self {
            Self::Recovery { path, cause } => {
                write!(f, " (recovery at {}, {cause})", path.display())
            }
            Self::Io(err) => write!(f, " ({err})"),
            Self::MovedUnverified { path } => write!(f, " (inspect {})", path.display()),
            _ => Ok(()),
        }
    }
}

impl std::error::Error for PublishError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(err) => Some(err),
            Self::Recovery { cause, .. } => Some(cause),
            _ => None,
        }
    }
}

impl From<io::Error> for PublishError {
    fn from(err: io::Error) -> Self {
        match err.kind() {
            io::ErrorKind::AlreadyExists => Self::Occupied,
            io::ErrorKind::Unsupported => Self::Unsupported,
            _ => Self::Io(err),
        }
    }
}

/// Keep the selected project and session home identities across confirmation.
#[derive(Clone, Debug)]
pub struct ProjectRoot {
    #[cfg(any(target_os = "macos", windows))]
    project: native::Selection,
    #[cfg(any(target_os = "macos", windows))]
    home: native::Selection,
}

#[cfg(any(target_os = "macos", windows))]
impl ProjectRoot {
    /// Qualifies the absolute project and session home paths without writing.
    pub fn open(path: &Path, session_home: &Path) -> Result<Self, PublishError> {
        let (home, home_dir) = native::Selection::open(session_home)?;
        let (project, project_dir) = native::Selection::open(path)?;
        project_dir.outside_home_metadata(&home_dir)?;
        Ok(Self { project, home })
    }

    fn with_project<T>(
        &self,
        operation: impl FnOnce(&native::Dir) -> Result<T, PublishError>,
    ) -> Result<T, PublishError> {
        let home = self.home.pin()?;
        let project = self.project.pin()?;
        project.outside_home_metadata(&home)?;
        operation(&project)
    }

    /// Returns a managed acquisition only after verifying the receipt, live object and complete inventory.
    /// Missing management state is `Ok(None)`. Changes and unsafe paths are errors.
    pub fn installed(
        &self,
        engine: Engine,
        leaf: &SkillLeaf,
    ) -> Result<Option<Acquisition>, PublishError> {
        self.with_project(|dir| transaction::installed(dir, engine, leaf))
    }

    /// With no expected revision, installs into a vacant location or verifies an exact duplicate.
    /// An update requires the previously installed review ID and the same source. Identical files keep
    /// the existing acquisition and review ID. A retry after publication may supply the original expected revision.
    pub fn apply(
        &self,
        engine: Engine,
        leaf: &SkillLeaf,
        binding: &ReviewBinding,
        files: &BTreeMap<String, Vec<u8>>,
        expected_review_id: Option<&str>,
    ) -> Result<PublishStatus, PublishError> {
        self.with_project(|dir| {
            transaction::apply(dir, engine, leaf, binding, files, expected_review_id)
        })
    }

    pub fn adopt_existing(
        &self,
        engine: Engine,
        leaf: &SkillLeaf,
        local: &LocalSkill,
        binding: &ReviewBinding,
        files: &BTreeMap<String, Vec<u8>>,
    ) -> Result<(), PublishError> {
        self.with_project(|dir| transaction::adopt(dir, engine, leaf, local, binding, files))
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
impl ProjectRoot {
    pub fn open(_: &Path, _: &Path) -> Result<Self, PublishError> {
        Err(PublishError::Unsupported)
    }
    pub fn installed(&self, _: Engine, _: &SkillLeaf) -> Result<Option<Acquisition>, PublishError> {
        Err(PublishError::Unsupported)
    }
    pub fn apply(
        &self,
        _: Engine,
        _: &SkillLeaf,
        _: &ReviewBinding,
        _: &BTreeMap<String, Vec<u8>>,
        _: Option<&str>,
    ) -> Result<PublishStatus, PublishError> {
        Err(PublishError::Unsupported)
    }

    pub fn adopt_existing(
        &self,
        _: Engine,
        _: &SkillLeaf,
        _: &LocalSkill,
        _: &ReviewBinding,
        _: &BTreeMap<String, Vec<u8>>,
    ) -> Result<(), PublishError> {
        Err(PublishError::Unsupported)
    }
}
