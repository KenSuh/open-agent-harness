// 단일 배너: 배정된 에이전트의 설치·로그인·독립 규칙을 설계 §6.3 순서대로 보고 처음 맞는 하나만 고른다.
//! The one banner of the Team Workflow page (design §6.3; order from PROTOTYPE-INVENTORY §2.5). It reads the
//! effective config and the agent statuses the caller holds; an agent that was not checked yet is not reported as
//! missing but as one whose sign-in OAH cannot check.

use crate::config::{AgentId, Effective};
use crate::dto::{
    AgentIdDto, AgentStatusDto, BannerAgentDto, BannerDto, BannerPairDto, BannerSectionDto,
    LoginHintDto, SignInDto,
};
use crate::rules::body::agent_name;

/// Every broken `independent_of` rule as (section, other section, shared agent), in section order. An id that is
/// not a section, or that names the section itself, is skipped.
pub fn independence_breaks(effective: &Effective) -> Vec<(String, String, AgentId)> {
    independence_pairs(effective)
        .filter_map(|(section, other)| {
            let agent = effective.agent_for(section)?;
            (effective.agent_for(other) == Some(agent))
                .then(|| (section.to_string(), other.to_string(), agent))
        })
        .collect()
}

fn independence_pairs(effective: &Effective) -> impl Iterator<Item = (&str, &str)> {
    effective.sections.iter().flat_map(move |section| {
        section.independent_of.iter().filter_map(move |other| {
            (*other != section.id && effective.sections.iter().any(|s| &s.id == other))
                .then_some((section.id.as_str(), other.as_str()))
        })
    })
}

fn find(agents: &[AgentStatusDto], id: AgentId) -> Option<&AgentStatusDto> {
    agents.iter().find(|agent| agent.id == AgentIdDto(id))
}

fn label(effective: &Effective, id: &str) -> String {
    effective
        .sections
        .iter()
        .find(|section| section.id == id)
        .map_or_else(|| id.to_string(), |section| section.label.clone())
}

/// The assigned agents, each once, in section order.
fn assigned(effective: &Effective) -> Vec<AgentId> {
    let mut agents: Vec<AgentId> = Vec::new();
    for section in &effective.sections {
        if let Some(agent) = effective.agent_for(&section.id)
            && !agents.contains(&agent)
        {
            agents.push(agent);
        }
    }
    agents
}

fn banner_agent(agents: &[AgentStatusDto], id: AgentId) -> BannerAgentDto {
    let login_hint = find(agents, id).map_or_else(
        || LoginHintDto {
            text: String::new(),
            is_command: false,
        },
        |status| status.login_hint.clone(),
    );
    BannerAgentDto {
        id: AgentIdDto(id),
        name: agent_name(id).to_string(),
        login_hint,
    }
}

fn keeps_rules(effective: &Effective, missing: &[BannerSectionDto], candidate: AgentId) -> bool {
    let replaced = |id: &str| missing.iter().any(|section| section.id == id);
    independence_pairs(effective).all(|(section, other)| {
        match (replaced(section), replaced(other)) {
            (true, true) => false,
            (true, false) => effective.agent_for(other) != Some(candidate),
            (false, true) => effective.agent_for(section) != Some(candidate),
            (false, false) => true,
        }
    })
}

/// The banner, first match wins.
pub fn status(effective: &Effective, agents: &[AgentStatusDto]) -> BannerDto {
    let missing: Vec<BannerSectionDto> = effective
        .sections
        .iter()
        .filter_map(|section| {
            let agent = effective.agent_for(&section.id)?;
            find(agents, agent)
                .is_some_and(|status| !status.installed)
                .then(|| BannerSectionDto {
                    id: section.id.clone(),
                    label: section.label.clone(),
                    agent: AgentIdDto(agent),
                })
        })
        .collect();
    if !missing.is_empty() {
        let suggestion = AgentId::ALL.into_iter().find(|candidate| {
            candidate.can_own_section()
                && find(agents, *candidate)
                    .is_some_and(|status| status.installed && status.sign_in == SignInDto::SignedIn)
                && keeps_rules(effective, &missing, *candidate)
        });
        let named: Vec<String> = missing
            .iter()
            .map(|section| format!("{} ({})", section.label, agent_name(section.agent.0)))
            .collect();
        let mut message = format!("Not installed on this machine: {}.", named.join(", "));
        if let Some(candidate) = suggestion {
            message.push_str(&format!(" Use {} instead?", agent_name(candidate)));
        }
        return BannerDto::NotInstalled {
            message,
            sections: missing,
            suggestion: suggestion.map(AgentIdDto),
        };
    }
    let assigned = assigned(effective);
    let signed_out: Vec<AgentId> = assigned
        .iter()
        .copied()
        .filter(|id| find(agents, *id).is_some_and(|status| status.sign_in == SignInDto::SignedOut))
        .collect();
    if !signed_out.is_empty() {
        let named: Vec<String> = signed_out
            .iter()
            .map(|id| {
                let hint = banner_agent(agents, *id).login_hint.text;
                if hint.is_empty() {
                    agent_name(*id).to_string()
                } else {
                    format!("{} ({hint})", agent_name(*id))
                }
            })
            .collect();
        return BannerDto::NotSignedIn {
            message: format!("Not signed in: {}.", named.join(", ")),
            agents: signed_out
                .iter()
                .map(|id| banner_agent(agents, *id))
                .collect(),
        };
    }
    let unknown: Vec<AgentId> = assigned
        .iter()
        .copied()
        .filter(|id| find(agents, *id).is_none_or(|status| status.sign_in == SignInDto::Unknown))
        .collect();
    if !unknown.is_empty() {
        let named: Vec<&str> = unknown.iter().map(|id| agent_name(*id)).collect();
        return BannerDto::SignInUnknown {
            message: format!("OAH cannot check sign-in for {}.", named.join(", ")),
            agents: unknown.iter().map(|id| banner_agent(agents, *id)).collect(),
        };
    }
    let breaks = independence_breaks(effective);
    if !breaks.is_empty() {
        let lines: Vec<String> = breaks
            .iter()
            .map(|(section, other, agent)| {
                format!(
                    "{} is done by the same agent as {} ({})",
                    label(effective, section),
                    label(effective, other),
                    agent_name(*agent)
                )
            })
            .collect();
        return BannerDto::NotIndependent {
            message: format!("{}.", lines.join("; ")),
            pairs: breaks
                .into_iter()
                .map(|(section, other, agent)| BannerPairDto {
                    section,
                    other,
                    agent: AgentIdDto(agent),
                })
                .collect(),
        };
    }
    BannerDto::Ready {
        message: String::from("Ready"),
    }
}
