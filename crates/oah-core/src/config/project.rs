// 프로젝트 1.0 유효 설정: 섹션·단계 배정·에이전트·모델·스킬을 §3.1 규칙과 v0.4.0 파일 읽기 표대로 해석한다.
use super::error::{ConfigError, Notice, codes, notice_codes};
use super::file::{ConfigFile, Merger, not_a_table, type_error};
use super::save::UpgradeWarning;
use super::tools::ProjectTools;
use super::value::{Table, Value};
use std::fmt;
use std::path::Path;

/// The seven agents OAH knows, in display order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum AgentId {
    Claude,
    Codex,
    Gemini,
    Copilot,
    Opencode,
    Antigravity,
    Cursor,
}

impl AgentId {
    pub const ALL: [AgentId; 7] = [
        AgentId::Claude,
        AgentId::Codex,
        AgentId::Gemini,
        AgentId::Copilot,
        AgentId::Opencode,
        AgentId::Antigravity,
        AgentId::Cursor,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            AgentId::Claude => "claude",
            AgentId::Codex => "codex",
            AgentId::Gemini => "gemini",
            AgentId::Copilot => "copilot",
            AgentId::Opencode => "opencode",
            AgentId::Antigravity => "antigravity",
            AgentId::Cursor => "cursor",
        }
    }

    pub fn parse(text: &str) -> Option<AgentId> {
        AgentId::ALL.into_iter().find(|id| id.as_str() == text)
    }

    /// Whether the agent can own a section. Cursor receives skills and rules only.
    pub fn can_own_section(self) -> bool {
        self != AgentId::Cursor
    }
}

impl fmt::Display for AgentId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Where a section's difficulty comes from (§7.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SectionKind {
    Design,
    Implementation,
    Review,
}

impl SectionKind {
    pub fn as_str(self) -> &'static str {
        match self {
            SectionKind::Design => "design",
            SectionKind::Implementation => "implementation",
            SectionKind::Review => "review",
        }
    }

    fn parse(text: &str) -> Option<SectionKind> {
        [
            SectionKind::Design,
            SectionKind::Implementation,
            SectionKind::Review,
        ]
        .into_iter()
        .find(|kind| kind.as_str() == text)
    }
}

/// A section's fixed difficulty, or none (taken from its kind).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Difficulty {
    FromKind,
    Low,
    Medium,
    High,
    Max,
}

impl Difficulty {
    pub fn as_str(self) -> &'static str {
        match self {
            Difficulty::FromKind => "",
            Difficulty::Low => "low",
            Difficulty::Medium => "medium",
            Difficulty::High => "high",
            Difficulty::Max => "max",
        }
    }

    fn parse(text: &str) -> Option<Difficulty> {
        [
            Difficulty::FromKind,
            Difficulty::Low,
            Difficulty::Medium,
            Difficulty::High,
            Difficulty::Max,
        ]
        .into_iter()
        .find(|difficulty| difficulty.as_str() == text)
    }
}

/// One section of the workflow.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Section {
    pub id: String,
    pub label: String,
    pub kind: SectionKind,
    pub difficulty: Difficulty,
    pub skills: Vec<String>,
    pub independent_of: Vec<String>,
}

/// The four built-in sections, in their default order.
pub const BUILTIN_SECTIONS: [&str; 4] = ["plan", "build", "review", "fix"];

/// The agent a built-in section has when the file does not assign one.
pub fn default_agent(section: &str) -> Option<AgentId> {
    match section {
        "plan" | "build" => Some(AgentId::Claude),
        "review" | "fix" => Some(AgentId::Codex),
        _ => None,
    }
}

