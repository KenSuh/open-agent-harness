// 형식 보존 저장: 짧은 잠금 아래 읽은 원본을 다시 확인하고, v0.4.0 파일은 경고 승인과 백업 뒤에만 1.0으로 올린다.
use super::error::{ConfigError, codes};
use super::file::ConfigFile;
use super::patch::{self, Op};
use super::project::{
    AgentConfig, AgentId, AgentSource, Effective, Format, ProjectConfig, interpret,
};
use super::tools::{self, ProjectTools, TOOL_KEYS, ToolsChange, ToolsKept};
use super::value::Value;
#[cfg(any(target_os = "macos", windows))]
use crate::fsx::skill_publish::{BoundFile, BoundFolder, PublishError};
use crate::fsx::{self, FileLock, Fingerprint, FsxErrorKind, LockOptions, WriteReceipt};
use crate::paths;
use crate::rules::input::{ExpectedEntry, InspectedEntry, inspect_project_entry};
use std::ffi::OsString;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

/// The top-level tables of `.oah/config.toml` that OAH writes.
pub const PROJECT_TABLES: [&str; 5] = ["stages", "sections", "models", "agents", "skills"];

const BOM: &[u8] = b"\xEF\xBB\xBF";

/// One change a caller asks for.
#[derive(Debug, Clone, PartialEq)]
pub enum Edit {
    /// Sets the value at a key path, such as `["stages", "plan"]`.
    Set { path: Vec<String>, value: Value },
    /// Removes the key at a path, if present.
    Remove { path: Vec<String> },
}

impl Edit {
    pub fn set<S: AsRef<str>>(path: &[S], value: impl Into<Value>) -> Edit {
        Edit::Set {
            path: path.iter().map(|part| part.as_ref().to_string()).collect(),
            value: value.into(),
        }
    }

    pub fn remove<S: AsRef<str>>(path: &[S]) -> Edit {
        Edit::Remove {
            path: path.iter().map(|part| part.as_ref().to_string()).collect(),
        }
    }

    /// Assigns `agent` to `section`.
    pub fn stage(section: &str, agent: AgentId) -> Edit {
        Edit::set(&["stages", section], agent.as_str())
    }

    fn path(&self) -> &[String] {
        match self {
            Edit::Set { path, .. } | Edit::Remove { path } => path,
        }
    }

    pub(crate) fn op(&self) -> Op {
        match self {
            Edit::Set { path, value } => Op::Set {
                path: path.clone(),
                value: value.clone(),
            },
            Edit::Remove { path } => Op::Remove { path: path.clone() },
        }
    }
}

/// What the user must be told, once, before a v0.4.0 file is first saved by 1.0.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpgradeWarning {
    source: Fingerprint,
}

/// The text of the upgrade warning (§3.1).
pub const UPGRADE_WARNING: &str = "Saving converts .oah/config.toml to the OAH 1.0 format, and oah 0.4 commands \
will stop reading this project's settings. A later `oah rules sync` or `oah init` from the 0.4 CLI replaces the \
1.0 workflow block in AGENTS.md with the 0.4 rules text, because both use the block marker v1; OAH 1.0 reports \
that drift and restores the block at its next sync. The original file is kept as .oah/config.toml.v0.4.bak.";

impl UpgradeWarning {
    pub(crate) fn for_bytes(bytes: &[u8]) -> Self {
        UpgradeWarning {
            source: Fingerprint::of(bytes),
        }
    }

    pub fn message(&self) -> &'static str {
        UPGRADE_WARNING
    }

    /// Records that the user approved the upgrade of exactly the file this warning was made for.
    pub fn approve(self) -> UpgradeApproval {
        UpgradeApproval {
            source: self.source,
        }
    }
}

/// The user's approval of one upgrade. It can only come from [`UpgradeWarning::approve`] and is valid only for
/// the bytes that warning was made for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpgradeApproval {
    source: Fingerprint,
}

/// The backup of an upgraded v0.4.0 file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Backup {
    /// Written by this save.
    Written(WriteReceipt),
    /// Already present with exactly the original bytes, so it was kept as it is.
    Reused(PathBuf),
}

/// What a save did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SaveOutcome {
    /// The verified write, or `None` when the edits changed nothing and nothing was written.
    pub written: Option<WriteReceipt>,
    /// Whether this save was the one-way upgrade of a v0.4.0 file.
    pub upgraded: bool,
    pub backup: Option<Backup>,
}

/// The bytes a save would write, computed without touching the disk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SavePlan {
    /// `None` when the edits change nothing.
    pub bytes: Option<Vec<u8>>,
    /// Whether the save upgrades a v0.4.0 file.
    pub upgrade: bool,
}

/// The backup path of a project file: `<file>.v0.4.bak`.
pub fn backup_path(config: &Path) -> PathBuf {
    let mut name = config
        .file_name()
        .map(OsString::from)
        .unwrap_or_else(|| OsString::from("config.toml"));
    name.push(".v0.4.bak");
    config.with_file_name(name)
}

