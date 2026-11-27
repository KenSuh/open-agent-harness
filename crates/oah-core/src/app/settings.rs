// 설정 작업: settings.toml 을 읽어 보여 주고, 점 표기 키 변경을 설정 저장 경로로만 쓰며 자기 쓰기를 기록한다.
use super::Ctx;
use crate::config::{
    ConfigError, Edit, SettingsValues, UserSettings, Value, codes as config_codes, save_settings,
};
use crate::dto::{Empty, SettingEdit, SettingValue, SettingsDto, SettingsSetReq, UiDto};
use crate::error::Error;
use crate::fsx::LockOptions;
use crate::paths;
use std::path::Path;

/// `settings_get`: the user settings the app shows.
pub fn settings_get(ctx: &Ctx<'_>, _req: Empty) -> Result<SettingsDto, Error> {
    let settings = UserSettings::read(&paths::settings_file(ctx.env)?)?;
    Ok(settings_dto(settings.values()))
}

/// `settings_set`: applies dotted-key edits such as `ui.theme` through `config::save_settings`, records the
/// write, and returns the settings as saved. A key outside the settings OAH owns, or with an empty segment, is
/// `OAH_CONFIG_KEY_NOT_OWNED`; a value the settings reject is `OAH_CONFIG_INVALID`; nothing is written then.
pub fn settings_set(ctx: &Ctx<'_>, req: SettingsSetReq) -> Result<SettingsDto, Error> {
    let path = paths::settings_file(ctx.env)?;
    let snapshot = UserSettings::read(&path)?;
    let edits = req
        .edits
        .iter()
        .map(|edit| to_edit(&path, edit))
        .collect::<Result<Vec<_>, ConfigError>>()?;
    let saved = save_settings(&snapshot, &edits, LockOptions::default())?;
    if let Some(receipt) = &saved.written {
        ctx.session.self_writes().record(receipt);
    }
    Ok(settings_dto(UserSettings::read(&path)?.values()))
}

fn to_edit(file: &Path, edit: &SettingEdit) -> Result<Edit, ConfigError> {
    let path: Vec<&str> = edit.key.split('.').collect();
    if path.iter().any(|segment| segment.is_empty()) {
        return Err(ConfigError::new(
            config_codes::KEY_NOT_OWNED,
            format!(
                "{}: {:?} is not a key OAH writes; write keys as table.key, such as ui.theme",
                file.display(),
                edit.key
            ),
        ));
    }
    let value = match &edit.value {
        SettingValue::Bool(flag) => Value::from(*flag),
        SettingValue::Integer(number) => Value::from(*number),
        SettingValue::Text(text) => Value::from(text.as_str()),
    };
    Ok(Edit::set(&path, value))
}

fn settings_dto(values: &SettingsValues) -> SettingsDto {
    SettingsDto {
        automation_level: values.automation_level.clone(),
        wiki_index: values.wiki_index.clone(),
        ui: UiDto {
            theme: values.ui.theme.clone(),
            quick_start_dismissed: values.ui.quick_start_dismissed,
        },
    }
}