fn builtin_section(id: &str) -> Option<(&'static str, SectionKind, &'static [&'static str])> {
    match id {
        "plan" => Some(("Plan & Clarify", SectionKind::Design, &[])),
        "build" => Some(("Write Code", SectionKind::Implementation, &[])),
        "review" => Some(("Code Review", SectionKind::Review, &["build"])),
        "fix" => Some(("Fix Issues", SectionKind::Implementation, &[])),
        _ => None,
    }
}

/// Whether `id` is a section id: `[a-z][a-z0-9-]{0,31}`.
pub fn is_section_id(id: &str) -> bool {
    let bytes = id.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 32
        && bytes[0].is_ascii_lowercase()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

/// Which `[stages]` layout the file uses.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StageFormat {
    /// Section ids as keys (1.0).
    New,
    /// v0.4.0's `initial` / `review` / `correction`, read through the legacy table of §3.1.
    Legacy,
}

/// Where an assignment came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AssignmentSource {
    /// The section's own `[stages]` key.
    File,
    /// A v0.4.0 key: `initial` for plan and build, `correction` for fix.
    Legacy(&'static str),
    /// A built-in section's default.
    Default,
}

/// A section and the agent that does it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Assignment {
    pub section: String,
    pub agent: AgentId,
    pub source: AssignmentSource,
}

/// Where an agent's settings were read from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentSource {
    /// No table in the file.
    Defaults,
    /// `[agents.<id>]`.
    Agents,
    /// v0.4.0's `[engines.<id>]`, read because `[agents.<id>]` is absent.
    Engines,
}

/// The model tiers, in order.
pub const TIERS: [&str; 4] = ["low", "medium", "high", "max"];

/// One tier's model and effort.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TierConfig {
    pub tier: &'static str,
    /// Empty: chosen by the model rule.
    pub model: String,
    pub effort: String,
}

/// One agent's settings.
#[derive(Debug, Clone, PartialEq)]
pub struct AgentConfig {
    pub id: AgentId,
    pub source: AgentSource,
    pub command: Vec<String>,
    pub quality_order: Vec<String>,
    pub allowed_models: Vec<String>,
    pub effort_caps: Vec<(String, String)>,
    pub tier_pools: Vec<(String, Vec<String>)>,
    pub tiers: Vec<TierConfig>,
    /// The keys the file gives for this agent that 1.0 uses, as written.
    pub given: Table,
    /// Dotted keys (relative to the agent's table) kept in the file and not used: v0.4.0's `permission_mode`,
    /// `tools` and `tiers.<tier>.timeout_min` of an `[engines.<id>]` table. Sorted.
    pub unused_kept_in_file: Vec<String>,
}

/// `[models]`.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelsConfig {
    pub profile: String,
    pub fetch_scores: bool,
    pub ttl_hours: f64,
}

/// `[skills]`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkillsConfig {
    pub update_interval_days: i64,
    /// Empty: the 1.0 default list.
    pub license_allow: Vec<String>,
}

/// The project configuration as 1.0 uses it.
#[derive(Debug, Clone, PartialEq)]
pub struct Effective {
    pub sections: Vec<Section>,
    /// Every assigned section, in section order. A custom section without a `[stages]` key is unassigned.
    pub stages: Vec<Assignment>,
    pub stage_format: StageFormat,
    /// All seven agents, in [`AgentId::ALL`] order.
    pub agents: Vec<AgentConfig>,
    pub models: ModelsConfig,
    pub skills: SkillsConfig,
}

impl Effective {
    /// The agent assigned to `section`, if any.
    pub fn agent_for(&self, section: &str) -> Option<AgentId> {
        self.stages
            .iter()
            .find(|assignment| assignment.section == section)
            .map(|assignment| assignment.agent)
    }

    pub fn agent(&self, id: AgentId) -> &AgentConfig {
        self.agents
            .iter()
            .find(|agent| agent.id == id)
            .expect("every agent is present")
    }
}

/// The file's generation, which decides what a save must do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    /// No file yet: a save creates it, with no upgrade and no backup.
    Missing,
    /// A file without any 1.0 key (`[stages]` plan, build or fix; `[sections]`; `[agents]`). Its first save is
    /// the one-way upgrade, which needs approval.
    Legacy,
    /// A 1.0 file.
    Current,
}

/// `.oah/config.toml` as read and interpreted. Reading never changes the file.
#[derive(Debug, Clone)]
pub struct ProjectConfig {
    file: ConfigFile,
    format: Format,
    effective: Effective,
    notices: Vec<Notice>,
}