/// Computes the new bytes of `.oah/config.toml` for `edits`, including the upgrade of a v0.4.0 file.
///
/// Every key outside the edited ones keeps its bytes. The result is parsed again and must be exactly the old
/// values plus the edits, and must read as a valid 1.0 configuration; the upgrade must not change any effective
/// value.
pub fn plan_project_save(
    snapshot: &ProjectConfig,
    edits: &[Edit],
) -> Result<SavePlan, ConfigError> {
    check_owned(snapshot.file(), edits, &PROJECT_TABLES)?;
    plan_edits(snapshot, edits)
}

/// [`plan_project_save`] without the check of which tables the edits name; each caller makes its own.
fn plan_edits(snapshot: &ProjectConfig, edits: &[Edit]) -> Result<SavePlan, ConfigError> {
    let file = snapshot.file();
    let ops: Vec<Op> = edits.iter().map(Edit::op).collect();
    let values = file.values();
    let changed = patch::apply_to_tree(values, &ops).map_err(ConfigError::invalid)? != *values;
    if !changed {
        return Ok(SavePlan {
            bytes: None,
            upgrade: false,
        });
    }
    let upgrade = snapshot.format() == Format::Legacy;
    let mut text = file.text().to_string();
    let mut current = file.clone();
    if upgrade {
        let upgrade_ops = upgrade_ops(snapshot);
        text = patch::patch(&text, current.values(), &upgrade_ops, file.where_())?;
        current = reread(file, &text)?;
        let (after, _) = interpret(&current)?;
        if !same_effective(snapshot.effective(), &after) {
            return Err(ConfigError::new(
                codes::LAYOUT_UNSUPPORTED,
                format!(
                    "{}: the upgrade would change what the file means; upgrade it by hand",
                    file.where_()
                ),
            ));
        }
    }
    text = patch::patch(&text, current.values(), &ops, file.where_())?;
    let mut proposed = reread(file, &text)?;
    if super::project::classify(&proposed) == Format::Legacy {
        let (effective, _) = interpret(&proposed)?;
        let marker = Op::Set {
            path: vec!["sections".into(), "order".into()],
            value: Value::Array(
                effective
                    .sections
                    .iter()
                    .map(|section| Value::from(section.id.as_str()))
                    .collect(),
            ),
        };
        text = patch::patch(&text, proposed.values(), &[marker], file.where_())?;
        proposed = reread(file, &text)?;
    }
    interpret(&proposed)?;
    if Some(text.as_bytes()) == file.source_bytes().map(strip_bom) {
        return Ok(SavePlan {
            bytes: None,
            upgrade: false,
        });
    }
    Ok(SavePlan {
        bytes: Some(with_bom(file.has_bom(), &text)),
        upgrade,
    })
}

/// Saves `edits` to the project file `snapshot` was read from.
///
/// Under the file's short writer lock the file is read again and must still be the snapshot's bytes, otherwise
/// [`codes::CHANGED`]. A v0.4.0 file is upgraded only with an approval for exactly its bytes, otherwise
/// [`codes::UPGRADE_REQUIRED`]; its backup `.oah/config.toml.v0.4.bak` is written and synced before the file is
/// replaced, an existing backup is never overwritten (reused only when it holds exactly the original bytes,
/// otherwise [`codes::BACKUP_CONFLICT`]). Every refusal happens before anything is written.
pub fn save_project(
    snapshot: &ProjectConfig,
    edits: &[Edit],
    approval: Option<&UpgradeApproval>,
    lock: LockOptions,
) -> Result<SaveOutcome, ConfigError> {
    let plan = plan_project_save(snapshot, edits)?;
    let file = snapshot.file();
    let Some(bytes) = plan.bytes else {
        return Ok(SaveOutcome {
            written: None,
            upgraded: false,
            backup: None,
        });
    };
    if plan.upgrade {
        let approved = match (approval, file.source_bytes()) {
            (Some(approval), Some(bytes)) => approval.source == Fingerprint::of(bytes),
            _ => false,
        };
        if !approved {
            return Err(ConfigError::new(
                codes::UPGRADE_REQUIRED,
                format!(
                    "{}: saving upgrades this v0.4.0 file to the 1.0 format; show the upgrade warning and save again \
                     with its approval",
                    file.where_()
                ),
            ));
        }
    }
    publish(file, &bytes, plan.upgrade, lock)
}

pub(crate) fn publish(
    file: &ConfigFile,
    bytes: &[u8],
    upgrade: bool,
    lock: LockOptions,
) -> Result<SaveOutcome, ConfigError> {
    let path = file.path();
    if !file.exists()
        && let Some(parent) = path.parent()
    {
        std::fs::create_dir_all(parent).map_err(|error| {
            ConfigError::new(
                FsxErrorKind::Io.code(),
                format!("cannot create the folder {}: {error}", parent.display()),
            )
        })?;
    }
    let held = FileLock::acquire(path, lock)?;
    let now = fsx::read_optional(path)?;
    if now.as_deref() != file.source_bytes() {
        return Err(ConfigError::new(
            codes::CHANGED,
            format!(
                "{}: the file changed after it was read; read it again and repeat the change",
                file.where_()
            ),
        ));
    }
    let backup = if upgrade {
        let original = file.source_bytes().unwrap_or_default();
        Some(write_backup(path, original)?)
    } else {
        None
    };
    let receipt = fsx::write_atomic(path, bytes)?;
    held.release()?;
    Ok(SaveOutcome {
        written: Some(receipt),
        upgraded: upgrade,
        backup,
    })
}

