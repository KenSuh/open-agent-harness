use super::AgentIdDto;
use serde::Serialize;

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
    pub scope: SkillScopeDto,
    pub location: String,
}
