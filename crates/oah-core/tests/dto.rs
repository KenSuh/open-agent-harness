// DTO 표본 검사: 고정한 값을 직렬화해 tests/dto_samples 의 JSON 과 바이트 단위로 맞추고, 요청 표본은 그대로 읽히는지 본다.
use oah_core::dto::{
    AppInfoDto, ApplyReportDto, BasisDto, ChangePlanDto, CliDto, CliState, ConfigStateDto,
    ItemResultDto, NoticeDto, PlanItemDto, ProjectDto, ProjectKindDto, RefreshDto, SettingEdit,
    SettingValue, SettingsDto, SettingsSetReq, TaskCancelReq, UiDto,
};
use oah_core::error::codes;
use oah_core::paths::Env;
use oah_core::plan::{Action, ItemStatus, Owner};
use oah_core::tasks::{Progress, TaskError, TaskEvent, TaskKey, TaskRegistry};
use oah_core::{Error, ErrorDto};
use serde::Serialize;
use serde::de::DeserializeOwned;
use std::fmt::Debug;
use std::path::PathBuf;
use std::sync::Arc;

fn sample_path(area: &str, name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("dto_samples")
        .join(area)
        .join(format!("{name}.json"))
}

/// The pretty JSON of `value`, with a final newline, must be the sample file byte for byte.
fn assert_sample(area: &str, name: &str, value: &impl Serialize) {
    let actual = serde_json::to_string_pretty(value).unwrap() + "\n";
    let path = sample_path(area, name);
    let expected = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("{}: {error}\nactual:\n{actual}", path.display()));
    assert_eq!(actual, expected, "{} differs", path.display());
}

/// A request sample must deserialize into `T`, equal `value`, and serialize back to the same bytes.
fn assert_request<T: Serialize + DeserializeOwned + PartialEq + Debug>(
    area: &str,
    name: &str,
    value: T,
) {
    let text = std::fs::read_to_string(sample_path(area, name)).unwrap();
    let parsed: T = serde_json::from_str(&text).unwrap();
    assert_eq!(parsed, value);
    assert_sample(area, name, &parsed);
}

fn env() -> Env {
    Env::new(std::env::temp_dir()).unwrap()
}

/// The notice a start gives when the bundled CLI is missing; `source` is the bundled path as shown.
fn copy_failed_notice(source: &str) -> NoticeDto {
    NoticeDto::warning(
        codes::CLI_COPY_FAILED,
        "cli-copy",
        format!("the CLI copy was not updated (bundled CLI not found): {source}"),
    )
}

#[test]
fn app_info_samples() {
    assert_sample(
        "app",
        "app_info",
        &AppInfoDto {
            version: String::from("0.0.0"),
            os: String::from("macos"),
            oah_home: String::from("~/.oah"),
            cli: Some(CliDto {
                state: CliState::Copied,
                path: String::from("~/.oah/bin/oah"),
                version: Some(String::from("0.0.0")),
            }),
            notices: Vec::new(),
        },
    );
    // On Windows a shown path keeps the platform's separator after `~`.
    assert_sample(
        "app",
        "app_info_kept_previous",
        &AppInfoDto {
            version: String::from("0.0.0"),
            os: String::from("windows"),
            oah_home: String::from(r"~\.oah"),
            cli: Some(CliDto {
                state: CliState::KeptPrevious,
                path: String::from(r"~\.oah\bin\oah.exe"),
                version: None,
            }),
            notices: vec![copy_failed_notice(r"~\dev\oah.exe")],
        },
    );
    assert_sample(
        "app",
        "app_info_before_copy",
        &AppInfoDto {
            version: String::from("0.0.0"),
            os: String::from("linux"),
            oah_home: String::from("~/.oah"),
            cli: None,
            notices: Vec::new(),
        },
    );
}

#[test]
fn settings_samples() {
    assert_sample(
        "app",
        "settings",
        &SettingsDto {
            automation_level: String::from("auto-safe"),
            wiki_index: Default::default(),
            ui: UiDto {
                theme: String::from("dark"),
                quick_start_dismissed: true,
            },
        },
    );
    assert_request(
        "app",
        "settings_set_req",
        SettingsSetReq {
            edits: vec![
                SettingEdit {
                    key: String::from("ui.theme"),
                    value: SettingValue::Text(String::from("dark")),
                },
                SettingEdit {
                    key: String::from("ui.quick_start_dismissed"),
                    value: SettingValue::Bool(true),
                },
                SettingEdit {
                    key: String::from("history.retention_days"),
                    value: SettingValue::Integer(30),
                },
            ],
        },
    );
    assert_sample(
        "app",
        "refresh",
        &RefreshDto {
            projects: 1,
            notices: 2,
        },
    );
}

#[test]
fn task_event_samples() {
    let progress: TaskEvent<RefreshDto> = TaskEvent::Progress(Progress {
        phase: "read-project",
        done: 0,
        total: Some(1),
    });
    assert_sample("task", "event_progress", &progress);
    let done: TaskEvent<RefreshDto> = TaskEvent::Done(RefreshDto {
        projects: 1,
        notices: 0,
    });
    assert_sample("task", "event_done", &done);
    let busy = Error::from(TaskError::Busy {
        kind: "refresh",
        subject: String::new(),
    });
    let failed: TaskEvent<RefreshDto> = TaskEvent::Failed(busy.to_dto(&env()));
    assert_sample("task", "event_failed", &failed);
    let cancelled: TaskEvent<RefreshDto> = TaskEvent::Cancelled;
    assert_sample("task", "event_cancelled", &cancelled);
}