const DIR_LABEL: &str = ".oah";
const FILE_LABEL: &str = ".oah/config.toml";
const LOCK_LABEL: &str = ".oah/config.toml.lock";
const FILE_NAME: &str = "config.toml";
const LOCK_NAME: &str = "config.toml.lock";
#[cfg(any(target_os = "macos", windows))]
const BACKUP_NAME: &str = "config.toml.v0.4.bak";
const BACKUP_LABEL: &str = ".oah/config.toml.v0.4.bak";

/// The project a tools change is made for: the id the caller holds for its open project (`paths::folder_id` of
/// the canonical root) and that project's root folder. A plan and its save are bound to both.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolsBinding {
    project_id: String,
    root: PathBuf,
}

impl ToolsBinding {
    pub fn new(project_id: impl Into<String>, root: impl Into<PathBuf>) -> Self {
        ToolsBinding {
            project_id: project_id.into(),
            root: root.into(),
        }
    }

    pub fn project_id(&self) -> &str {
        &self.project_id
    }

    pub fn root(&self) -> &Path {
        &self.root
    }
}

/// One key a tools plan changes, with the file's value before and after; `None` is a key the file does not have.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolsKeyChange {
    pub key: String,
    pub before: Option<Value>,
    pub after: Option<Value>,
}

/// What the upgrade of a v0.4.0 file would do about its backup, as the plan found the backup path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BackupPlan {
    /// No backup yet: the save writes the original bytes there first.
    Write(PathBuf),
    /// A backup with exactly the original bytes is there and is kept.
    Reuse(PathBuf),
    /// Another file is there; the save refuses with `OAH_CONFIG_BACKUP_CONFLICT` until it is moved away.
    Conflict(PathBuf),
}

impl BackupPlan {
    pub fn path(&self) -> &Path {
        match self {
            BackupPlan::Write(path) | BackupPlan::Reuse(path) | BackupPlan::Conflict(path) => path,
        }
    }
}

/// What a tools change would do, computed by reading only. Nothing is written, created or locked to make one.
#[derive(Debug, Clone)]
pub struct ToolsPlan {
    id: String,
    project_id: String,
    root: PathBuf,
    snapshot: ProjectConfig,
    tools: ProjectTools,
    tools_after: ProjectTools,
    after: Option<Vec<u8>>,
    changes: Vec<ToolsKeyChange>,
    kept: Vec<ToolsKept>,
    upgrade: Option<UpgradeWarning>,
    backup: Option<BackupPlan>,
}

impl ToolsPlan {
    /// 64 hexadecimal digits that name this plan: the project id, the canonical root, the exact bytes the file
    /// has now (or that it is missing) and the exact bytes the save would write. An approval carries it back.
    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn project_id(&self) -> &str {
        &self.project_id
    }

    /// The canonical project root every path of the plan lies in.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// `<root>/.oah/config.toml`.
    pub fn config_path(&self) -> &Path {
        self.snapshot.file().path()
    }

    /// The fingerprint of the bytes the plan was made from; `None` when the file does not exist.
    pub fn before(&self) -> Option<Fingerprint> {
        self.snapshot.file().source_bytes().map(Fingerprint::of)
    }

    /// The settings as the file has them now.
    pub fn tools(&self) -> &ProjectTools {
        &self.tools
    }

    /// The settings as the file would have them after the save.
    pub fn tools_after(&self) -> &ProjectTools {
        &self.tools_after
    }

    /// The keys the save changes, in the order of `TOOL_KEYS`. Empty when there is nothing to write.
    pub fn changes(&self) -> &[ToolsKeyChange] {
        &self.changes
    }

    /// Values the change leaves as the file has them, with the reason.
    pub fn kept(&self) -> &[ToolsKept] {
        &self.kept
    }

    /// The bytes the save would write; `None` when the file already says what the change asks.
    pub fn after_bytes(&self) -> Option<&[u8]> {
        self.after.as_deref()
    }

    /// For a v0.4.0 file: the one-way upgrade warning the user must approve for exactly these bytes.
    pub fn upgrade_warning(&self) -> Option<&UpgradeWarning> {
        self.upgrade.as_ref()
    }

    /// For a v0.4.0 file: where the original is kept and what the save finds there.
    pub fn backup(&self) -> Option<&BackupPlan> {
        self.backup.as_ref()
    }

    /// The user's approval of this plan. `upgrade` records that the upgrade warning was shown and accepted; it
    /// is ignored for a plan that upgrades nothing.
    pub fn approve(&self, upgrade: bool) -> ToolsApproval {
        ToolsApproval {
            plan_id: self.id.clone(),
            upgrade: match (upgrade, &self.upgrade) {
                (true, Some(_)) => self.before(),
                _ => None,
            },
        }
    }
}