impl ProjectConfig {
    /// Reads and interprets `path` (normally `<root>/.oah/config.toml`); a missing file is the defaults.
    pub fn read(path: &Path) -> Result<ProjectConfig, ConfigError> {
        ProjectConfig::from_file(ConfigFile::read(path)?)
    }

    /// Interprets an already read file.
    pub fn from_file(file: ConfigFile) -> Result<ProjectConfig, ConfigError> {
        let (effective, notices) = interpret(&file)?;
        let format = classify(&file);
        Ok(ProjectConfig {
            file,
            format,
            effective,
            notices,
        })
    }

    pub fn file(&self) -> &ConfigFile {
        &self.file
    }

    pub fn format(&self) -> Format {
        self.format
    }

    pub fn effective(&self) -> &Effective {
        &self.effective
    }

    pub fn notices(&self) -> &[Notice] {
        &self.notices
    }

    /// The Project Wiki, Impact Map and Jev settings of the same file. They are read on request and are not part
    /// of [`Effective`], so a broken tools table never stops the sections and stages from being read.
    pub fn tools(&self) -> Result<ProjectTools, ConfigError> {
        ProjectTools::read(&self.file)
    }

    /// For a v0.4.0 file, the warning the user must see and approve before the first save.
    pub fn upgrade_warning(&self) -> Option<UpgradeWarning> {
        match (self.format, self.file.source_bytes()) {
            (Format::Legacy, Some(bytes)) => Some(UpgradeWarning::for_bytes(bytes)),
            _ => None,
        }
    }
}

const NEW_STAGE_KEYS: [&str; 3] = ["plan", "build", "fix"];
const LEGACY_STAGE_KEYS: [&str; 2] = ["initial", "correction"];

pub(crate) fn classify(file: &ConfigFile) -> Format {
    if !file.exists() {
        return Format::Missing;
    }
    let values = file.values();
    let new_stage_key = values
        .get("stages")
        .and_then(Value::as_table)
        .is_some_and(|stages| NEW_STAGE_KEYS.iter().any(|key| stages.contains_key(key)));
    if new_stage_key || values.contains_key("sections") || values.contains_key("agents") {
        Format::Current
    } else {
        Format::Legacy
    }
}

pub(crate) fn interpret(file: &ConfigFile) -> Result<(Effective, Vec<Notice>), ConfigError> {
    let where_ = file.where_();
    let values = file.values();
    let mut notices = Vec::new();
    let sections = read_sections(where_, values, &mut notices)?;
    let (stages, stage_format) = read_stages(where_, values, &sections, &mut notices)?;
    let agents = read_agents(where_, values, &mut notices)?;
    let models = read_models(where_, values, &mut notices)?;
    let skills = read_skills(where_, values, &mut notices)?;
    Ok((
        Effective {
            sections,
            stages,
            stage_format,
            agents,
            models,
            skills,
        },
        notices,
    ))
}

fn top_table<'a>(
    where_: &str,
    values: &'a Table,
    name: &str,
) -> Result<Option<&'a Table>, ConfigError> {
    match values.get(name) {
        None => Ok(None),
        Some(Value::Table(table)) => Ok(Some(table)),
        Some(other) => Err(not_a_table(where_, name, other)),
    }
}

fn string_list(
    where_: &str,
    name: &str,
    value: &Value,
    what: &str,
) -> Result<Vec<String>, ConfigError> {
    let invalid = || ConfigError::invalid(format!("{where_}: {name} must list {what} as strings"));
    let items = value.as_array().ok_or_else(invalid)?;
    items
        .iter()
        .map(|item| item.as_str().map(str::to_string).ok_or_else(invalid))
        .collect()
}

