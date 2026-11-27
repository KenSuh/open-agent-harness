// 프로젝트 도구 설정: 위키·영향 지도·Jev 와 관리형 색인 계약을 같은 .oah/config.toml 에서 읽고, 정해진 변경만 키 편집으로 바꾼다.
//! The project-tools settings of `.oah/config.toml`: Project Wiki, Impact Map and Jev.
//!
//! The desktop app, the Rust CLI and the Python source CLI share one file. This module owns the part of its
//! format the three tools need, and nothing else of the tables it reads:
//!
//! | field | key | default |
//! |---|---|---|
//! | `wiki.dir` | `[wiki] dir` | `"docs/wiki"` |
//! | `impact.wiki_dirs` | `[impact] wiki_dirs` | `["docs/wiki"]` |
//! | `impact.graph_file` | `[impact] graph_file` | `"graphify-out/graph.json"` |
//! | `impact.rerank` | `[impact] rerank` | `false` |
//! | `jev.enabled` | `[rerank] enabled` | `false` |
//! | `jev.provider` | `[rerank] provider` | `""` |
//! | `managed.wiki_index.*` | `[project_tools.wiki_index]` `managed`, `exclude`, `embedding_model` | `false`, `[]`, `""` |
//! | `managed.graphify.managed` | `[project_tools.graphify] managed` | `false` |
//!
//! A key the file does not give keeps the source CLI's default, and reading never follows one key from another:
//! `impact.wiki_dirs` without a value is `["docs/wiki"]` whatever `wiki.dir` says. Every other key of
//! `[wiki]`, `[impact]` and `[rerank]` belongs to the source CLI: it is not read, reported or rewritten here.
//! `[project_tools]` is the reserved table for the managed wiki index and graph of a later step; a source CLI
//! that does not know it ignores it. Its unknown keys are `OAH_CONFIG_UNKNOWN_KEY` notices.
//!
//! Values in this file are portable: folders and globs relative to the project, written with `/`, and model
//! names. A program path, a cache or index folder, a host approval, a key and a network consent are per-machine
//! state and never belong here. An existing value that is not portable is read as written with an
//! `OAH_CONFIG_NOT_PORTABLE` notice; a write refuses one.
//!
//! Writes are typed: a [`ToolsChange`] names one setting, and `edits_for` turns a list of them into edits of
//! exactly the keys in [`TOOL_KEYS`]. `config::plan_tools_save` and `config::apply_tools_save` are the only
//! way those edits reach the file.

use super::error::{ConfigError, Notice, notice_codes};
use super::file::{ConfigFile, Merger, not_a_table, type_error};
use super::save::Edit;
use super::value::{Table, Value};

/// `[wiki] dir` when the file does not give it.
pub const DEFAULT_WIKI_DIR: &str = "docs/wiki";
/// `[impact] graph_file` when the file does not give it.
pub const DEFAULT_GRAPH_FILE: &str = "graphify-out/graph.json";
/// The provider `[rerank] provider` names when Jev is turned on and the file names none.
pub const JEV_PROVIDER: &str = "typesafe";
/// The reserved top-level table of the managed wiki index and graph.
pub const MANAGED_TABLE: &str = "project_tools";

/// Every key the project-tools writer sets or removes, and no other.
pub const TOOL_KEYS: [&[&str]; 10] = [
    &["wiki", "dir"],
    &["impact", "wiki_dirs"],
    &["impact", "graph_file"],
    &["impact", "rerank"],
    &["rerank", "enabled"],
    &["rerank", "provider"],
    &["project_tools", "wiki_index", "managed"],
    &["project_tools", "wiki_index", "exclude"],
    &["project_tools", "wiki_index", "embedding_model"],
    &["project_tools", "graphify", "managed"],
];

/// A value and whether the file gives it; without the key the value is the default.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Setting<T> {
    pub value: T,
    pub explicit: bool,
}

impl<T> Setting<T> {
    fn default_of(value: T) -> Self {
        Setting {
            value,
            explicit: false,
        }
    }

    fn given(value: T) -> Self {
        Setting {
            value,
            explicit: true,
        }
    }
}

/// Project Wiki: `[wiki] dir`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiTool {
    pub dir: Setting<String>,
}

/// Impact Map: `[impact] wiki_dirs`, `graph_file` and `rerank`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImpactTool {
    pub wiki_dirs: Setting<Vec<String>>,
    pub graph_file: Setting<String>,
    pub rerank: Setting<bool>,
}