/// An approval of one tools plan: its id and, for a v0.4.0 file, the fingerprint of the bytes whose upgrade the
/// user accepted. It is plain data so it can cross a process boundary; it is only ever valid for the one plan
/// and file it names.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolsApproval {
    plan_id: String,
    upgrade: Option<Fingerprint>,
}

impl ToolsApproval {
    pub fn new(plan_id: impl Into<String>, upgrade: Option<Fingerprint>) -> Self {
        ToolsApproval {
            plan_id: plan_id.into(),
            upgrade,
        }
    }

    pub fn plan_id(&self) -> &str {
        &self.plan_id
    }

    pub fn upgrade(&self) -> Option<Fingerprint> {
        self.upgrade
    }
}

enum Unsafe {
    Link,
    NotFolder,
    NotFile,
}

fn unsafe_entry(label: &str, what: Unsafe, saved: bool) -> ConfigError {
    let what = match what {
        Unsafe::Link => "a link or reparse entry",
        Unsafe::NotFolder => "not a regular folder",
        Unsafe::NotFile => "not a regular file",
    };
    if saved {
        ConfigError::new(
            FsxErrorKind::Unverified.code(),
            format!(
                "{label} is {what} after the save; OAH only writes regular project configuration files inside \
                 the selected project, so read the file again before relying on it"
            ),
        )
    } else {
        ConfigError::new(
            FsxErrorKind::Io.code(),
            format!(
                "{label} is {what}; OAH only writes regular project configuration files inside the selected \
                 project; nothing was written"
            ),
        )
    }
}

/// Inspects one entry without following a link or a Windows reparse point. `Ok(true)` when it is there as the
/// expected regular folder or file, `Ok(false)` when it is absent.
fn check_entry(
    path: &Path,
    expected: ExpectedEntry,
    label: &str,
    saved: bool,
) -> Result<bool, ConfigError> {
    match inspect_project_entry(path, expected)? {
        InspectedEntry::Missing => Ok(false),
        InspectedEntry::Allowed => Ok(true),
        InspectedEntry::LinkOrReparse => Err(unsafe_entry(label, Unsafe::Link, saved)),
        InspectedEntry::Unsupported => Err(unsafe_entry(
            label,
            match expected {
                ExpectedEntry::Directory => Unsafe::NotFolder,
                ExpectedEntry::RegularFile => Unsafe::NotFile,
            },
            saved,
        )),
    }
}

/// Reads a regular file without following its final link.
fn read_entry(path: &Path, label: &str, saved: bool) -> Result<Option<Vec<u8>>, ConfigError> {
    match fsx::read_regular_optional(path) {
        Ok(read) => Ok(read.map(|(bytes, _)| bytes)),
        Err(error) => {
            if let Some(io) = error.io_error() {
                #[cfg(unix)]
                if rustix::io::Errno::from_io_error(io) == Some(rustix::io::Errno::LOOP) {
                    return Err(unsafe_entry(label, Unsafe::Link, saved));
                }
                if io.kind() == ErrorKind::InvalidData {
                    return Err(unsafe_entry(label, Unsafe::NotFile, saved));
                }
            }
            Err(error.into())
        }
    }
}

/// The canonical root of the bound project, or `OAH_PROJECT_CHANGED` when the folder no longer resolves to the
/// project the binding names.
fn bound_root(project_id: &str, root: &Path) -> Result<PathBuf, ConfigError> {
    let changed = |why: String| {
        ConfigError::new(
            codes::PROJECT_CHANGED,
            format!(
                "{}: {why}; open the project again and make a new plan; nothing was written",
                root.display()
            ),
        )
    };
    let canonical = paths::canonical_folder(root)
        .map_err(|error| changed(format!("the project folder cannot be resolved ({error})")))?;
    if !canonical.is_dir() {
        return Err(changed(String::from("it is not a folder")));
    }
    if paths::folder_id(&canonical) != project_id {
        return Err(changed(String::from(
            "this folder is not the project the change was made for",
        )));
    }
    Ok(canonical)
}

/// Reads `<root>/.oah/config.toml` through a regular `.oah` folder and a regular file, following no link.
fn read_bound_config(root: &Path) -> Result<ProjectConfig, ConfigError> {
    let dir = root.join(".oah");
    let path = dir.join(FILE_NAME);
    let bytes = if check_entry(&dir, ExpectedEntry::Directory, DIR_LABEL, false)? {
        check_entry(&path, ExpectedEntry::RegularFile, FILE_LABEL, false)?;
        read_entry(&path, FILE_LABEL, false)?
    } else {
        None
    };
    ProjectConfig::from_file(ConfigFile::from_bytes(&path, bytes)?)
}

fn plan_backup(config: &Path, original: &[u8]) -> Result<BackupPlan, ConfigError> {
    let path = backup_path(config);
    check_entry(&path, ExpectedEntry::RegularFile, BACKUP_LABEL, false)?;
    Ok(match read_entry(&path, BACKUP_LABEL, false)? {
        None => BackupPlan::Write(path),
        Some(existing) if existing == original => BackupPlan::Reuse(path),
        Some(_) => BackupPlan::Conflict(path),
    })
}

