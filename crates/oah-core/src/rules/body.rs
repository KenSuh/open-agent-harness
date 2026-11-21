// 1.0 블록 본문: 실제 config 의 섹션 순서·배정·독립 규칙·스킬로 팀 작업 흐름 표와 문장을 블록 언어(en·ko)로 그린다.
//! The 1.0 body of the managed block (design §3.4, D-14, D-27). Rows follow `sections.order`; one independence
//! bullet per `independent_of` pair whose other section exists; the packet bullet only when `sections.plan.skills`
//! is not empty (the one section id named here, §3.4); `### Skills` only when a line follows it. The text is in the
//! block's language; section labels and skill names come from config in both languages.

use super::plan::{BlockBodies, Lang};
use crate::config::{AgentId, Effective};

/// The section id whose skills make it end with a work packet (§3.4, §6.2).
const PACKET_SECTION: &str = "plan";

/// An always-on project skill, as the project lock describes it (empty until `skills::lock` exists, D-14).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlwaysSkill {
    pub name: String,
    pub description: String,
    /// The skill's folder, project-relative with `/`.
    pub dir: String,
}

/// The skills the block names: always-on project skills and, per section, the names bound in config.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkillsForBlock {
    pub always: Vec<AlwaysSkill>,
    sections: Vec<(String, Vec<String>)>,
}

impl SkillsForBlock {
    /// Section names always come from `sections.<id>.skills`; `always` is the caller's lock-derived list.
    pub fn from_config(effective: &Effective, always: Vec<AlwaysSkill>) -> SkillsForBlock {
        let sections = effective
            .sections
            .iter()
            .map(|s| (s.id.clone(), s.skills.clone()))
            .collect();
        SkillsForBlock { always, sections }
    }

    /// The names bound to `section` (empty when none or unknown).
    pub fn section(&self, section: &str) -> &[String] {
        self.sections
            .iter()
            .find(|(id, _)| id == section)
            .map_or(&[], |(_, names)| names)
    }
}

/// The agent's product name, as the design's tables write it.
pub fn agent_name(agent: AgentId) -> &'static str {
    match agent {
        AgentId::Claude => "Claude Code",
        AgentId::Codex => "Codex",
        AgentId::Gemini => "Gemini CLI",
        AgentId::Copilot => "Copilot CLI",
        AgentId::Opencode => "OpenCode",
        AgentId::Antigravity => "Antigravity",
        AgentId::Cursor => "Cursor",
    }
}

struct Texts {
    heading: &'static str,
    header: &'static str,
    unassigned: &'static str,
    listed: &'static str,
    independent: fn(&str, &str) -> String,
    packet: fn(&str) -> String,
    skills: &'static str,
    always: &'static str,
}

const EN: Texts = Texts {
    heading: "## Team workflow (managed by OAH)",
    header: "| Section | Agent |",
    unassigned: "Not assigned",
    listed: "- Do the section your agent is listed for. If asked to do another section, say which agent is listed for it.",
    independent: |a, b| format!("- {a} is done by a different agent than {b}."),
    packet: |p| format!("- {p} ends with a written work packet. Use the skills listed for {p}."),
    skills: "### Skills",
    always: "Always",
};

const KO: Texts = Texts {
    heading: "## 팀 작업 흐름 (OAH 관리)",
    header: "| 단계 | 에이전트 |",
    unassigned: "배정 없음",
    listed: "- 자기 에이전트가 맡은 단계만 한다. 다른 단계를 요청받으면 그 단계를 맡은 에이전트를 알려 준다.",
    independent: |a, b| format!("- {a} 단계는 {b} 단계와 다른 에이전트가 맡는다."),
    packet: |p| format!("- {p} 단계는 작성된 작업 패킷으로 끝난다. {p} 단계에 등록된 스킬을 쓴다."),
    skills: "### 스킬",
    always: "항상",
};

/// Collapses every run of whitespace, line ends included, to one space.
fn one_line(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn cell(text: &str) -> String {
    one_line(text).replace('|', "\\|")
}

/// The body in `lang`, ending with one LF.
pub fn render_body(effective: &Effective, skills: &SkillsForBlock, lang: Lang) -> String {
    let texts = match lang {
        Lang::En => &EN,
        Lang::Ko => &KO,
    };
    let label = |id: &str| {
        effective
            .sections
            .iter()
            .find(|s| s.id == id)
            .map(|s| one_line(&s.label))
    };
    let mut lines = vec![
        texts.heading.to_string(),
        String::new(),
        texts.header.to_string(),
        "|---|---|".to_string(),
    ];
    for section in &effective.sections {
        let agent = effective
            .agent_for(&section.id)
            .map_or(texts.unassigned, agent_name);
        lines.push(format!("| {} | {agent} |", cell(&section.label)));
    }
    lines.extend([String::new(), texts.listed.to_string()]);
    for section in &effective.sections {
        for other in section
            .independent_of
            .iter()
            .filter(|other| **other != section.id)
        {
            if let Some(other) = label(other) {
                lines.push((texts.independent)(&one_line(&section.label), &other));
            }
        }
    }
    if let Some(plan) = label(PACKET_SECTION).filter(|_| !skills.section(PACKET_SECTION).is_empty())
    {
        lines.push((texts.packet)(&plan));
    }
    let mut skill_lines: Vec<String> = skills
        .always
        .iter()
        .map(|s| {
            format!(
                "- {}: `{}`: {} (`{}/SKILL.md`)",
                texts.always,
                one_line(&s.name),
                one_line(&s.description),
                one_line(&s.dir)
            )
        })
        .collect();
    for section in &effective.sections {
        let names = skills.section(&section.id);
        if !names.is_empty() {
            let names: Vec<String> = names.iter().map(|n| format!("`{}`", one_line(n))).collect();
            skill_lines.push(format!(
                "- {}: {}",
                one_line(&section.label),
                names.join(", ")
            ));
        }
    }
    if !skill_lines.is_empty() {
        lines.extend([String::new(), texts.skills.to_string()]);
        lines.extend(skill_lines);
    }
    lines.push(String::new());
    lines.join("\n")
}

/// The body in both languages, for [`super::plan`], which picks the block's language.
pub fn render_bodies(effective: &Effective, skills: &SkillsForBlock) -> BlockBodies {
    BlockBodies {
        en: render_body(effective, skills, Lang::En),
        ko: render_body(effective, skills, Lang::Ko),
    }
}