/// Jev: `[rerank] enabled` and `provider`. A key, a consent and a destination are never read from this file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JevTool {
    pub enabled: Setting<bool>,
    pub provider: Setting<String>,
}

/// `[project_tools.wiki_index]`: what the project asks of a managed local wiki index.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WikiIndexTool {
    /// The project asks OAH to keep a local index of its wiki. Each machine still approves and stores it locally.
    pub managed: Setting<bool>,
    /// Globs under the wiki folder left out of the index, beside the ones the index always leaves out.
    pub exclude: Setting<Vec<String>>,
    /// The embedding model's name; empty for the default of the machine's index.
    pub embedding_model: Setting<String>,
}

/// `[project_tools.graphify]`: what the project asks of a managed code graph. Its file is `impact.graph_file`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GraphifyTool {
    /// The project asks OAH to refresh the graph. Each machine still approves a run locally.
    pub managed: Setting<bool>,
}

/// `[project_tools]`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedTools {
    pub wiki_index: WikiIndexTool,
    pub graphify: GraphifyTool,
}

/// The project-tools settings as both CLIs and the desktop app read them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectTools {
    pub wiki: WikiTool,
    pub impact: ImpactTool,
    pub jev: JevTool,
    pub managed: ManagedTools,
    /// `OAH_CONFIG_NOT_PORTABLE` and `OAH_CONFIG_UNKNOWN_KEY` findings; the values above are still the file's.
    pub notices: Vec<Notice>,
}

impl ProjectTools {
    /// Reads every tool's settings. A wrong type, a table written as a value or a blank wiki folder is
    /// `OAH_CONFIG_INVALID`. Reading changes nothing.
    pub fn read(file: &ConfigFile) -> Result<ProjectTools, ConfigError> {
        let mut notices = Vec::new();
        let wiki = WikiTool::read(file, &mut notices)?;
        let impact = ImpactTool::read(file, &mut notices)?;
        let jev = JevTool::read(file)?;
        let managed = ManagedTools::read(file, &mut notices)?;
        Ok(ProjectTools {
            wiki,
            impact,
            jev,
            managed,
            notices,
        })
    }
}

impl WikiTool {
    /// Reads `[wiki] dir` alone, so a broken table elsewhere does not hide it.
    pub fn read(file: &ConfigFile, notices: &mut Vec<Notice>) -> Result<WikiTool, ConfigError> {
        let where_ = file.where_();
        let table = top_table(file, "wiki")?;
        let dir = string_key(where_, table, "wiki.dir", DEFAULT_WIKI_DIR)?;
        // Blank as the source CLI's `str.strip()` sees it, which also strips the four separators U+001C..U+001F.
        let blank = |c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c);
        if dir.value.chars().all(blank) {
            return Err(ConfigError::invalid(format!(
                "{where_}: wiki.dir must be a folder path; set it to a folder inside the project or remove it to \
                 use the default"
            )));
        }
        note_path(where_, "wiki.dir", &dir, Portable::Path, notices);
        Ok(WikiTool { dir })
    }
}

impl ImpactTool {
    /// Reads the three `[impact]` keys alone.
    pub fn read(file: &ConfigFile, notices: &mut Vec<Notice>) -> Result<ImpactTool, ConfigError> {
        let where_ = file.where_();
        let table = top_table(file, "impact")?;
        let wiki_dirs = list_key(
            where_,
            table,
            "impact.wiki_dirs",
            &[DEFAULT_WIKI_DIR],
            "folder paths",
        )?;
        let graph_file = string_key(where_, table, "impact.graph_file", DEFAULT_GRAPH_FILE)?;
        let rerank = bool_key(where_, table, "impact.rerank", false)?;
        note_list(
            where_,
            "impact.wiki_dirs",
            &wiki_dirs,
            Portable::Path,
            notices,
        );
        note_path(
            where_,
            "impact.graph_file",
            &graph_file,
            Portable::Path,
            notices,
        );
        Ok(ImpactTool {
            wiki_dirs,
            graph_file,
            rerank,
        })
    }
}

