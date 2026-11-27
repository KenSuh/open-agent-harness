// 설정 모듈: 프로젝트 `.oah/config.toml` 과 사용자 `settings.toml` 을 읽고 해석하고 형식을 지켜 저장한다.
//! `.oah/config.toml` and `OAH_HOME/settings.toml`: read, interpret and save without disturbing other bytes.
//!
//! Three layers stay apart:
//!
//! * [`ConfigFile`] holds the exact bytes, the decoded text and the parsed values. A missing file reads as an
//!   empty one and is never created by reading. A UTF-8 byte order mark and CRLF line ends are accepted; bytes
//!   that are not UTF-8 or not TOML are `OAH_CONFIG_INVALID`. [`ConfigFile::section`] merges one table over a
//!   caller's defaults by v0.4.0's type rules.
//! * [`ProjectConfig`] interprets a project file as 1.0 uses it ([`Effective`]): sections, stage assignments
//!   (with v0.4.0's `initial` / `review` / `correction` read through the legacy table), agents (with v0.4.0's
//!   `[engines.<id>]` read as `[agents.<id>]` when that is absent), models and skills, plus non-fatal
//!   [`Notice`]s such as `OAH_CONFIG_UNKNOWN_KEY`.
//! * [`save_project`] and [`save_settings`] change only the keys an [`Edit`] names, keep comments, order, line
//!   ends, the byte order mark and every other table byte for byte, and re-read the result before writing it.
//!   The first save of a v0.4.0 file is the one-way upgrade of §3.1 and needs the user's [`UpgradeApproval`].
//! * [`ProjectTools`] reads the Project Wiki, Impact Map and Jev settings that the Python source CLI reads from
//!   the same file (`[wiki] dir`, `[impact] wiki_dirs` / `graph_file` / `rerank`, `[rerank] enabled` /
//!   `provider`) and the reserved `[project_tools]` table. [`plan_tools_save`] shows what a typed
//!   [`ToolsChange`] would write, reading only; [`apply_tools_save`] writes exactly an approved plan, bound to
//!   the project, its root and the bytes the plan was made from. These two are the entry points the settings
//!   preview and apply of the app and the CLI call.
//!
//! Known difference: the TOML parser reads TOML 1.1, which accepts a few forms (such as a newline inside an
//! inline table) that v0.4.0's TOML 1.0 parser refused.

mod error;
mod file;
mod patch;
mod project;
mod save;
mod settings;
mod tools;
mod value;

pub use error::{ConfigError, Notice, codes, notice_codes};
pub use file::{ConfigFile, Merged};
pub use project::{
    AgentConfig, AgentId, AgentSource, Assignment, AssignmentSource, BUILTIN_SECTIONS, Difficulty,
    Effective, Format, ModelsConfig, PROFILES, ProjectConfig, Section, SectionKind, SkillsConfig,
    StageFormat, TIERS, TierConfig, agent_defaults, default_agent, is_section_id, models_defaults,
    skills_defaults,
};
pub use save::{
    Backup, BackupPlan, Edit, PROJECT_TABLES, SaveOutcome, SavePlan, ToolsApproval, ToolsBinding,
    ToolsKeyChange, ToolsPlan, UPGRADE_WARNING, UpgradeApproval, UpgradeWarning, apply_tools_save,
    backup_path, plan_project_save, plan_tools_save, save_project,
};
pub use settings::{
    AUTOMATION_LEVELS, AutoloadSettings, HistorySettings, SETTINGS_TABLES, SettingsValues, THEMES,
    UiSettings, UserSettings, save_settings,
};
pub use tools::{
    DEFAULT_GRAPH_FILE, DEFAULT_WIKI_DIR, GraphifyTool, ImpactTool, JEV_PROVIDER, JevTool,
    KeptReason, MANAGED_TABLE, ManagedTools, Portable, ProjectTools, Setting, TOOL_KEYS,
    ToolsChange, ToolsKept, WikiIndexTool, WikiTool, managed_defaults, model_problem,
    portable_problem,
};
pub use value::{Datetime, DatetimeKind, Table, Value};
