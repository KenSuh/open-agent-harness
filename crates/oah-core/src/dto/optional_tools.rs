use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
pub enum JevInput {
    Status,
    Prepare,
    Enable { token: String },
    Disable,
    KeyState,
    StoreKey { key: String },
    DeleteKey,
    PrepareTest,
    Test { token: String },
}

impl JevInput {
    pub(crate) fn action(&self) -> &'static str {
        match self {
            Self::Status => "status",
            Self::Prepare => "prepare",
            Self::Enable { .. } => "enable",
            Self::Disable => "disable",
            Self::KeyState => "key-state",
            Self::StoreKey { .. } => "store-key",
            Self::DeleteKey => "delete-key",
            Self::PrepareTest => "prepare-test",
            Self::Test { .. } => "test",
        }
    }

    pub(crate) fn may_change(&self) -> bool {
        matches!(
            self,
            Self::Enable { .. } | Self::Disable | Self::StoreKey { .. } | Self::DeleteKey
        )
    }
}

#[derive(Serialize)]
pub struct JevReply {
    pub project_id: String,
    pub action: &'static str,
    #[serde(flatten)]
    pub result: JevResult,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct JevResult {
    pub ok: bool,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub destination: Option<JevDestination>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub consent: Option<JevConsent>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scopes: Option<Vec<JevScope>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub token: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub key_state: Option<JevKeyState>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<JevKeySource>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub requests: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timeout_seconds: Option<f64>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct JevDestination {
    pub kind: JevDestinationKind,
    pub url: String,
    pub model: String,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum JevDestinationKind {
    Official,
    Custom,
    Refused,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum JevConsent {
    None,
    Enabled,
    Disabled,
    ConsentStale,
    DeviceState,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct JevScope {
    pub scope: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sends: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub labels: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub never: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bounds: Option<BTreeMap<String, JevBound>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub state: Option<JevScopeState>,
}

#[derive(Deserialize, Serialize)]
#[serde(untagged)]
pub enum JevBound {
    Text(String),
    Number(u32),
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum JevScopeState {
    Granted,
    ConsentStale,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct JevKeyState {
    pub environment: bool,
    pub device: JevDeviceKey,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum JevDeviceKey {
    Present,
    Missing,
    Locked,
    Denied,
    Unsupported,
    Error,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum JevKeySource {
    Device,
}

#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum WikiIndexInput {
    Status,
    Probe,
    Build,
    Search { query: String },
}

impl WikiIndexInput {
    pub(crate) fn action(&self) -> &'static str {
        match self {
            Self::Status => "status",
            Self::Probe => "probe",
            Self::Build => "build",
            Self::Search { .. } => "search",
        }
    }
}

#[derive(Clone, Serialize)]
pub struct WikiIndexReply {
    pub project_id: String,
    pub action: &'static str,
    #[serde(flatten)]
    pub result: WikiIndexResult,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WikiIndexResult {
    pub ok: bool,
    pub state: WikiIndexState,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<WikiIndexSource>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<WikiIndexModel>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mode: Option<WikiIndexMode>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub results: Option<Vec<WikiIndexHit>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub truncated: Option<bool>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WikiIndexState {
    Ready,
    Stale,
    NotBuilt,
    Incomplete,
    Unavailable,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WikiIndexSource {
    pub commit: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<u32>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WikiIndexModel {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dimensions: Option<u32>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WikiIndexMode {
    Semantic,
    Text,
    None,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WikiIndexHit {
    pub file: String,
    pub line: u32,
    pub title: String,
    pub heading: String,
    pub snippet: String,
}