impl JevTool {
    /// Reads the two `[rerank]` keys alone.
    pub fn read(file: &ConfigFile) -> Result<JevTool, ConfigError> {
        let where_ = file.where_();
        let table = top_table(file, "rerank")?;
        Ok(JevTool {
            enabled: bool_key(where_, table, "rerank.enabled", false)?,
            provider: string_key(where_, table, "rerank.provider", "")?,
        })
    }
}

/// The schema of `[project_tools]` with its defaults.
pub fn managed_defaults() -> Table {
    let mut wiki_index = Table::new();
    wiki_index.insert("managed", Value::from(false));
    wiki_index.insert("exclude", Value::Array(Vec::new()));
    wiki_index.insert("embedding_model", Value::from(""));
    let mut graphify = Table::new();
    graphify.insert("managed", Value::from(false));
    let mut table = Table::new();
    table.insert("wiki_index", Value::Table(wiki_index));
    table.insert("graphify", Value::Table(graphify));
    table
}

impl ManagedTools {
    /// Reads `[project_tools]` alone.
    pub fn read(file: &ConfigFile, notices: &mut Vec<Notice>) -> Result<ManagedTools, ConfigError> {
        let where_ = file.where_();
        let defaults = managed_defaults();
        let (merged, used) = match top_table(file, MANAGED_TABLE)? {
            None => (defaults, Table::new()),
            Some(given) => {
                let mut merger = Merger::new(where_);
                let merged = merger.merge(&defaults, given, MANAGED_TABLE)?;
                notices.extend(merger.notices);
                merged
            }
        };
        let flag = |path: &[&str]| Setting {
            value: merged
                .get_path(path)
                .and_then(Value::as_bool)
                .unwrap_or(false),
            explicit: used.get_path(path).is_some(),
        };
        let exclude_path = ["wiki_index", "exclude"];
        let exclude_items = merged
            .get_path(&exclude_path)
            .and_then(Value::as_array)
            .unwrap_or_default();
        let exclude = Setting {
            value: strings(
                where_,
                "project_tools.wiki_index.exclude",
                exclude_items,
                "globs",
            )?,
            explicit: used.get_path(&exclude_path).is_some(),
        };
        let model_path = ["wiki_index", "embedding_model"];
        let embedding_model = Setting {
            value: merged
                .get_path(&model_path)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            explicit: used.get_path(&model_path).is_some(),
        };
        note_list(
            where_,
            "project_tools.wiki_index.exclude",
            &exclude,
            Portable::Glob,
            notices,
        );
        if let Some(reason) = model_problem(&embedding_model.value) {
            notices.push(not_portable(
                where_,
                "project_tools.wiki_index.embedding_model",
                &embedding_model.value,
                reason,
            ));
        }
        Ok(ManagedTools {
            wiki_index: WikiIndexTool {
                managed: flag(&["wiki_index", "managed"]),
                exclude,
                embedding_model,
            },
            graphify: GraphifyTool {
                managed: flag(&["graphify", "managed"]),
            },
        })
    }
}

fn top_table<'a>(file: &'a ConfigFile, name: &str) -> Result<Option<&'a Table>, ConfigError> {
    match file.values().get(name) {
        None => Ok(None),
        Some(Value::Table(table)) => Ok(Some(table)),
        Some(other) => Err(not_a_table(file.where_(), name, other)),
    }
}

fn last_part(dotted: &str) -> &str {
    dotted.rsplit('.').next().unwrap_or(dotted)
}

fn string_key(
    where_: &str,
    table: Option<&Table>,
    dotted: &str,
    default: &str,
) -> Result<Setting<String>, ConfigError> {
    match table.and_then(|table| table.get(last_part(dotted))) {
        None => Ok(Setting::default_of(default.to_string())),
        Some(Value::String(text)) => Ok(Setting::given(text.clone())),
        Some(other) => Err(type_error(where_, dotted, &Value::from(""), other)),
    }
}

fn bool_key(
    where_: &str,
    table: Option<&Table>,
    dotted: &str,
    default: bool,
) -> Result<Setting<bool>, ConfigError> {
    match table.and_then(|table| table.get(last_part(dotted))) {
        None => Ok(Setting::default_of(default)),
        Some(Value::Boolean(flag)) => Ok(Setting::given(*flag)),
        Some(other) => Err(type_error(where_, dotted, &Value::from(false), other)),
    }
}