fn tools_plan_id(
    project_id: &str,
    root: &Path,
    before: Option<Fingerprint>,
    after: Option<&[u8]>,
    upgrade: bool,
) -> String {
    let before = before.map(Fingerprint::to_hex);
    let after = after.map(|bytes| Fingerprint::of(bytes).to_hex());
    let parts: [&[u8]; 6] = [
        b"oah-project-tools-plan-v1",
        project_id.as_bytes(),
        root.as_os_str().as_encoded_bytes(),
        before.as_deref().unwrap_or("missing").as_bytes(),
        after.as_deref().unwrap_or("unchanged").as_bytes(),
        &[u8::from(upgrade)],
    ];
    let mut buffer = Vec::new();
    for part in parts {
        buffer.extend_from_slice(&(part.len() as u64).to_le_bytes());
        buffer.extend_from_slice(part);
    }
    Fingerprint::of(&buffer).to_hex()
}

/// Plans `changes` to the Project Wiki, Impact Map and Jev settings of the bound project. Reads only.
///
/// The folder must resolve to the bound project (`OAH_PROJECT_CHANGED` otherwise). `.oah` and its
/// `config.toml` are read without following a link, a junction or another reparse point; one of those, or a
/// non-regular entry, is `OAH_FILE_IO`. A file that cannot be read as a valid configuration, including its tools
/// tables, is `OAH_CONFIG_INVALID`: OAH does not edit what it cannot read. A v0.4.0 file is planned as its
/// one-way upgrade, with the warning to approve and the state of its backup.
pub fn plan_tools_save(
    binding: &ToolsBinding,
    changes: &[ToolsChange],
) -> Result<ToolsPlan, ConfigError> {
    let root = bound_root(&binding.project_id, &binding.root)?;
    let snapshot = read_bound_config(&root)?;
    let plan = build_tools_plan(&binding.project_id, root, snapshot, changes, plan_backup)?;
    if plan.after.is_some() {
        check_entry(
            &plan.config_path().with_file_name(LOCK_NAME),
            ExpectedEntry::RegularFile,
            LOCK_LABEL,
            false,
        )?;
    }
    Ok(plan)
}

/// The plan for `changes` to the file read as `snapshot`; `backup` reports what is at the backup path of a v0.4.0
/// file.
fn build_tools_plan(
    project_id: &str,
    root: PathBuf,
    snapshot: ProjectConfig,
    changes: &[ToolsChange],
    backup: impl FnOnce(&Path, &[u8]) -> Result<BackupPlan, ConfigError>,
) -> Result<ToolsPlan, ConfigError> {
    let file = snapshot.file();
    let current = ProjectTools::read(file)?;
    let (edits, kept) = tools::edits_for(file.where_(), &current, changes)?;
    for edit in &edits {
        let owned = TOOL_KEYS.iter().any(|key| {
            key.iter()
                .copied()
                .eq(edit.path().iter().map(String::as_str))
        });
        if !owned {
            return Err(ConfigError::new(
                codes::KEY_NOT_OWNED,
                format!(
                    "{}: {} is not a project-tools key",
                    file.where_(),
                    patch::dotted(edit.path())
                ),
            ));
        }
    }
    let save = plan_edits(&snapshot, &edits)?;
    let (tools_after, key_changes) = match &save.bytes {
        None => (current.clone(), Vec::new()),
        Some(bytes) => {
            let proposed = ConfigFile::from_bytes(file.path(), Some(bytes.clone()))?;
            let ops: Vec<Op> = edits.iter().map(Edit::op).collect();
            let expected =
                patch::apply_to_tree(file.values(), &ops).map_err(ConfigError::invalid)?;
            let mut key_changes = Vec::new();
            for key in TOOL_KEYS {
                let (before, after) =
                    (file.values().get_path(key), proposed.values().get_path(key));
                // The upgrade of a v0.4.0 file and the 1.0 marker must leave every tools value alone.
                if after != expected.get_path(key) {
                    return Err(ConfigError::new(
                        codes::LAYOUT_UNSUPPORTED,
                        format!(
                            "{}: the save would change {} in a way the change did not ask for; edit the file by \
                             hand",
                            file.where_(),
                            key.join(".")
                        ),
                    ));
                }
                if before != after {
                    key_changes.push(ToolsKeyChange {
                        key: key.join("."),
                        before: before.cloned(),
                        after: after.cloned(),
                    });
                }
            }
            (ProjectTools::read(&proposed)?, key_changes)
        }
    };
    let upgrade = if save.upgrade {
        snapshot.upgrade_warning()
    } else {
        None
    };
    let backup = match (&upgrade, file.source_bytes()) {
        (Some(_), Some(original)) => Some(backup(file.path(), original)?),
        _ => None,
    };
    let id = tools_plan_id(
        project_id,
        &root,
        file.source_bytes().map(Fingerprint::of),
        save.bytes.as_deref(),
        upgrade.is_some(),
    );
    Ok(ToolsPlan {
        id,
        project_id: project_id.to_string(),
        root,
        tools: current,
        tools_after,
        after: save.bytes,
        changes: key_changes,
        kept,
        upgrade,
        backup,
        snapshot,
    })
}

