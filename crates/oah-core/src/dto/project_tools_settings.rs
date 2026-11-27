use crate::config::{ConfigError, ProjectTools, ToolsChange, Value};
use crate::dto::NoticeDto;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProjectToolsPlanReq {
    pub edits: Vec<ToolsChange>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProjectToolsApplyReq {
    pub edits: Vec<ToolsChange>,
    pub plan_id: String,
    pub approve_upgrade: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum ToolSettingValue {
    Text(String),
    Flag(bool),
    List(Vec<String>),
}

impl TryFrom<&Value> for ToolSettingValue {
    type Error = ConfigError;

    fn try_from(value: &Value) -> Result<Self, Self::Error> {
        fn unsupported() -> ConfigError {
            ConfigError::invalid("the settings preview contains an unsupported value")
        }
        match value {
            Value::String(value) => Ok(Self::Text(value.clone())),
            Value::Boolean(value) => Ok(Self::Flag(*value)),
            Value::Array(values) => values
                .iter()
                .map(|entry| match entry {
                    Value::String(value) => Ok(value.clone()),
                    _ => Err(unsupported()),
                })
                .collect::<Result<Vec<_>, _>>()
                .map(Self::List),
            _ => Err(unsupported()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ToolsKeyChangeDto {
    pub key: String,
    pub before: Option<ToolSettingValue>,
    pub after: Option<ToolSettingValue>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ToolsKeptDto {
    pub key: &'static str,
    pub value: ToolSettingValue,
    pub reason: &'static str,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ToolsUpgradeDto {
    pub message: &'static str,
    pub fingerprint: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ToolsBackupStateDto {
    Write,
    Reuse,
    Conflict,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ToolsBackupDto {
    pub state: ToolsBackupStateDto,
    pub path: &'static str,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProjectToolsPlanDto {
    pub project_id: String,
    pub plan_id: String,
    pub config_path: &'static str,
    pub settings: ProjectTools,
    pub optional: Vec<&'static str>,
    pub changes: Vec<ToolsKeyChangeDto>,
    pub kept: Vec<ToolsKeptDto>,
    pub upgrade: Option<ToolsUpgradeDto>,
    pub backup: Option<ToolsBackupDto>,
    pub notices: Vec<NoticeDto>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProjectToolsApplyDto {
    pub project_id: String,
    pub plan_id: String,
    pub saved: bool,
    pub upgraded: bool,
    pub backup: Option<&'static str>,
}