fn list_key(
    where_: &str,
    table: Option<&Table>,
    dotted: &str,
    default: &[&str],
    what: &str,
) -> Result<Setting<Vec<String>>, ConfigError> {
    match table.and_then(|table| table.get(last_part(dotted))) {
        None => Ok(Setting::default_of(
            default.iter().map(|item| item.to_string()).collect(),
        )),
        Some(Value::Array(items)) => Ok(Setting::given(strings(where_, dotted, items, what)?)),
        Some(other) => Err(type_error(where_, dotted, &Value::Array(Vec::new()), other)),
    }
}

fn strings(
    where_: &str,
    dotted: &str,
    items: &[Value],
    what: &str,
) -> Result<Vec<String>, ConfigError> {
    items
        .iter()
        .map(|item| {
            item.as_str().map(str::to_string).ok_or_else(|| {
                ConfigError::invalid(format!("{where_}: {dotted} must list {what} as strings"))
            })
        })
        .collect()
}

/// What a portable value names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Portable {
    /// A folder or file below the project root.
    Path,
    /// A glob below a project folder: a path that may also use `*`, `?` and `[...]`.
    Glob,
}

const RESERVED_NAMES: [&str; 22] = [
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
    "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
];

/// Why `text` is not a portable project-relative path or glob, or `None` when it is one: relative, written
/// with `/`, without `.`, `..`, a drive or scheme, and usable as a name on Windows as on Unix. One trailing `/` is
/// accepted.
pub fn portable_problem(text: &str, kind: Portable) -> Option<&'static str> {
    if text.is_empty() {
        return Some("it is empty");
    }
    if text.contains('\\') {
        return Some("it uses a backslash");
    }
    if text.starts_with('/') {
        return Some("it is an absolute path");
    }
    if text.starts_with('~') {
        return Some("it starts with ~");
    }
    if text.contains(':') {
        return Some("it names a drive, a scheme or a stream");
    }
    if text.chars().any(|c| c < ' ' || c == '\u{7f}') {
        return Some("it has a control character");
    }
    let refused: &[char] = match kind {
        Portable::Path => &['<', '>', '"', '|', '?', '*'],
        Portable::Glob => &['<', '>', '"', '|'],
    };
    if text.contains(refused) {
        return Some("it has a character Windows does not allow in names");
    }
    for part in text.strip_suffix('/').unwrap_or(text).split('/') {
        if part.is_empty() {
            return Some("it has an empty name between two /");
        }
        if part == "." || part == ".." {
            return Some("it has a . or .. part");
        }
        if part.ends_with('.') || part.ends_with(' ') {
            return Some("a name in it ends with a dot or a space");
        }
        let stem = part.split('.').next().unwrap_or(part);
        if RESERVED_NAMES
            .iter()
            .any(|name| name.eq_ignore_ascii_case(stem))
        {
            return Some("it uses a name Windows reserves");
        }
    }
    None
}

/// Why `text` is not a model name, or `None` when it is one or empty: at most 128 ASCII letters, digits and
/// `. _ : / -`, starting with a letter or digit, without `//` or `..`. A URL, a host and a file path are refused.
pub fn model_problem(text: &str) -> Option<&'static str> {
    if text.is_empty() {
        return None;
    }
    let allowed = |c: char| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | ':' | '/' | '-');
    if text.len() > 128
        || !text.starts_with(|c: char| c.is_ascii_alphanumeric())
        || !text.chars().all(allowed)
        || text.contains("//")
        || text.contains("..")
    {
        return Some("it is not a model name");
    }
    None
}

fn not_portable(where_: &str, key: &str, value: &str, reason: &str) -> Notice {
    Notice::new(
        notice_codes::NOT_PORTABLE,
        format!(
            "{where_}: {key} has {}, which another machine cannot use as written: {reason}",
            Value::from(value)
        ),
        vec![key.to_string()],
    )
}

fn note_path(
    where_: &str,
    key: &str,
    setting: &Setting<String>,
    kind: Portable,
    notices: &mut Vec<Notice>,
) {
    // An empty value names nothing to carry to another machine; the source CLI reads it as it always did.
    if setting.explicit
        && !setting.value.is_empty()
        && let Some(reason) = portable_problem(&setting.value, kind)
    {
        notices.push(not_portable(where_, key, &setting.value, reason));
    }
}