/// Saves `changes` exactly as the approved plan showed them.
///
/// The write is bound to the opened project folder, not to its path. Every folder from the volume root down to
/// the project root, and `.oah` in it, is opened without following a link and held open; the file, its lock and
/// its backup are then reached only through those handles (`fsx::skill_publish::BoundFolder`). Swapping the root,
/// `.oah` or the file for a link, a junction or another folder after that cannot move the write: each step
/// compares the names with the handles again and stops.
///
/// The plan is made again from the bytes read through those handles and must be the approved one, otherwise
/// `OAH_PLAN_STALE`: a plan for another project or root, for a file that changed since, or for other changes
/// cannot be used. A v0.4.0 file needs the approval of the upgrade of exactly its bytes
/// (`OAH_CONFIG_UPGRADE_REQUIRED`), and its backup must be new or identical (`OAH_CONFIG_BACKUP_CONFLICT`).
///
/// Then, under the writer lock that [`save_project`] also takes, the file is opened once more and must still hold
/// the planned bytes (`OAH_CONFIG_CHANGED`). The backup is written without replacing anything. The file is
/// replaced in one atomic step that is refused when the name no longer is that opened file, so a reader finds
/// the old or the new file and never a missing one, also when the process stops half-way. A missing file is
/// created without replacing anything. The result is read back by identity and bytes before a receipt is made.
/// Only `.oah` is ever created, directly in the bound root, and removed again when the save fails after it.
///
/// Where the folder cannot be bound the save is refused with `OAH_FILE_IO` and nothing is written: on a volume
/// that is not local APFS (macOS) or fixed NTFS (Windows), and on other systems.
#[cfg(any(target_os = "macos", windows))]
pub fn apply_tools_save(
    binding: &ToolsBinding,
    changes: &[ToolsChange],
    approval: &ToolsApproval,
    lock: LockOptions,
) -> Result<SaveOutcome, ConfigError> {
    let root = bound_root(&binding.project_id, &binding.root)?;
    let dir = root.join(".oah");
    let path = dir.join(FILE_NAME);
    let backup_file = backup_path(&path);
    // These inspections only name the entry in a refusal; what binds the write is the handles opened next.
    if check_entry(&dir, ExpectedEntry::Directory, DIR_LABEL, false)? {
        check_entry(&path, ExpectedEntry::RegularFile, FILE_LABEL, false)?;
        check_entry(
            &path.with_file_name(LOCK_NAME),
            ExpectedEntry::RegularFile,
            LOCK_LABEL,
            false,
        )?;
    }
    let mut folder = BoundFolder::open(&root, ".oah").map_err(|error| bound_error(error, &dir))?;
    still_bound(binding, &root)?;
    let seen = folder
        .read(FILE_NAME)
        .map_err(|error| bound_error(error, &path))?
        .map(|file| file.bytes().to_vec());
    let snapshot = ProjectConfig::from_file(ConfigFile::from_bytes(&path, seen)?)?;
    let plan = build_tools_plan(
        &binding.project_id,
        root.clone(),
        snapshot,
        changes,
        |_, original| {
            check_entry(
                &backup_file,
                ExpectedEntry::RegularFile,
                BACKUP_LABEL,
                false,
            )?;
            let found = folder
                .read(BACKUP_NAME)
                .map_err(|error| bound_error(error, &backup_file))?;
            Ok(match found {
                None => BackupPlan::Write(backup_file.clone()),
                Some(existing) if existing.bytes() == original => {
                    BackupPlan::Reuse(backup_file.clone())
                }
                Some(_) => BackupPlan::Conflict(backup_file.clone()),
            })
        },
    )?;
    if plan.id != approval.plan_id {
        return Err(ConfigError::new(
            codes::PLAN_STALE,
            format!(
                "{}: the approved plan is not the plan for this project and file now; make a new plan and \
                 approve it; nothing was written",
                plan.snapshot.file().where_()
            ),
        ));
    }
    let Some(bytes) = plan.after.as_deref() else {
        return Ok(SaveOutcome {
            written: None,
            upgraded: false,
            backup: None,
        });
    };
    if plan.upgrade.is_some() {
        if approval.upgrade.is_none() || approval.upgrade != plan.before() {
            return Err(ConfigError::new(
                codes::UPGRADE_REQUIRED,
                format!(
                    "{}: saving upgrades this v0.4.0 file to the 1.0 format; show the upgrade warning and save again \
                     with its approval",
                    plan.snapshot.file().where_()
                ),
            ));
        }
        if let Some(BackupPlan::Conflict(path)) = &plan.backup {
            return Err(backup_conflict(path));
        }
    }
    let created = folder.create().map_err(|error| bound_error(error, &dir))?;
    let outcome = publish_bound(binding, &plan, &folder, bytes, lock);
    if outcome.is_err() && created {
        // Only the empty folder this save made is removed, so a refusal leaves no trace.
        let _ = folder.discard();
    }
    outcome
}

