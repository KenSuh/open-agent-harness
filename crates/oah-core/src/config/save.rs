// 형식 보존 저장: 짧은 잠금 아래 읽은 원본을 다시 확인하고, v0.4.0 파일은 경고 승인과 백업 뒤에만 1.0으로 올린다.
use super::error::{ConfigError, codes};
use super::file::ConfigFile;
use super::patch::{self, Op};
use super::project::{
    AgentConfig, AgentId, AgentSource, Effective, Format, ProjectConfig, interpret,
};
use super::value::Value;
use crate::fsx::{self, FileLock, Fingerprint, FsxErrorKind, LockOptions, WriteReceipt};
use std::ffi::OsString;
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
    let file = snapshot.file();
    check_owned(file, edits, &PROJECT_TABLES)?;
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
    let proposed = reread(file, &text)?;
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

fn write_backup(config: &Path, original: &[u8]) -> Result<Backup, ConfigError> {
    let path = backup_path(config);
    match fsx::publish_new(&path, original) {
        Ok(receipt) => Ok(Backup::Written(receipt)),
        Err(error) if error.kind() == FsxErrorKind::Exists => match fsx::read_optional(&path)? {
            Some(existing) if existing == original => Ok(Backup::Reused(path)),
            _ => Err(ConfigError::new(
                codes::BACKUP_CONFLICT,
                format!(
                    "{} already exists and is not the file being upgraded; move it away or delete it, then save \
                     again",
                    path.display()
                ),
            )),
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