fn read_sections(
    where_: &str,
    values: &Table,
    notices: &mut Vec<Notice>,
) -> Result<Vec<Section>, ConfigError> {
    let table = top_table(where_, values, "sections")?;
    let empty = Table::new();
    let table = table.unwrap_or(&empty);
    let order: Vec<String> = match table.get("order") {
        None => BUILTIN_SECTIONS.iter().map(|id| id.to_string()).collect(),
        Some(value @ Value::Array(_)) => {
            let ids = string_list(where_, "sections.order", value, "section ids")?;
            let mut seen: Vec<&str> = Vec::new();
            for id in &ids {
                if !is_section_id(id) {
                    return Err(ConfigError::invalid(format!(
                        "{where_}: sections.order has {}, which is not a section id; use a lowercase letter \
                         followed by up to 31 lowercase letters, digits or hyphens",
                        Value::from(id.as_str())
                    )));
                }
                if seen.contains(&id.as_str()) {
                    return Err(ConfigError::invalid(format!(
                        "{where_}: sections.order lists {} twice; list each section once",
                        Value::from(id.as_str())
                    )));
                }
                seen.push(id);
            }
            ids
        }
        Some(other) => {
            return Err(type_error(
                where_,
                "sections.order",
                &Value::Array(Vec::new()),
                other,
            ));
        }
    };
    for (key, value) in table.iter() {
        if key == "order" {
            continue;
        }
        let name = format!("sections.{key}");
        if value.as_table().is_none() {
            notices.push(Notice::new(
                notice_codes::UNKNOWN_KEY,
                format!(
                    "{where_}: unknown key {name}; remove it or use one of: order, [sections.<id>] tables"
                ),
                vec![name],
            ));
        } else if !order.iter().any(|id| id == key) {
            notices.push(Notice::new(
                notice_codes::SECTION_UNLISTED,
                format!(
                    "{where_}: [{name}] is kept but not used, because {key} is not in sections.order; add it \
                     to sections.order or remove the table"
                ),
                vec![name],
            ));
        }
    }
    let mut sections = Vec::with_capacity(order.len());
    for id in order {
        let (label, kind, independent_of) = match builtin_section(&id) {
            Some((label, kind, independent)) => (
                label.to_string(),
                kind,
                independent
                    .iter()
                    .map(|other| Value::from(*other))
                    .collect(),
            ),
            None => (id.clone(), SectionKind::Implementation, Vec::new()),
        };
        let mut defaults = Table::new();
        defaults.insert("label", Value::from(label));
        defaults.insert("kind", Value::from(kind.as_str()));
        defaults.insert("difficulty", Value::from(""));
        defaults.insert("skills", Value::Array(Vec::new()));
        defaults.insert("independent_of", Value::Array(independent_of));
        let dotted = format!("sections.{id}");
        let merged = match table.get(&id) {
            Some(Value::Table(given)) => {
                let mut merger = Merger::new(where_);
                let (merged, _) = merger.merge(&defaults, given, &dotted)?;
                notices.extend(merger.notices);
                merged
            }
            _ => defaults,
        };
        let text = |key: &str| {
            merged
                .get(key)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string()
        };
        let kind_text = text("kind");
        let kind = SectionKind::parse(&kind_text).ok_or_else(|| {
            ConfigError::invalid(format!(
                "{where_}: {dotted}.kind = {}; use one of: design, implementation, review",
                Value::from(kind_text.as_str())
            ))
        })?;
        let difficulty_text = text("difficulty");
        let difficulty = Difficulty::parse(&difficulty_text).ok_or_else(|| {
            ConfigError::invalid(format!(
                "{where_}: {dotted}.difficulty = {}; use \"\" to take it from the kind, or one of: low, \
                 medium, high, max",
                Value::from(difficulty_text.as_str())
            ))
        })?;
        let skills = string_list(
            where_,
            &format!("{dotted}.skills"),
            merged.get("skills").unwrap_or(&Value::Array(Vec::new())),
            "skill names",
        )?;
        let independent_of = string_list(
            where_,
            &format!("{dotted}.independent_of"),
            merged
                .get("independent_of")
                .unwrap_or(&Value::Array(Vec::new())),
            "section ids",
        )?;
        if let Some(bad) = independent_of.iter().find(|other| !is_section_id(other)) {
            return Err(ConfigError::invalid(format!(
                "{where_}: {dotted}.independent_of has {}, which is not a section id",
                Value::from(bad.as_str())
            )));
        }
        sections.push(Section {
            label: text("label"),
            id,
            kind,
            difficulty,
            skills,
            independent_of,
        });
    }
    Ok(sections)
}