fn note_list(
    where_: &str,
    key: &str,
    setting: &Setting<Vec<String>>,
    kind: Portable,
    notices: &mut Vec<Notice>,
) {
    if !setting.explicit {
        return;
    }
    let found = setting.value.iter().find_map(|item| {
        (!item.is_empty())
            .then(|| portable_problem(item, kind).map(|reason| (item, reason)))
            .flatten()
    });
    if let Some((item, reason)) = found {
        notices.push(not_portable(where_, key, item, reason));
    }
}

/// One setting a caller asks to change. Each names a value of the model, never a key of the file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolsChange {
    /// The project wiki's folder. `impact.wiki_dirs` follows it while the file does not pin other folders: when
    /// that key is absent or names exactly the wiki folder used until now. Another explicit list is kept.
    WikiDir(String),
    /// The explicit list of wiki folders the Impact Map reads, or `None` to remove the override.
    ImpactWikiDirs(Option<Vec<String>>),
    /// The graph file the Impact Map reads, relative to the project.
    ImpactGraphFile(String),
    /// Whether Impact Map results are re-ordered. Separate from [`ToolsChange::JevEnabled`].
    ImpactRerank(bool),
    /// Whether this project uses Jev: `[rerank] enabled`. Turning it on names the provider only when the file
    /// names none; no key, consent or destination is written, and `impact.rerank` is left alone.
    JevEnabled(bool),
    WikiIndexManaged(bool),
    WikiIndexExclude(Vec<String>),
    WikiIndexEmbeddingModel(String),
    GraphifyManaged(bool),
}

impl ToolsChange {
    /// The setting this change is about, as its dotted key.
    pub fn key(&self) -> &'static str {
        match self {
            ToolsChange::WikiDir(_) => "wiki.dir",
            ToolsChange::ImpactWikiDirs(_) => "impact.wiki_dirs",
            ToolsChange::ImpactGraphFile(_) => "impact.graph_file",
            ToolsChange::ImpactRerank(_) => "impact.rerank",
            ToolsChange::JevEnabled(_) => "rerank.enabled",
            ToolsChange::WikiIndexManaged(_) => "project_tools.wiki_index.managed",
            ToolsChange::WikiIndexExclude(_) => "project_tools.wiki_index.exclude",
            ToolsChange::WikiIndexEmbeddingModel(_) => "project_tools.wiki_index.embedding_model",
            ToolsChange::GraphifyManaged(_) => "project_tools.graphify.managed",
        }
    }
}

/// Why a value stayed as the file has it although a change touched its tool.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeptReason {
    /// `impact.wiki_dirs` lists folders of its own, so a new wiki folder did not replace them.
    ExplicitWikiDirs,
    /// `rerank.provider` names another provider, so turning Jev on did not replace it.
    OtherProvider,
}

impl KeptReason {
    pub fn as_str(self) -> &'static str {
        match self {
            KeptReason::ExplicitWikiDirs => "explicit-wiki-dirs",
            KeptReason::OtherProvider => "other-provider",
        }
    }
}

/// A value a plan leaves as it is, shown with the plan.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolsKept {
    pub key: &'static str,
    pub value: Value,
    pub reason: KeptReason,
}

fn list_value(items: &[String]) -> Value {
    Value::Array(
        items
            .iter()
            .map(|item| Value::from(item.as_str()))
            .collect(),
    )
}

fn refuse(where_: &str, key: &str, value: &str, reason: &str, hint: &str) -> ConfigError {
    ConfigError::invalid(format!(
        "{where_}: {key} cannot be set to {}: {reason}; {hint}; nothing was written",
        Value::from(value)
    ))
}

fn require_path(where_: &str, key: &str, value: &str, kind: Portable) -> Result<(), ConfigError> {
    let hint = match kind {
        Portable::Path => "use a path inside the project written with /, such as docs/wiki",
        Portable::Glob => "use a glob below the wiki folder written with /, such as drafts/**",
    };
    let problem =
        portable_problem(value, kind).or_else(|| value.ends_with('/').then_some("it ends with /"));
    match problem {
        Some(reason) => Err(refuse(where_, key, value, reason, hint)),
        None => Ok(()),
    }
}

