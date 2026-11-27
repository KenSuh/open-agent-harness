// 프로젝트 도구 상태: 연 프로젝트의 위키 폴더·영향 지도·Jev 설정을 읽기만 하여 카드 세 장의 상태와 다음 단계를 만든다.
//! The Project Wiki, Impact Map and Jev cards of the Team Workflow page (read).
//!
//! [`project_tools_status`] is the one source the desktop command and `oah project-tools status` share. It takes
//! the session's open project, already located and identified by `app::project_open`, and requires the caller to
//! name that project's id; the answer carries the id, so a caller that moved on meanwhile can drop it.
//!
//! [`project_tools_status_in`] is the CLI's entry: it finds the project itself, without starting Git.
//!
//! [`tools_status_at`] does the reading. It receives a located project and nothing else: no environment, so it
//! cannot find a program to start, a home folder or a key, and it has no transport. It creates nothing. What it
//! reads is bounded: `.oah/config.toml` through the Team Workflow's own link-refusing reader, the metadata of a
//! few named folders and files, at most [`wiki::ROOT_SCAN_LIMIT`] names of the project root, and short prefixes
//! of Git's own `HEAD`, `packed-refs` and `index`. Missing rules files and agent sign-in are not inputs.
//!
//! Every path in the answer is relative (to the project root, or to the Git common folder or the state folder for
//! a map file) and written with `/`. The text is plain English for a person: what was found, why when it is a
//! problem, and one next step that exists today. A step this app cannot do yet says so instead of offering it.
//!
//! State words. Wiki: `found`, `detected`, `ambiguous`, `none`, `configured-missing`, `invalid`, `link`,
//! `not-folder`, `permission-denied`, `unreadable`, `settings-unreadable`. Impact Map: `no-git`, `no-commit`,
//! `no-tracked-files`, `ready`, `map-found`, `unknown`. Jev: `off`, `on`, `no-provider`, `unknown-provider`,
//! `invalid`, `settings-unreadable`.

mod impact;
mod locate;
mod settings;
mod wiki;

pub use settings::{
    project_tools_apply, project_tools_apply_in, project_tools_plan, project_tools_plan_in,
};

use super::WorkflowError;
use super::ops::{ConfigPathPhase, read_config_source};
use crate::app::Ctx;
use crate::config::{ProjectConfig, Table, Value};
use crate::dto::{
    ProjectToolDto, ProjectToolIdDto, ProjectToolsDto, ToolPathDto, WorkflowProjectReq,
};
use crate::error::{Coded, Error};
use crate::paths::{self, Project, ProjectKind};
use locate::Git;
use std::path::Path;

/// The settings file the cards name.
const SETTINGS_FILE: &str = ".oah/config.toml";
/// The one provider the source CLI's `[rerank]` knows (`src/oah/rerank/__init__.py` `PROVIDERS`).
const JEV_PROVIDER: &str = "typesafe";
/// The longest setting value a card repeats.
const SHOWN_VALUE_LIMIT: usize = 80;

/// `.oah/config.toml` as the cards read it: the parsed file, or the code of why it cannot be read.
type Settings = Result<ProjectConfig, &'static str>;

/// `project_tools_status`: the three cards of the open project. `OAH_PROJECT_NOT_OPEN` without one, and
/// `OAH_PROJECT_CHANGED` when `req.project_id` is not the open project's id.
pub fn project_tools_status(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq,
) -> Result<ProjectToolsDto, Error> {
    let open = ctx.session.project().ok_or(WorkflowError::NoProject)?;
    if open.id != req.project_id {
        return Err(WorkflowError::ProjectChanged.into());
    }
    Ok(tools_status_at(&open.project, &open.id))
}