fn owner_list() -> String {
    AgentId::ALL
        .iter()
        .filter(|id| id.can_own_section())
        .map(|id| id.as_str())
        .collect::<Vec<_>>()
        .join(", ")
}

fn stage_agent(where_: &str, key: &str, value: &Value) -> Result<AgentId, ConfigError> {
    let name = format!("stages.{key}");
    let Value::String(text) = value else {
        return Err(type_error(where_, &name, &Value::from(""), value));
    };
    match AgentId::parse(text) {
        Some(AgentId::Cursor) => Err(ConfigError::new(
            codes::AGENT_DEPLOY_ONLY,
            format!(
                "{where_}: {name} = \"cursor\"; Cursor receives skills and rules but cannot own a section; use \
                 one of: {}",
                owner_list()
            ),
        )),
        Some(id) => Ok(id),
        None => Err(ConfigError::invalid(format!(
            "{where_}: {name} = {}; use one of: {}",
            value,
            owner_list()
        ))),
    }
}

fn read_stages(
    where_: &str,
    values: &Table,
    sections: &[Section],
    notices: &mut Vec<Notice>,
) -> Result<(Vec<Assignment>, StageFormat), ConfigError> {
    let empty = Table::new();
    let table = top_table(where_, values, "stages")?.unwrap_or(&empty);
    let has_new = NEW_STAGE_KEYS.iter().any(|key| table.contains_key(key));
    let has_legacy = LEGACY_STAGE_KEYS.iter().any(|key| table.contains_key(key));
    let format = if has_legacy && !has_new {
        StageFormat::Legacy
    } else {
        StageFormat::New
    };
    let listed = |id: &str| sections.iter().any(|section| section.id == id);
    let mut chosen: Vec<(String, AgentId, AssignmentSource)> = Vec::new();
    let mut set = |section: &str, agent: AgentId, source: AssignmentSource| {
        if listed(section) {
            chosen.retain(|(known, _, _)| known != section);
            chosen.push((section.to_string(), agent, source));
        }
    };
    let mut ignored = Vec::new();
    let mut unlisted = Vec::new();
    for (key, value) in table.iter() {
        match (format, key) {
            (StageFormat::Legacy, "initial") => {
                let agent = stage_agent(where_, key, value)?;
                set("plan", agent, AssignmentSource::Legacy("initial"));
                set("build", agent, AssignmentSource::Legacy("initial"));
            }
            (StageFormat::Legacy, "correction") => {
                let agent = stage_agent(where_, key, value)?;
                set("fix", agent, AssignmentSource::Legacy("correction"));
            }
            (StageFormat::New, "initial" | "correction") if !listed(key) => {
                ignored.push(format!("stages.{key}"));
            }
            _ if listed(key) => {
                let agent = stage_agent(where_, key, value)?;
                set(key, agent, AssignmentSource::File);
            }
            _ => unlisted.push(format!("stages.{key}")),
        }
    }
    if !ignored.is_empty() {
        notices.push(Notice::new(
            notice_codes::STAGES_LEGACY_IGNORED,
            format!(
                "{where_}: {} {} v0.4.0 {} and {} ignored, because the file also has 1.0 stage keys (plan, \
                 build, fix); remove {}",
                ignored.join(", "),
                if ignored.len() == 1 { "is a" } else { "are" },
                if ignored.len() == 1 { "key" } else { "keys" },
                if ignored.len() == 1 { "is" } else { "are" },
                if ignored.len() == 1 { "it" } else { "them" },
            ),
            ignored,
        ));
    }
    for name in unlisted {
        let id = name.trim_start_matches("stages.");
        notices.push(Notice::new(
            notice_codes::SECTION_UNLISTED,
            format!(
                "{where_}: {name} is kept but not used, because {id} is not in sections.order; add it to \
                 sections.order or remove the key"
            ),
            vec![name.clone()],
        ));
    }
    let mut stages = Vec::new();
    for section in sections {
        if let Some((_, agent, source)) = chosen.iter().find(|(id, _, _)| *id == section.id) {
            stages.push(Assignment {
                section: section.id.clone(),
                agent: *agent,
                source: *source,
            });
        } else if let Some(agent) = default_agent(&section.id) {
            stages.push(Assignment {
                section: section.id.clone(),
                agent,
                source: AssignmentSource::Default,
            });
        }
    }
    Ok((stages, format))
}