/// On a system where OAH cannot hold the project folder open, nothing is saved.
#[cfg(not(any(target_os = "macos", windows)))]
pub fn apply_tools_save(
    binding: &ToolsBinding,
    _changes: &[ToolsChange],
    _approval: &ToolsApproval,
    _lock: LockOptions,
) -> Result<SaveOutcome, ConfigError> {
    Err(ConfigError::new(
        FsxErrorKind::Io.code(),
        format!(
            "{}: these settings are saved only on macOS and Windows, where OAH binds the write to the opened \
             project folder; nothing was written",
            binding.root.display()
        ),
    ))
}

/// The folder must still resolve to the bound project and to the root that was opened.
#[cfg(any(target_os = "macos", windows))]
fn still_bound(binding: &ToolsBinding, root: &Path) -> Result<(), ConfigError> {
    if bound_root(&binding.project_id, &binding.root)? != root {
        return Err(ConfigError::new(
            codes::PROJECT_CHANGED,
            format!(
                "{}: the project folder moved while saving; open the project again and make a new plan; nothing \
                 was written",
                root.display()
            ),
        ));
    }
    Ok(())
}

/// A refusal of the bound folder as a configuration error about `at`.
#[cfg(any(target_os = "macos", windows))]
fn bound_error(error: PublishError, at: &Path) -> ConfigError {
    let at = at.display();
    match error {
        PublishError::Busy => ConfigError::new(
            FsxErrorKind::Busy.code(),
            format!("another writer or reader held it for the whole wait; try again ({at})"),
        ),
        PublishError::UnsafePath => ConfigError::new(
            FsxErrorKind::Io.code(),
            format!(
                "{at}: the project folder, .oah or this file is a link, a reparse entry or not a plain folder \
                 or file, or changed while saving; OAH only writes regular project configuration files inside \
                 the selected project; nothing was written"
            ),
        ),
        PublishError::Unsupported => ConfigError::new(
            FsxErrorKind::Io.code(),
            format!(
                "{at}: these settings are saved only on a local APFS or fixed NTFS volume, where OAH binds the \
                 write to the opened project folder; nothing was written"
            ),
        ),
        PublishError::Changed | PublishError::Occupied => ConfigError::new(
            codes::CHANGED,
            format!(
                "{at}: the file changed after it was read; read it again and repeat the change"
            ),
        ),
        PublishError::MovedUnverified { path } => ConfigError::new(
            FsxErrorKind::Unverified.code(),
            format!(
                "{}: the save could not be verified here; read the file again before relying on it",
                path.display()
            ),
        ),
        other => ConfigError::new(FsxErrorKind::Io.code(), format!("{at}: {other}")),
    }
}

#[cfg(any(target_os = "macos", windows))]
fn publish_bound(
    binding: &ToolsBinding,
    plan: &ToolsPlan,
    folder: &BoundFolder,
    bytes: &[u8],
    lock: LockOptions,
) -> Result<SaveOutcome, ConfigError> {
    let file = plan.snapshot.file();
    let path = file.path();
    let held = folder
        .lock(LOCK_NAME, lock.wait, lock.poll)
        .map_err(|error| bound_error(error, path))?;
    still_bound(binding, &plan.root)?;
    let current = folder
        .hold(FILE_NAME)
        .map_err(|error| bound_error(error, path))?;
    if current.as_ref().map(BoundFile::bytes) != file.source_bytes() {
        return Err(bound_error(PublishError::Changed, path));
    }
    let backup = match (&plan.upgrade, file.source_bytes()) {
        (Some(_), Some(original)) => {
            Some(write_bound_backup(folder, &backup_path(path), original)?)
        }
        _ => None,
    };
    match current {
        Some(before) => folder.replace(FILE_NAME, before, bytes),
        None => folder.publish_new(FILE_NAME, bytes),
    }
    .map_err(|error| bound_error(error, path))?;
    folder
        .check()
        .map_err(|_| bound_error(PublishError::MovedUnverified { path: path.into() }, path))?;
    held.release()
        .map_err(|error| bound_error(error, &path.with_file_name(LOCK_NAME)))?;
    Ok(SaveOutcome {
        written: Some(WriteReceipt::verified(path, bytes)),
        upgraded: plan.upgrade.is_some(),
        backup,
    })
}

/// Keeps the original of an upgraded v0.4.0 file: written through the bound folder without replacing anything,
/// or reused when the same bytes are already there.
#[cfg(any(target_os = "macos", windows))]
fn write_bound_backup(
    folder: &BoundFolder,
    path: &Path,
    original: &[u8],
) -> Result<Backup, ConfigError> {
    let found = folder
        .read(BACKUP_NAME)
        .map_err(|error| bound_error(error, path))?;
    match found {
        Some(existing) if existing.bytes() == original => Ok(Backup::Reused(path.to_path_buf())),
        Some(_) => Err(backup_conflict(path)),
        None => match folder.publish_new(BACKUP_NAME, original) {
            Ok(()) => Ok(Backup::Written(WriteReceipt::verified(path, original))),
            Err(PublishError::Occupied) => Err(backup_conflict(path)),
            Err(error) => Err(bound_error(error, path)),
        },
    }
}

fn backup_conflict(path: &Path) -> ConfigError {
    ConfigError::new(
        codes::BACKUP_CONFLICT,
        format!(
            "{} already exists and is not the file being upgraded; move it away or delete it, then save \
             again",
            path.display()
        ),
    )
}

