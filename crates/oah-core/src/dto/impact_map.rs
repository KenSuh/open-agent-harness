use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ImpactMapAction {
    Status,
    Refresh,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImpactMapRunReq {
    pub action: ImpactMapAction,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ImpactGraphState {
    Absent,
    Available,
    Stale,
    Unproven,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ImpactMapRunDto {
    pub project_id: String,
    pub action: ImpactMapAction,
    pub changed: bool,
    pub graph: Option<ImpactGraphState>,
    pub refusal: Option<String>,
}