/// The 1.0 schema of `[agents.<id>]` with its defaults.
pub fn agent_defaults(id: AgentId) -> Table {
    let mut tiers = Table::new();
    for tier in TIERS {
        let effort = if id == AgentId::Codex && tier == "max" {
            "highest"
        } else {
            tier
        };
        let mut entry = Table::new();
        entry.insert("model", Value::from(""));
        entry.insert("effort", Value::from(effort));
        tiers.insert(tier, Value::Table(entry));
    }
    let mut table = Table::new();
    table.insert("command", Value::Array(vec![Value::from(id.as_str())]));
    table.insert("quality_order", Value::Array(Vec::new()));
    table.insert("allowed_models", Value::Array(Vec::new()));
    table.insert("effort_caps", Value::Table(Table::new()));
    table.insert("tier_pools", Value::Table(Table::new()));
    table.insert("tiers", Value::Table(tiers));
    table
}

/// v0.4.0 engine keys that 1.0 keeps in an `[engines.<id>]` table without using them.
fn kept_unused_engine_key(relative: &[String]) -> bool {
    match relative {
        [key] => key == "permission_mode" || key == "tools",
        [tiers, tier, key] => {
            tiers == "tiers" && TIERS.contains(&tier.as_str()) && key == "timeout_min"
        }
        _ => false,
    }
}

fn read_agents(
    where_: &str,
    values: &Table,
    notices: &mut Vec<Notice>,
) -> Result<Vec<AgentConfig>, ConfigError> {
    let empty = Table::new();
    let agents = top_table(where_, values, "agents")?.unwrap_or(&empty);
    let engines = top_table(where_, values, "engines")?.unwrap_or(&empty);
    let mut known: Vec<&str> = AgentId::ALL.iter().map(|id| id.as_str()).collect();
    known.sort_unstable();
    for (scope, table) in [("agents", agents), ("engines", engines)] {
        for key in table.keys() {
            if AgentId::parse(key).is_none() {
                let name = format!("{scope}.{key}");
                notices.push(Notice::new(
                    notice_codes::UNKNOWN_KEY,
                    format!(
                        "{where_}: unknown key {name}; remove it or use one of: {}",
                        known.join(", ")
                    ),
                    vec![name],
                ));
            }
        }
    }
    let mut out = Vec::with_capacity(AgentId::ALL.len());
    for id in AgentId::ALL {
        let defaults = agent_defaults(id);
        let (source, dotted, given) = match (agents.get(id.as_str()), engines.get(id.as_str())) {
            (Some(value), _) => (AgentSource::Agents, format!("agents.{id}"), Some(value)),
            (None, Some(value)) => (AgentSource::Engines, format!("engines.{id}"), Some(value)),
            (None, None) => (AgentSource::Defaults, format!("agents.{id}"), None),
        };
        let (merged, used, mut unused) = match given {
            None => (defaults, Table::new(), Vec::new()),
            Some(Value::Table(table)) => {
                let keep = kept_unused_engine_key;
                let mut merger = if source == AgentSource::Engines {
                    Merger::keeping_unused(where_, &keep)
                } else {
                    Merger::new(where_)
                };
                let (merged, used) = merger.merge(&defaults, table, &dotted)?;
                notices.extend(std::mem::take(&mut merger.notices));
                (merged, used, std::mem::take(&mut merger.unused))
            }
            Some(other) => return Err(not_a_table(where_, &dotted, other)),
        };
        unused.sort();
        out.push(agent_config(
            where_, id, source, &dotted, &merged, used, unused,
        )?);
    }
    Ok(out)
}