#[test]
fn task_list_and_cancel_samples() {
    let registry = TaskRegistry::new();
    let quiet = Arc::new(|_: &TaskEvent<RefreshDto>| {});
    let started = registry
        .begin(TaskKey::joinable("refresh", ""), quiet)
        .unwrap();
    let oah_core::tasks::Begin::Started(handle) = started else {
        panic!("the first refresh must start");
    };
    oah_core::tasks::ProgressSink::progress(
        &handle,
        Progress {
            phase: "read-project",
            done: 0,
            total: Some(1),
        },
    );
    assert_sample("task", "snapshots", &registry.list());
    assert_request(
        "task",
        "cancel_req",
        TaskCancelReq {
            id: String::from("task-1"),
        },
    );
}

#[test]
fn project_samples() {
    assert_sample(
        "project",
        "git_legacy",
        &ProjectDto {
            id: String::from("3f1c9a0b7d2e4a55"),
            root: String::from("~/code/app"),
            kind: ProjectKindDto::Git,
            config: ConfigStateDto::Legacy,
            notices: vec![NoticeDto::warning(
                "OAH_CONFIG_UNKNOWN_KEY",
                "unknown.x",
                String::from(
                    "~/code/app/.oah/config.toml: unknown.x is not a key OAH reads; it is kept as it is",
                ),
            )],
        },
    );
    assert_sample(
        "project",
        "folder_invalid",
        &ProjectDto {
            id: String::from("9a8b7c6d5e4f3a2b"),
            root: String::from("~/notes"),
            kind: ProjectKindDto::Folder,
            config: ConfigStateDto::Invalid {
                error: ErrorDto {
                    code: String::from("OAH_CONFIG_INVALID"),
                    message: String::from("~/notes/.oah/config.toml: not valid TOML"),
                    hint: oah_core::hint_for("OAH_CONFIG_INVALID").map(str::to_string),
                },
            },
            notices: Vec::new(),
        },
    );
}

#[test]
fn plan_samples() {
    assert_sample(
        "plan",
        "change_plan",
        &ChangePlanDto {
            id: String::from("plan-1"),
            items: vec![
                PlanItemDto {
                    id: String::from("a"),
                    target: String::from("~/code/app/.oah/config.toml"),
                    owner: Owner::Project,
                    action: Action::Update,
                    summary: String::from("Set the build section to codex"),
                    detail: Some(String::from("[stages]\nbuild = \"codex\"\n")),
                },
                PlanItemDto {
                    id: String::from("b"),
                    target: String::from("~/.oah/settings.toml"),
                    owner: Owner::User,
                    action: Action::ReplaceBlock,
                    summary: String::from("Remember the dark theme"),
                    detail: None,
                },
                PlanItemDto {
                    id: String::from("c"),
                    target: String::from("~/.claude/settings.json"),
                    owner: Owner::Agent(String::from("claude")),
                    action: Action::Keep,
                    summary: String::from("Leave the agent settings as they are"),
                    detail: None,
                },
            ],
            warnings: Vec::new(),
            basis: vec![
                BasisDto {
                    target: String::from("~/code/app/.oah/config.toml"),
                    fingerprint: Some(String::from(
                        "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
                    )),
                },
                BasisDto {
                    target: String::from("~/.oah/settings.toml"),
                    fingerprint: None,
                },
            ],
        },
    );
    assert_sample(
        "plan",
        "apply_report",
        &ApplyReportDto {
            items: vec![
                ItemResultDto {
                    id: String::from("a"),
                    target: String::from("~/code/app/.oah/config.toml"),
                    status: ItemStatus::Done,
                },
                ItemResultDto {
                    id: String::from("b"),
                    target: String::from("~/.oah/settings.toml"),
                    status: ItemStatus::Stale,
                },
                ItemResultDto {
                    id: String::from("c"),
                    target: String::from("~/.claude/settings.json"),
                    status: ItemStatus::NotRun,
                },
            ],
            error: Some(ErrorDto {
                code: String::from("OAH_PLAN_STALE"),
                message: String::from(
                    "~/.oah/settings.toml changed after the plan was made; make a new plan",
                ),
                hint: oah_core::hint_for("OAH_PLAN_STALE").map(str::to_string),
            }),
        },
    );
}

#[test]
fn notice_sample() {
    assert_sample(
        "notice",
        "cli_copy_failed",
        &copy_failed_notice("~/dev/oah"),
    );
}

#[test]
fn requests_refuse_unknown_fields() {
    assert!(serde_json::from_str::<TaskCancelReq>(r#"{"id":"task-1","force":true}"#).is_err());
    assert!(serde_json::from_str::<oah_core::dto::Empty>(r#"{"x":1}"#).is_err());
    assert!(serde_json::from_str::<oah_core::dto::Empty>("{}").is_ok());
    assert!(serde_json::from_str::<SettingsSetReq>(r#"{"edits":[{"key":"ui.theme"}]}"#).is_err());
}