/// The edits of `changes` over the settings `tools` read, and what they leave as it is.
///
/// A change that asks for the value the setting already has (given or default) yields no edit, so a file is
/// never touched to write a default. A value that is not portable, or the same setting listed twice, is
/// `OAH_CONFIG_INVALID`.
pub(crate) fn edits_for(
    where_: &str,
    tools: &ProjectTools,
    changes: &[ToolsChange],
) -> Result<(Vec<Edit>, Vec<ToolsKept>), ConfigError> {
    for (index, change) in changes.iter().enumerate() {
        if changes[..index]
            .iter()
            .any(|other| other.key() == change.key())
        {
            return Err(ConfigError::invalid(format!(
                "{where_}: the change lists {} twice; give each setting once; nothing was written",
                change.key()
            )));
        }
    }
    let pins_wiki_dirs = changes
        .iter()
        .any(|change| matches!(change, ToolsChange::ImpactWikiDirs(_)));
    let mut edits = Vec::new();
    let mut kept = Vec::new();
    for change in changes {
        let key = change.key();
        match change {
            ToolsChange::WikiDir(dir) => {
                require_path(where_, key, dir, Portable::Path)?;
                if tools.wiki.dir.value != *dir {
                    edits.push(Edit::set(&["wiki", "dir"], dir.as_str()));
                }
                let dirs = &tools.impact.wiki_dirs;
                let wanted = std::slice::from_ref(dir);
                let follows =
                    !dirs.explicit || dirs.value == std::slice::from_ref(&tools.wiki.dir.value);
                if pins_wiki_dirs || dirs.value == wanted {
                    // The same change sets the list itself, or it already names the new folder.
                } else if follows {
                    edits.push(Edit::set(&["impact", "wiki_dirs"], list_value(wanted)));
                } else {
                    kept.push(ToolsKept {
                        key: "impact.wiki_dirs",
                        value: list_value(&dirs.value),
                        reason: KeptReason::ExplicitWikiDirs,
                    });
                }
            }
            ToolsChange::ImpactWikiDirs(Some(dirs)) => {
                for dir in dirs {
                    require_path(where_, key, dir, Portable::Path)?;
                }
                let current = &tools.impact.wiki_dirs;
                if !current.explicit || current.value != *dirs {
                    edits.push(Edit::set(&["impact", "wiki_dirs"], list_value(dirs)));
                }
            }
            ToolsChange::ImpactWikiDirs(None) => {
                if tools.impact.wiki_dirs.explicit {
                    edits.push(Edit::remove(&["impact", "wiki_dirs"]));
                }
            }
            ToolsChange::ImpactGraphFile(file) => {
                require_path(where_, key, file, Portable::Path)?;
                if tools.impact.graph_file.value != *file {
                    edits.push(Edit::set(&["impact", "graph_file"], file.as_str()));
                }
            }
            ToolsChange::ImpactRerank(on) => {
                if tools.impact.rerank.value != *on {
                    edits.push(Edit::set(&["impact", "rerank"], *on));
                }
            }
            ToolsChange::JevEnabled(on) => {
                if tools.jev.enabled.value != *on {
                    edits.push(Edit::set(&["rerank", "enabled"], *on));
                }
                let provider = &tools.jev.provider.value;
                if *on && provider.is_empty() {
                    edits.push(Edit::set(&["rerank", "provider"], JEV_PROVIDER));
                } else if *on && provider != JEV_PROVIDER {
                    kept.push(ToolsKept {
                        key: "rerank.provider",
                        value: Value::from(provider.as_str()),
                        reason: KeptReason::OtherProvider,
                    });
                }
            }
            ToolsChange::WikiIndexManaged(on) => {
                if tools.managed.wiki_index.managed.value != *on {
                    edits.push(Edit::set(TOOL_KEYS[6], *on));
                }
            }
            ToolsChange::WikiIndexExclude(globs) => {
                for glob in globs {
                    require_path(where_, key, glob, Portable::Glob)?;
                }
                if tools.managed.wiki_index.exclude.value != *globs {
                    edits.push(Edit::set(TOOL_KEYS[7], list_value(globs)));
                }
            }
            ToolsChange::WikiIndexEmbeddingModel(model) => {
                if let Some(reason) = model_problem(model) {
                    return Err(refuse(
                        where_,
                        key,
                        model,
                        reason,
                        "use the model's name alone, such as bge-m3, or \"\" for the default",
                    ));
                }
                if tools.managed.wiki_index.embedding_model.value != *model {
                    edits.push(Edit::set(TOOL_KEYS[8], model.as_str()));
                }
            }
            ToolsChange::GraphifyManaged(on) => {
                if tools.managed.graphify.managed.value != *on {
                    edits.push(Edit::set(TOOL_KEYS[9], *on));
                }
            }
        }
    }
    Ok((edits, kept))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn file(text: &str) -> ConfigFile {
        ConfigFile::from_bytes(Path::new("c.toml"), Some(text.as_bytes().to_vec())).unwrap()
    }

    fn paths(edits: &[Edit]) -> Vec<String> {
        edits
            .iter()
            .map(|edit| match edit {
                Edit::Set { path, value } => format!("{}={value}", path.join(".")),
                Edit::Remove { path } => format!("-{}", path.join(".")),
            })
            .collect()
    }

    fn edits(text: &str, changes: &[ToolsChange]) -> (Vec<String>, Vec<ToolsKept>) {
        let tools = ProjectTools::read(&file(text)).unwrap();
        let (edits, kept) = edits_for("c.toml", &tools, changes).unwrap();
        (paths(&edits), kept)
    }

    #[test]
    fn portable_paths_are_relative_slash_paths_usable_on_windows() {
        for good in [
            "docs/wiki",
            "wiki",
            "knowledge/wiki/",
            "a.b/c-d_e",
            "문서/위키",
        ] {
            assert_eq!(portable_problem(good, Portable::Path), None, "{good}");
        }
        for bad in [
            "",
            "/abs",
            "C:/wiki",
            "docs\\wiki",
            "../wiki",
            "docs/./wiki",
            "docs//wiki",
            "~/wiki",
            "docs/wiki.",
            "docs/wiki ",
            "docs/aux",
            "docs/NUL.txt",
            "docs/wi*ki",
            "docs/a\tb",
            "https://example.com/wiki",
        ] {
            assert!(portable_problem(bad, Portable::Path).is_some(), "{bad:?}");
        }
        assert_eq!(portable_problem("drafts/**/*.md", Portable::Glob), None);
        assert!(portable_problem("../**", Portable::Glob).is_some());
    }

    #[test]
    fn model_names_are_names_and_never_urls_or_paths() {
        for good in ["", "bge-m3", "bge-m3:latest", "nomic-ai/nomic-embed-text"] {
            assert_eq!(model_problem(good), None, "{good}");
        }
        for bad in [
            "http://localhost:11434",
            "/models/x",
            "a b",
            "..",
            "-x",
            "a\\b",
        ] {
            assert!(model_problem(bad).is_some(), "{bad}");
        }
        assert!(model_problem(&"a".repeat(129)).is_some());
    }

    #[test]
    fn a_change_to_the_value_a_setting_already_has_writes_nothing() {
        let (edits, kept) = edits(
            "",
            &[
                ToolsChange::WikiDir(DEFAULT_WIKI_DIR.into()),
                ToolsChange::ImpactGraphFile(DEFAULT_GRAPH_FILE.into()),
                ToolsChange::ImpactRerank(false),
                ToolsChange::JevEnabled(false),
                ToolsChange::WikiIndexManaged(false),
                ToolsChange::GraphifyManaged(false),
                ToolsChange::WikiIndexExclude(Vec::new()),
                ToolsChange::WikiIndexEmbeddingModel(String::new()),
                ToolsChange::ImpactWikiDirs(None),
            ],
        );
        assert!(edits.is_empty(), "{edits:?}");
        assert!(kept.is_empty());
    }

    #[test]
    fn the_map_follows_a_new_wiki_folder_unless_it_lists_its_own() {
        let moved = [ToolsChange::WikiDir("knowledge/wiki".into())];
        let (absent, kept) = edits("", &moved);
        assert_eq!(
            absent,
            [
                "wiki.dir=\"knowledge/wiki\"",
                "impact.wiki_dirs=[\"knowledge/wiki\"]"
            ]
        );
        assert!(kept.is_empty());
        let (mirror, _) = edits(
            "[wiki]\ndir = \"notes\"\n[impact]\nwiki_dirs = [\"notes\"]\n",
            &moved,
        );
        assert_eq!(
            mirror,
            [
                "wiki.dir=\"knowledge/wiki\"",
                "impact.wiki_dirs=[\"knowledge/wiki\"]"
            ]
        );
        let (own, kept) = edits("[impact]\nwiki_dirs = [\"docs/wiki\", \"notes\"]\n", &moved);
        assert_eq!(own, ["wiki.dir=\"knowledge/wiki\""]);
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0].reason, KeptReason::ExplicitWikiDirs);
        let (back, _) = edits(
            "[wiki]\ndir = \"notes\"\n[impact]\nwiki_dirs = [\"notes\"]\n",
            &[ToolsChange::WikiDir(DEFAULT_WIKI_DIR.into())],
        );
        assert_eq!(
            back,
            ["wiki.dir=\"docs/wiki\"", "impact.wiki_dirs=[\"docs/wiki\"]"]
        );
        let (pinned, kept) = edits(
            "",
            &[
                ToolsChange::WikiDir("notes".into()),
                ToolsChange::ImpactWikiDirs(Some(vec!["a".into(), "b".into()])),
            ],
        );
        assert_eq!(
            pinned,
            ["wiki.dir=\"notes\"", "impact.wiki_dirs=[\"a\", \"b\"]"]
        );
        assert!(kept.is_empty());
    }

    #[test]
    fn turning_jev_on_names_the_provider_only_when_the_file_names_none() {
        let on = [ToolsChange::JevEnabled(true)];
        assert_eq!(
            edits("", &on).0,
            ["rerank.enabled=true", "rerank.provider=\"typesafe\""]
        );
        assert_eq!(
            edits("[rerank]\nenabled = false\nprovider = \"\"\n", &on).0,
            ["rerank.enabled=true", "rerank.provider=\"typesafe\""]
        );
        let (other, kept) = edits("[rerank]\nprovider = \"mine\"\n", &on);
        assert_eq!(other, ["rerank.enabled=true"]);
        assert_eq!(kept[0].reason, KeptReason::OtherProvider);
        let off = [ToolsChange::JevEnabled(false)];
        assert_eq!(
            edits("[rerank]\nenabled = true\nprovider = \"typesafe\"\n", &off).0,
            ["rerank.enabled=false"]
        );
        assert!(
            edits("[rerank]\nenabled = true\nprovider = \"typesafe\"\n", &on)
                .0
                .is_empty()
        );
    }

    #[test]
    fn a_value_that_is_not_portable_or_a_setting_listed_twice_is_refused() {
        let tools = ProjectTools::read(&file("")).unwrap();
        for change in [
            ToolsChange::WikiDir("/srv/wiki".into()),
            ToolsChange::WikiDir("docs/wiki/".into()),
            ToolsChange::ImpactWikiDirs(Some(vec!["ok".into(), "..".into()])),
            ToolsChange::ImpactGraphFile("C:\\graph.json".into()),
            ToolsChange::WikiIndexExclude(vec!["/abs/**".into()]),
            ToolsChange::WikiIndexEmbeddingModel("http://127.0.0.1:11434".into()),
        ] {
            let error = edits_for("c.toml", &tools, std::slice::from_ref(&change)).unwrap_err();
            assert_eq!(error.code(), "OAH_CONFIG_INVALID", "{change:?}");
            assert!(error.message().contains("nothing was written"), "{error}");
        }
        let twice = [
            ToolsChange::JevEnabled(true),
            ToolsChange::JevEnabled(false),
        ];
        assert_eq!(
            edits_for("c.toml", &tools, &twice).unwrap_err().code(),
            "OAH_CONFIG_INVALID"
        );
    }

    #[test]
    fn every_edit_names_a_key_of_the_writer() {
        let everything = [
            ToolsChange::WikiDir("w".into()),
            ToolsChange::ImpactWikiDirs(Some(vec!["w".into()])),
            ToolsChange::ImpactGraphFile("g.json".into()),
            ToolsChange::ImpactRerank(true),
            ToolsChange::JevEnabled(true),
            ToolsChange::WikiIndexManaged(true),
            ToolsChange::WikiIndexExclude(vec!["raw/**".into()]),
            ToolsChange::WikiIndexEmbeddingModel("bge-m3".into()),
            ToolsChange::GraphifyManaged(true),
        ];
        let tools = ProjectTools::read(&file("")).unwrap();
        let (edits, _) = edits_for("c.toml", &tools, &everything).unwrap();
        assert_eq!(edits.len(), TOOL_KEYS.len());
        for edit in &edits {
            let (Edit::Set { path, .. } | Edit::Remove { path }) = edit;
            assert!(
                TOOL_KEYS
                    .iter()
                    .any(|key| key.iter().copied().eq(path.iter().map(String::as_str))),
                "{path:?}"
            );
        }
    }
}
