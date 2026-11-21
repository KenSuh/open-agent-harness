// 프리셋: 여러 섹션을 한 번에 배정하는 설계 데이터이며, 설치되지 않은 에이전트를 부르면 이유와 함께 막는다.
//! Presets (design §6.1, PROTOTYPE-INVENTORY §3.4; the prototype's `cursor-companion` preset is dropped). A preset
//! sets only the sections that exist in `sections.order`; one that names an agent not installed here (or not
//! checked yet) is disabled with the reason.

use crate::config::{AgentId, Effective};
use crate::dto::{AgentIdDto, AgentStatusDto, PresetDto, PresetStageDto};
use crate::rules::body::agent_name;

/// A preset: an id, a label and the agent it gives each section id it names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Preset {
    pub id: &'static str,
    pub label: &'static str,
    pub stages: &'static [(&'static str, AgentId)],
}

/// The presets of §6.1.
pub const PRESETS: [Preset; 2] = [
    Preset {
        id: "balanced",
        label: "Balanced",
        stages: &[
            ("plan", AgentId::Claude),
            ("build", AgentId::Claude),
            ("review", AgentId::Codex),
            ("fix", AgentId::Codex),
        ],
    },
    Preset {
        id: "research-build",
        label: "Research and build",
        stages: &[
            ("plan", AgentId::Gemini),
            ("build", AgentId::Claude),
            ("review", AgentId::Codex),
            ("fix", AgentId::Codex),
        ],
    },
];

impl Preset {
    /// The (section, agent) pairs for the sections that exist.
    pub fn stages_for(&self, effective: &Effective) -> Vec<(String, AgentId)> {
        self.stages
            .iter()
            .filter(|(id, _)| effective.sections.iter().any(|section| section.id == *id))
            .map(|(id, agent)| (id.to_string(), *agent))
            .collect()
    }

    /// Why the preset cannot be applied here, or `None`.
    pub fn disabled_reason(
        &self,
        effective: &Effective,
        agents: &[AgentStatusDto],
    ) -> Option<String> {
        let stages = self.stages_for(effective);
        if stages.is_empty() {
            return Some(String::from("none of its sections is in sections.order"));
        }
        let mut reasons: Vec<String> = Vec::new();
        for (_, agent) in stages {
            let status = agents.iter().find(|status| status.id == AgentIdDto(agent));
            let reason = match status {
                Some(status) if status.installed => continue,
                Some(_) => format!("{} is not installed", agent_name(agent)),
                None => format!("{} has not been checked yet", agent_name(agent)),
            };
            if !reasons.contains(&reason) {
                reasons.push(reason);
            }
        }
        (!reasons.is_empty()).then(|| reasons.join("; "))
    }

    pub fn to_dto(&self, effective: &Effective, agents: &[AgentStatusDto]) -> PresetDto {
        PresetDto {
            id: self.id.to_string(),
            label: self.label.to_string(),
            stages: self
                .stages_for(effective)
                .into_iter()
                .map(|(section, agent)| PresetStageDto {
                    section,
                    agent: AgentIdDto(agent),
                })
                .collect(),
            disabled_reason: self.disabled_reason(effective, agents),
        }
    }
}