/// `oah project-tools status`: the three cards of the project `folder` belongs to, found without starting Git or
/// any other program (`locate`), so it works where Git is not installed. It neither reads nor changes the
/// session's open project. `OAH_PROJECT_MISSING` or `OAH_PROJECT_NOT_FOLDER` when `folder` is not a folder.
pub fn project_tools_status_in(ctx: &Ctx<'_>, folder: &Path) -> Result<ProjectToolsDto, Error> {
    let (root, git) = locate::discover(ctx.env, folder)?;
    let state = match &git {
        Git::WorkTree { common_dir, .. } => Some(
            match ctx
                .env
                .var("OAH_STATE_DIR")
                .filter(|value| !value.is_empty())
            {
                Some(value) if Path::new(value).is_absolute() => Path::new(value).to_path_buf(),
                Some(value) => ctx.env.cwd().join(value),
                None => common_dir.join("oah"),
            },
        ),
        _ => None,
    };
    Ok(cards(
        &root,
        &git,
        state.as_deref(),
        &paths::folder_id(&root),
    ))
}

/// The three cards of the located `project`, read as the module describes. Never fails: what cannot be read is
/// that card's state. A Git project's files must agree with what Git reported when it was located.
pub fn tools_status_at(project: &Project, project_id: &str) -> ProjectToolsDto {
    let (git, state) = match &project.kind {
        ProjectKind::Folder { .. } => (Git::None, None),
        ProjectKind::Git { common_dir } => (
            locate::confirmed(&project.root, common_dir),
            project.state_dir.parent(),
        ),
    };
    cards(&project.root, &git, state, project_id)
}

fn cards(root: &Path, git: &Git, state: Option<&Path>, project_id: &str) -> ProjectToolsDto {
    let settings: Settings =
        read_config_source(root, ConfigPathPhase::BeforeWrite).map_err(|error| error.code());
    ProjectToolsDto {
        project_id: project_id.to_string(),
        tools: vec![
            wiki::card(root, &settings),
            impact::card(root, git, state),
            jev_card(&settings),
        ],
    }
}

/// One card. `reason` is why, for a problem state.
struct Card {
    state: &'static str,
    status: String,
    reason: Option<String>,
    next_step: String,
    paths: Vec<ToolPathDto>,
    notes: Vec<String>,
}

impl Card {
    fn new(state: &'static str, status: impl Into<String>, next_step: impl Into<String>) -> Card {
        Card {
            state,
            status: status.into(),
            reason: None,
            next_step: next_step.into(),
            paths: Vec::new(),
            notes: Vec::new(),
        }
    }

    fn reason(mut self, reason: impl Into<String>) -> Card {
        self.reason = Some(reason.into());
        self
    }

    fn path(mut self, path: ToolPathDto) -> Card {
        self.paths.push(path);
        self
    }

    fn note(mut self, note: impl Into<String>) -> Card {
        self.notes.push(note.into());
        self
    }

    fn into_dto(self, id: ProjectToolIdDto) -> ProjectToolDto {
        ProjectToolDto {
            id,
            name: String::from(match id {
                ProjectToolIdDto::Wiki => "Project Wiki",
                ProjectToolIdDto::ImpactMap => "Impact Map",
                ProjectToolIdDto::Jev => "Jev",
            }),
            state: self.state.to_string(),
            status: self.status,
            reason: self.reason,
            next_step: self.next_step,
            paths: self.paths,
            notes: self.notes,
        }
    }
}

/// The card of a tool whose setting cannot be known because the settings file cannot be read.
fn settings_unreadable(code: &str, unknown: &str) -> Card {
    Card::new(
        "settings-unreadable",
        format!("The project settings cannot be read, so {unknown} is unknown."),
        format!("Fix {SETTINGS_FILE}, then check again."),
    )
    .reason(format!("{SETTINGS_FILE} could not be read ({code})."))
}

/// A setting value as a card repeats it: one line, at most [`SHOWN_VALUE_LIMIT`] characters.
fn shown(value: &str) -> String {
    let mut text: String = value
        .chars()
        .filter(|c| !c.is_control())
        .take(SHOWN_VALUE_LIMIT)
        .collect();
    if value.chars().count() > SHOWN_VALUE_LIMIT {
        text.push('…');
    }
    text
}

