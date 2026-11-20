// 사용자 설정: OAH_HOME/settings.toml 을 같은 파서·병합·형식 보존 저장으로 다루며 비밀·동의·네트워크 값은 두지 않는다.
use super::error::{ConfigError, Notice, codes};
use super::file::ConfigFile;
use super::patch::{self, Op};
use super::project::merge_top;
use super::save::{Edit, SaveOutcome, check_owned, publish, reread, with_bom};
use super::value::{Table, Value};
use crate::fsx::LockOptions;
use std::path::Path;

/// The top-level tables of `settings.toml` that OAH writes.
pub const SETTINGS_TABLES: [&str; 4] = ["automation", "autoload", "history", "ui"];

/// The automation levels of `[automation] level`.
pub const AUTOMATION_LEVELS: [&str; 2] = ["auto-safe", "recommend-only"];

/// The values of `[ui] theme`.
pub const THEMES: [&str; 3] = ["auto", "light", "dark"];

/// `[ui]`: the app's own preferences that survive a restart (DECISIONS D-19).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UiSettings {
    pub theme: String,
    pub quick_start_dismissed: bool,
}

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
    pub ui: UiSettings,
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
        "ui" => {
            out.insert("theme", Value::from("auto"));
            out.insert("quick_start_dismissed", Value::from(false));
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
    let ui = merge_top(where_, values, "ui", &defaults("ui"), &mut notices)?;
    let theme = ui
        .get("theme")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if !THEMES.contains(&theme.as_str()) {
        return Err(ConfigError::invalid(format!(
            "{where_}: ui.theme = {}; use one of: {}",
            Value::from(theme.as_str()),
            THEMES.join(", ")
        )));
    }
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
            ui: UiSettings {
                theme,
                quick_start_dismissed: ui
                    .get("quick_start_dismissed")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
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
    for edit in edits {
        let (Edit::Set { path, .. } | Edit::Remove { path }) = edit;
        if !matches!(path.as_slice(), [table, key] if defaults(table).get(key).is_some()) {
            return Err(ConfigError::new(
                codes::KEY_NOT_OWNED,
                format!(
                    "{}: {} is not a key OAH writes",
                    file.where_(),
                    patch::dotted(path)
                ),
            ));
        }
    }
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
