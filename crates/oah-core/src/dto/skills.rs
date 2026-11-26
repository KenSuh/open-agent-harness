use super::AgentIdDto;
use crate::fsx::skill_publish::Engine;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SkillScopeDto {
    Project,
    User,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct InstalledSkillDto {
    pub name: String,
    pub engine: AgentIdDto,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub compatible_agents: Vec<AgentIdDto>,
    pub scope: SkillScopeDto,
    pub location: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub managed: Option<ManagedSkillDto>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ManagedSkillDto {
    pub review_id: String,
    pub snapshot_id: String,
    pub repo: String,
    pub path: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstalledSkillTarget {
    pub project: Option<String>,
    pub location: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LinkedSkillSourceDto {
    pub repo: String,
    pub path: String,
    pub commit: String,
    pub updates_in_oah: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct InstalledSkillManagementDto {
    pub revision: String,
    pub location: String,
    pub agents: Vec<AgentIdDto>,
    pub source: Option<LinkedSkillSourceDto>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegisterSkillSourceReq {
    pub target: InstalledSkillTarget,
    pub revision: String,
    pub review_id: String,
    pub acknowledge_warnings: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoveInstalledSkillReq {
    pub target: InstalledSkillTarget,
    pub revision: String,
    pub confirm_shared: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RemovedSkillDto {
    pub archive: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApplyProjectSkillReq {
    pub project: String,
    pub engine: Engine,
    pub review_id: String,
    pub expected_review_id: Option<String>,
    pub acknowledge_warnings: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ApplyProjectSkillStatus {
    Installed,
    Updated,
    Unchanged,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ApplyProjectSkillDto {
    pub status: ApplyProjectSkillStatus,
    pub review_id: String,
}