fn agent_config(
    where_: &str,
    id: AgentId,
    source: AgentSource,
    dotted: &str,
    merged: &Table,
    given: Table,
    unused_kept_in_file: Vec<String>,
) -> Result<AgentConfig, ConfigError> {
    let list = |key: &str, what: &str| {
        string_list(
            where_,
            &format!("{dotted}.{key}"),
            merged.get(key).unwrap_or(&Value::Array(Vec::new())),
            what,
        )
    };
    let command = list("command", "the command and its arguments")?;
    if command.is_empty() || command[0].is_empty() {
        return Err(ConfigError::invalid(format!(
            "{where_}: {dotted}.command must name a program, such as [\"{id}\"]"
        )));
    }
    let quality_order = list("quality_order", "model names")?;
    let allowed_models = list("allowed_models", "model names")?;
    if allowed_models.iter().any(String::is_empty) {
        return Err(ConfigError::invalid(format!(
            "{where_}: {dotted}.allowed_models must list model names as strings, or be empty to allow every \
             model the CLI lists"
        )));
    }
    let empty = Table::new();
    let mut effort_caps = Vec::new();
    for (model, cap) in merged
        .get("effort_caps")
        .and_then(Value::as_table)
        .unwrap_or(&empty)
        .iter()
    {
        let cap = cap.as_str().ok_or_else(|| {
            ConfigError::invalid(format!(
                "{where_}: {dotted}.effort_caps.{model} must be an effort level as a string"
            ))
        })?;
        effort_caps.push((model.to_string(), cap.to_string()));
    }
    let mut tier_pools = Vec::new();
    for (tier, names) in merged
        .get("tier_pools")
        .and_then(Value::as_table)
        .unwrap_or(&empty)
        .iter()
    {
        if !TIERS.contains(&tier) {
            return Err(ConfigError::invalid(format!(
                "{where_}: {dotted}.tier_pools.{tier} is not a tier; use one of: {}",
                TIERS.join(", ")
            )));
        }
        let invalid = || {
            ConfigError::invalid(format!(
                "{where_}: {dotted}.tier_pools.{tier} must list model names as strings, such as {tier} = \
                 [\"<model>\"], or be empty"
            ))
        };
        let names = names.as_array().ok_or_else(invalid)?;
        let names = names
            .iter()
            .map(|name| match name.as_str() {
                Some(text) if !text.is_empty() => Ok(text.to_string()),
                _ => Err(invalid()),
            })
            .collect::<Result<Vec<_>, _>>()?;
        tier_pools.push((tier.to_string(), names));
    }
    let tiers_table = merged
        .get("tiers")
        .and_then(Value::as_table)
        .unwrap_or(&empty);
    let tiers = TIERS
        .iter()
        .map(|tier| {
            let entry = tiers_table.get(tier).and_then(Value::as_table);
            let text = |key: &str| {
                entry
                    .and_then(|entry| entry.get(key))
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string()
            };
            TierConfig {
                tier,
                model: text("model"),
                effort: text("effort"),
            }
        })
        .collect();
    Ok(AgentConfig {
        id,
        source,
        command,
        quality_order,
        allowed_models,
        effort_caps,
        tier_pools,
        tiers,
        given,
        unused_kept_in_file,
    })
}

/// The profiles of `[models] profile`.
pub const PROFILES: [&str; 3] = ["balanced", "performance", "efficient"];

/// The 1.0 schema of `[models]` with its defaults (v0.4.0's values).
pub fn models_defaults() -> Table {
    let mut table = Table::new();
    table.insert("profile", Value::from("balanced"));
    table.insert("fetch_scores", Value::from(true));
    table.insert("ttl_hours", Value::from(24.0));
    table
}

fn read_models(
    where_: &str,
    values: &Table,
    notices: &mut Vec<Notice>,
) -> Result<ModelsConfig, ConfigError> {
    let merged = merge_top(where_, values, "models", &models_defaults(), notices)?;
    let profile = merged
        .get("profile")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if !PROFILES.contains(&profile.as_str()) {
        return Err(ConfigError::invalid(format!(
            "{where_}: models.profile = {}; use one of: {}",
            Value::from(profile.as_str()),
            PROFILES.join(", ")
        )));
    }
    let ttl_hours = merged
        .get("ttl_hours")
        .and_then(Value::as_float)
        .unwrap_or(24.0);
    if ttl_hours.is_nan() || ttl_hours <= 0.0 {
        return Err(ConfigError::invalid(format!(
            "{where_}: models.ttl_hours must be a positive number of hours"
        )));
    }
    Ok(ModelsConfig {
        profile,
        fetch_scores: merged
            .get("fetch_scores")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        ttl_hours,
    })
}

