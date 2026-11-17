// 사용자 설정: OAH_HOME/settings.toml 을 같은 파서·병합·형식 보존 저장으로 다루며 비밀·동의·네트워크 값은 두지 않는다.
use super::error::{ConfigError, Notice};
use super::file::ConfigFile;
use super::patch::{self, Op};
use super::project::merge_top;
use super::save::{Edit, SaveOutcome, check_owned, publish, reread, with_bom};
use super::value::{Table, Value};
use crate::fsx::LockOptions;
use std::path::Path;

/// The top-level tables of `settings.toml` that OAH writes.
pub const SETTINGS_TABLES: [&str; 3] = ["automation", "autoload", "history"];

/// The automation levels of `[automation] level`.
pub const AUTOMATION_LEVELS: [&str; 2] = ["auto-safe", "recommend-only"];

/// `[autoload]`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutoloadSettings {
    pub enabled: bool,
    pub max_skills: i64,
    pub budget_ms: i64,
}

/// `[history]`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistorySettings {
    pub enabled: bool,
    pub retention_days: i64,
}

/// The user preferences 1.0 reads in this slice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SettingsValues {
    pub automation_level: String,
    pub autoload: AutoloadSettings,
    pub history: HistorySettings,
}

/// `OAH_HOME/settings.toml` as read. Other tables in the file are kept and not read here.
#[derive(Debug, Clone)]
pub struct UserSettings {
    file: ConfigFile,
    values: SettingsValues,
    notices: Vec<Notice>,
}

fn defaults(table: &str) -> Table {
    let mut out = Table::new();
    match table {
        "automation" => {
            out.insert("level", Value::from("auto-safe"));
        }
        "autoload" => {
            out.insert("enabled", Value::from(true));
            out.insert("max_skills", Value::from(3i64));
            out.insert("budget_ms", Value::from(1500i64));
        }
        "history" => {
            out.insert("enabled", Value::from(true));
            out.insert("retention_days", Value::from(90i64));
        }
        _ => {}
    }
    out
}

impl UserSettings {
    /// Reads `path` (normally from [`crate::paths::settings_file`]); a missing file is the defaults.
    pub fn read(path: &Path) -> Result<UserSettings, ConfigError> {
        UserSettings::from_file(ConfigFile::read(path)?)
    }

    pub fn from_file(file: ConfigFile) -> Result<UserSettings, ConfigError> {
        let (values, notices) = interpret(&file)?;
        Ok(UserSettings {
            file,
            values,
            notices,
        })
    }

    pub fn file(&self) -> &ConfigFile {
        &self.file
    }

    pub fn values(&self) -> &SettingsValues {
        &self.values
    }

    pub fn notices(&self) -> &[Notice] {
        &self.notices
    }
}

fn interpret(file: &ConfigFile) -> Result<(SettingsValues, Vec<Notice>), ConfigError> {
    let where_ = file.where_();
    let values = file.values();
    let mut notices = Vec::new();
    let automation = merge_top(
        where_,
        values,
        "automation",
        &defaults("automation"),
        &mut notices,
    )?;
    let autoload = merge_top(
        where_,
        values,
        "autoload",
        &defaults("autoload"),
        &mut notices,
    )?;
    let history = merge_top(
        where_,
        values,
        "history",
        &defaults("history"),
        &mut notices,
    )?;
    let automation_level = automation
        .get("level")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if !AUTOMATION_LEVELS.contains(&automation_level.as_str()) {
        return Err(ConfigError::invalid(format!(
            "{where_}: automation.level = {}; use one of: {}",
            Value::from(automation_level.as_str()),
            AUTOMATION_LEVELS.join(", ")
        )));
    }
    let count = |table: &Table, section: &str, key: &str| -> Result<i64, ConfigError> {
        let number = table
            .get(key)
            .and_then(Value::as_integer)
            .unwrap_or_default();
        if number < 0 {
            return Err(ConfigError::invalid(format!(
                "{where_}: {section}.{key} must be zero or more"
            )));
        }
        Ok(number)
    };
    let flag = |table: &Table, key: &str| table.get(key).and_then(Value::as_bool).unwrap_or(true);
    Ok((
        SettingsValues {
            automation_level,
            autoload: AutoloadSettings {
                enabled: flag(&autoload, "enabled"),
                max_skills: count(&autoload, "autoload", "max_skills")?,
                budget_ms: count(&autoload, "autoload", "budget_ms")?,
            },
            history: HistorySettings {
                enabled: flag(&history, "enabled"),
                retention_days: count(&history, "history", "retention_days")?,
            },
        },
        notices,
    ))
}

/// Saves `edits` to the settings file `snapshot` was read from, with the same guarantees as a project save:
/// only the edited keys change, the result must read as valid settings, and under the writer lock the file must
/// still be the snapshot's bytes. A missing file and its folder are created.
pub fn save_settings(
    snapshot: &UserSettings,
    edits: &[Edit],
    lock: LockOptions,
) -> Result<SaveOutcome, ConfigError> {
    let file = snapshot.file();
    check_owned(file, edits, &SETTINGS_TABLES)?;
    let ops: Vec<Op> = edits.iter().map(Edit::op).collect();
    let text = patch::patch(file.text(), file.values(), &ops, file.where_())?;
    if text == file.text() {
        return Ok(SaveOutcome {
            written: None,
            upgraded: false,
            backup: None,
        });
    }
    interpret(&reread(file, &text)?)?;
    publish(file, &with_bom(file.has_bom(), &text), false, lock)
}