/// One key of one top-level table.
enum Setting<'a> {
    /// The table or the key is absent.
    Unset,
    Set(&'a Value),
    /// `[table]` is not a table.
    NotTable,
}

fn setting<'a>(values: &'a Table, table: &str, key: &str) -> Setting<'a> {
    match values.get(table) {
        None => Setting::Unset,
        Some(value) => match value.as_table() {
            Some(found) => found.get(key).map_or(Setting::Unset, Setting::Set),
            None => Setting::NotTable,
        },
    }
}

/// Jev: the project's effective `[rerank]` setting with the source CLI's defaults (`enabled = false`,
/// `provider = ""`), and nothing else. No key is looked for and nothing is sent.
fn jev_card(settings: &Settings) -> ProjectToolDto {
    const SETTING_ONLY: &str =
        "This is the project setting only. OAH did not look for a key or contact TypeSafe.";
    let invalid = |what: &str| {
        Card::new(
            "invalid",
            "The Jev setting cannot be used.",
            format!(
                "Under [rerank] in {SETTINGS_FILE}, set enabled to true or false and provider to \
                 \"{JEV_PROVIDER}\". Then check again."
            ),
        )
        .reason(what.to_string())
    };
    let card = match settings {
        Err(code) => settings_unreadable(code, "the Jev setting"),
        Ok(config) => {
            let values = config.file().values();
            let enabled = match setting(values, "rerank", "enabled") {
                Setting::Unset => Ok(false),
                Setting::Set(value) => value
                    .as_bool()
                    .ok_or("enabled under [rerank] is not true or false."),
                Setting::NotTable => Err("rerank is not a table."),
            };
            let provider = match setting(values, "rerank", "provider") {
                Setting::Unset => Ok(""),
                Setting::Set(value) => value.as_str().ok_or("provider under [rerank] is not text."),
                Setting::NotTable => Err("rerank is not a table."),
            };
            match (enabled, provider) {
                (Err(what), _) | (_, Err(what)) => invalid(what),
                (Ok(false), _) => Card::new(
                    "off",
                    "Jev is off for this project.",
                    format!(
                        "No action needed. Jev stays off unless enabled = true is set under [rerank] in \
                         {SETTINGS_FILE}. Turning it on from this app is not available yet."
                    ),
                )
                .note(SETTING_ONLY),
                (Ok(true), Ok("")) => Card::new(
                    "no-provider",
                    "Jev is switched on, but no provider is set, so it does not run.",
                    format!(
                        "Under [rerank] in {SETTINGS_FILE}, set provider = \"{JEV_PROVIDER}\", or set \
                         enabled = false. Then check again."
                    ),
                )
                .note(SETTING_ONLY),
                (Ok(true), Ok(JEV_PROVIDER)) => {
                    let impact = match setting(values, "impact", "rerank") {
                        Setting::Set(value) => value.as_bool(),
                        Setting::Unset => Some(false),
                        Setting::NotTable => None,
                    };
                    Card::new(
                        "on",
                        format!("Jev is turned on in this project's settings (provider: {JEV_PROVIDER})."),
                        "No action needed. A connection test from this app is not available yet.",
                    )
                    .note(SETTING_ONLY)
                    .note(match impact {
                        Some(true) => "Re-ordering Impact Map results is switched on (rerank under [impact]).",
                        Some(false) => "Re-ordering Impact Map results is switched off (rerank under [impact]).",
                        None => "The rerank setting under [impact] could not be read.",
                    })
                }
                (Ok(true), Ok(other)) => Card::new(
                    "unknown-provider",
                    "Jev is switched on with a provider OAH does not know, so it does not run.",
                    format!(
                        "Under [rerank] in {SETTINGS_FILE}, set provider = \"{JEV_PROVIDER}\", or set \
                         enabled = false. Then check again."
                    ),
                )
                .reason(format!("provider is set to \"{}\".", shown(other)))
                .note(SETTING_ONLY),
            }
        }
    };
    card.into_dto(ProjectToolIdDto::Jev)
}