/// The 1.0 schema of `[skills]` with its defaults.
pub fn skills_defaults() -> Table {
    let mut review = Table::new();
    review.insert("license_allow", Value::Array(Vec::new()));
    let mut table = Table::new();
    table.insert("update_interval_days", Value::from(1i64));
    table.insert("review", Value::Table(review));
    table
}

fn read_skills(
    where_: &str,
    values: &Table,
    notices: &mut Vec<Notice>,
) -> Result<SkillsConfig, ConfigError> {
    let merged = merge_top(where_, values, "skills", &skills_defaults(), notices)?;
    let license_allow = string_list(
        where_,
        "skills.review.license_allow",
        merged
            .get_path(&["review", "license_allow"])
            .unwrap_or(&Value::Array(Vec::new())),
        "license ids",
    )?;
    Ok(SkillsConfig {
        update_interval_days: merged
            .get("update_interval_days")
            .and_then(Value::as_integer)
            .unwrap_or(1),
        license_allow,
    })
}

pub(crate) fn merge_top(
    where_: &str,
    values: &Table,
    name: &str,
    defaults: &Table,
    notices: &mut Vec<Notice>,
) -> Result<Table, ConfigError> {
    match top_table(where_, values, name)? {
        None => Ok(defaults.clone()),
        Some(given) => {
            let mut merger = Merger::new(where_);
            let (merged, _) = merger.merge(defaults, given, name)?;
            notices.extend(merger.notices);
            Ok(merged)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(text: &str) -> Result<(Effective, Vec<Notice>), ConfigError> {
        let file = ConfigFile::from_bytes(Path::new("c.toml"), Some(text.as_bytes().to_vec()))?;
        interpret(&file)
    }

    #[test]
    fn section_ids_follow_the_pattern() {
        assert!(is_section_id("a"));
        assert!(is_section_id("packet-2"));
        assert!(!is_section_id("2a"));
        assert!(!is_section_id("A"));
        assert!(!is_section_id(""));
        assert!(!is_section_id(&"a".repeat(33)));
        assert!(is_section_id(&"a".repeat(32)));
    }

    #[test]
    fn an_empty_file_has_the_four_builtin_sections_and_default_agents() {
        let (effective, notices) = read("").unwrap();
        let ids: Vec<&str> = effective.sections.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, BUILTIN_SECTIONS);
        assert_eq!(effective.agent_for("plan"), Some(AgentId::Claude));
        assert_eq!(effective.agent_for("fix"), Some(AgentId::Codex));
        assert_eq!(effective.sections[2].independent_of, ["build".to_string()]);
        assert!(notices.is_empty());
    }

    #[test]
    fn codex_keeps_v040s_highest_effort_at_max() {
        let defaults = agent_defaults(AgentId::Codex);
        assert_eq!(
            defaults.get_path(&["tiers", "max", "effort"]),
            Some(&Value::from("highest"))
        );
        let defaults = agent_defaults(AgentId::Gemini);
        assert_eq!(
            defaults.get_path(&["tiers", "max", "effort"]),
            Some(&Value::from("max"))
        );
    }

    #[test]
    fn a_stage_naming_an_unknown_agent_is_invalid() {
        let error = read("[stages]\nplan = \"robot\"\n").unwrap_err();
        assert_eq!(error.code(), codes::INVALID);
    }

    #[test]
    fn engines_keys_kept_unused_are_exactly_the_three_v040_keys() {
        let keys = |path: &[&str]| path.iter().map(|part| part.to_string()).collect::<Vec<_>>();
        assert!(kept_unused_engine_key(&keys(&["permission_mode"])));
        assert!(kept_unused_engine_key(&keys(&["tools"])));
        assert!(kept_unused_engine_key(&keys(&[
            "tiers",
            "max",
            "timeout_min"
        ])));
        assert!(!kept_unused_engine_key(&keys(&[
            "tiers",
            "ultra",
            "timeout_min"
        ])));
        assert!(!kept_unused_engine_key(&keys(&["command"])));
    }
}