fn write_backup(config: &Path, original: &[u8]) -> Result<Backup, ConfigError> {
    let path = backup_path(config);
    match fsx::publish_new(&path, original) {
        Ok(receipt) => Ok(Backup::Written(receipt)),
        Err(error) if error.kind() == FsxErrorKind::Exists => match fsx::read_optional(&path)? {
            Some(existing) if existing == original => Ok(Backup::Reused(path)),
            _ => Err(backup_conflict(&path)),
        },
        Err(error) => Err(error.into()),
    }
}

pub(crate) fn check_owned(
    file: &ConfigFile,
    edits: &[Edit],
    tables: &[&str],
) -> Result<(), ConfigError> {
    for edit in edits {
        let path = edit.path();
        match path.first() {
            Some(first) if path.len() >= 2 && tables.contains(&first.as_str()) => {}
            _ => {
                return Err(ConfigError::new(
                    codes::KEY_NOT_OWNED,
                    format!(
                        "{}: {} is not a key OAH writes; OAH writes keys inside: {}",
                        file.where_(),
                        if path.is_empty() {
                            String::from("an empty key")
                        } else {
                            patch::dotted(path)
                        },
                        tables.join(", ")
                    ),
                ));
            }
        }
    }
    Ok(())
}

pub(crate) fn reread(file: &ConfigFile, text: &str) -> Result<ConfigFile, ConfigError> {
    ConfigFile::from_bytes(file.path(), Some(text.as_bytes().to_vec()))
}

fn strip_bom(bytes: &[u8]) -> &[u8] {
    bytes.strip_prefix(BOM).unwrap_or(bytes)
}

pub(crate) fn with_bom(bom: bool, text: &str) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(text.len() + 3);
    if bom {
        bytes.extend_from_slice(BOM);
    }
    bytes.extend_from_slice(text.as_bytes());
    bytes
}

/// The edits of the one-way upgrade: the four built-in `[stages]` keys written with their effective agents
/// (`initial` replaced in place by plan and build, `correction` by fix), and an `[agents.<id>]` table for each
/// `[engines.<id>]` read as one. `[engines.*]` stays in the file.
fn upgrade_ops(snapshot: &ProjectConfig) -> Vec<Op> {
    let effective = snapshot.effective();
    let stages = snapshot
        .file()
        .values()
        .get("stages")
        .and_then(Value::as_table);
    let has = |key: &str| stages.is_some_and(|table| table.contains_key(key));
    let agent =
        |section: &str| Value::from(effective.agent_for(section).map_or("", AgentId::as_str));
    let path = |parts: &[&str]| {
        parts
            .iter()
            .map(|part| part.to_string())
            .collect::<Vec<_>>()
    };
    let mut ops = Vec::new();
    if has("initial") {
        ops.push(Op::Rename {
            path: path(&["stages", "initial"]),
            entries: vec![
                ("plan".into(), agent("plan")),
                ("build".into(), agent("build")),
            ],
        });
    } else {
        ops.push(Op::Set {
            path: path(&["stages", "plan"]),
            value: agent("plan"),
        });
        ops.push(Op::Set {
            path: path(&["stages", "build"]),
            value: agent("build"),
        });
    }
    ops.push(Op::Set {
        path: path(&["stages", "review"]),
        value: agent("review"),
    });
    if has("correction") {
        ops.push(Op::Rename {
            path: path(&["stages", "correction"]),
            entries: vec![("fix".into(), agent("fix"))],
        });
    } else {
        ops.push(Op::Set {
            path: path(&["stages", "fix"]),
            value: agent("fix"),
        });
    }
    for config in &effective.agents {
        if config.source != AgentSource::Engines {
            continue;
        }
        for (key, value) in config.given.iter() {
            match value {
                Value::Table(inner) => {
                    for (sub, item) in inner.iter() {
                        ops.push(Op::Set {
                            path: path(&["agents", config.id.as_str(), key, sub]),
                            value: item.clone(),
                        });
                    }
                }
                other => ops.push(Op::Set {
                    path: path(&["agents", config.id.as_str(), key]),
                    value: other.clone(),
                }),
            }
        }
    }
    ops
}

/// Whether two readings mean the same: sections, assignments, agent settings, models and skills.
fn same_effective(before: &Effective, after: &Effective) -> bool {
    let assignments = |effective: &Effective| {
        effective
            .stages
            .iter()
            .map(|assignment| (assignment.section.clone(), assignment.agent))
            .collect::<Vec<_>>()
    };
    let agent_values = |agent: &AgentConfig| {
        (
            agent.command.clone(),
            agent.quality_order.clone(),
            agent.allowed_models.clone(),
            agent.effort_caps.clone(),
            agent.tier_pools.clone(),
            agent.tiers.clone(),
        )
    };
    before.sections == after.sections
        && assignments(before) == assignments(after)
        && before.agents.len() == after.agents.len()
        && before
            .agents
            .iter()
            .zip(&after.agents)
            .all(|(a, b)| agent_values(a) == agent_values(b))
        && before.models == after.models
        && before.skills == after.skills
}
