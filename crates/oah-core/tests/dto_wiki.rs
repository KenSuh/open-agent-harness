// 프로젝트 위키 DTO 표본 검사: 고정한 값을 직렬화해 tests/dto_samples/wiki 의 JSON 과 바이트 단위로 맞추고, 요청 표본은 그대로 읽히는지 본다.
use oah_core::ErrorDto;
use oah_core::dto::{
    ProjectWikiDto, WikiActionKind, WikiActionReq, WikiApplyDto, WikiApplyReq, WikiBackupDto,
    WikiBackupState, WikiConfiguredDto, WikiCountsDto, WikiFileAction, WikiPlanDto,
    WikiPlanFileDto, WikiPlanMode, WikiProblemDto, WikiSearchDto, WikiSearchReq,
    WikiSettingChangeDto, WikiSettingKeptDto, WikiSettingValue, WikiSettingsPlanDto,
    WikiSettingsResultDto, WikiSettingsState, WikiTargetDto, WikiTargetState, WikiUpgradeDto,
    WorkflowProjectReq,
};
use oah_core::project_tools::wiki::{
    ApplyItem, CopyBlock, CopyBlocked, ItemStatus, LinkNote, SkipReason, WikiCandidate,
    WikiSearchHit, WikiSkip,
};
use serde::Serialize;
use serde::de::DeserializeOwned;
use std::fmt::Debug;
use std::path::PathBuf;

const PROJECT: &str = "3f2a9c1d5e7b8a60";
const PLAN: &str = "sha256:5d41402abc4b2a76b9719d911017c592ae2f8d1c7b3e4f5a6b7c8d9e0f1a2b3c";
const FINGERPRINT: &str = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";

fn sample_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("dto_samples")
        .join("wiki")
        .join(format!("{name}.json"))
}

/// The pretty JSON of `value`, with a final newline, must be the sample file byte for byte.
fn assert_sample(name: &str, value: &impl Serialize) {
    let actual = serde_json::to_string_pretty(value).unwrap() + "\n";
    let path = sample_path(name);
    let expected = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("{}: {error}\nactual:\n{actual}", path.display()));
    assert_eq!(actual, expected, "{} differs", path.display());
}

/// A request sample must deserialize into `T`, equal `value`, and serialize back to the same bytes.
fn assert_request<T: Serialize + DeserializeOwned + PartialEq + Debug>(name: &str, value: T) {
    let text = std::fs::read_to_string(sample_path(name)).unwrap();
    let parsed: T = serde_json::from_str(&text).unwrap();
    assert_eq!(parsed, value);
    assert_sample(name, &parsed);
}

fn text(value: &str) -> Option<WikiSettingValue> {
    Some(WikiSettingValue::Text(value.to_string()))
}

fn candidate(dir: &str, standard: bool, obsidian: bool, index: bool) -> WikiCandidate {
    WikiCandidate {
        dir: dir.to_string(),
        standard,
        obsidian,
        index,
        log: false,
    }
}

fn item(path: &str, status: ItemStatus) -> ApplyItem {
    ApplyItem {
        path: path.to_string(),
        status,
    }
}

#[test]
fn overview_samples() {
    assert_sample(
        "overview_choose",
        &ProjectWikiDto {
            project_id: PROJECT.to_string(),
            default_dir: String::from("docs/wiki"),
            configured: Some(WikiConfiguredDto {
                dir: String::from("docs/wiki"),
                explicit: false,
            }),
            settings_error: None,
            candidates: vec![
                candidate("wiki", true, false, true),
                candidate("notes", false, true, false),
            ],
            problems: vec![WikiProblemDto {
                dir: String::from("knowledge/wiki"),
                error: ErrorDto {
                    code: String::from("OAH_WIKI_PATH_UNSAFE"),
                    message: String::from(
                        "knowledge/wiki is not a real folder or file of this project (a link, a junction, a name \
                         that differs only by case, or something that changed while it was read); nothing was \
                         written through it",
                    ),
                    hint: Some(String::from(
                        "Use a real folder inside the project, not a link, then check again.",
                    )),
                },
            }],
            truncated: false,
            target: WikiTargetDto {
                state: WikiTargetState::Choose,
                dir: None,
                creates: 0,
                sets_dir: false,
                error: None,
            },
        },
    );
    assert_sample(
        "overview_none",
        &ProjectWikiDto {
            project_id: PROJECT.to_string(),
            default_dir: String::from("docs/wiki"),
            configured: Some(WikiConfiguredDto {
                dir: String::from("docs/wiki"),
                explicit: false,
            }),
            settings_error: None,
            candidates: Vec::new(),
            problems: Vec::new(),
            truncated: false,
            target: WikiTargetDto {
                state: WikiTargetState::None,
                dir: Some(String::from("docs/wiki")),
                creates: 5,
                sets_dir: false,
                error: None,
            },
        },
    );
    assert_sample(
        "overview_ready",
        &ProjectWikiDto {
            project_id: PROJECT.to_string(),
            default_dir: String::from("docs/wiki"),
            configured: Some(WikiConfiguredDto {
                dir: String::from("notes"),
                explicit: true,
            }),
            settings_error: None,
            candidates: vec![candidate("notes", false, true, true)],
            problems: Vec::new(),
            truncated: false,
            target: WikiTargetDto {
                state: WikiTargetState::Ready,
                dir: Some(String::from("notes")),
                creates: 0,
                sets_dir: false,
                error: None,
            },
        },
    );
}

fn file(path: &str, action: WikiFileAction, bytes: Option<u64>) -> WikiPlanFileDto {
    WikiPlanFileDto {
        path: path.to_string(),
        action,
        bytes,
    }
}

#[test]
fn plan_samples() {
    assert_sample(
        "plan_setup_adopt",
        &WikiPlanDto {
            project_id: PROJECT.to_string(),
            plan_id: PLAN.to_string(),
            action: WikiActionKind::Setup,
            dir: String::from("notes"),
            source: None,
            mode: WikiPlanMode::Adopt,
            today: Some(String::from("2026-01-02")),
            folders: vec![String::from("notes/templates")],
            files: vec![
                file("index.md", WikiFileAction::Keep, None),
                file("log.md", WikiFileAction::Create, None),
                file("wiki-guide.md", WikiFileAction::Create, None),
                file("templates/note.md", WikiFileAction::Create, None),
                file("templates/decision.md", WikiFileAction::Create, None),
            ],
            writes: 4,
            blocked: Vec::new(),
            blocked_total: 0,
            left: Vec::new(),
            unresolved_links: Vec::new(),
            unresolved_total: 0,
            settings: Some(WikiSettingsPlanDto {
                config_path: String::from(".oah/config.toml"),
                changes: vec![WikiSettingChangeDto {
                    key: String::from("wiki.dir"),
                    before: None,
                    after: text("notes"),
                }],
                kept: Vec::new(),
                upgrade: Some(WikiUpgradeDto {
                    message: oah_core::config::UPGRADE_WARNING.to_string(),
                    fingerprint: FINGERPRINT.to_string(),
                }),
                backup: Some(WikiBackupDto {
                    state: WikiBackupState::Write,
                    path: String::from(".oah/config.toml.v0.4.bak"),
                }),
            }),
        },
    );
    assert_sample(
        "plan_copy",
        &WikiPlanDto {
            project_id: PROJECT.to_string(),
            plan_id: PLAN.to_string(),
            action: WikiActionKind::Copy,
            dir: String::from("docs/wiki"),
            source: Some(String::from("notes")),
            mode: WikiPlanMode::Copy,
            today: None,
            folders: vec![
                String::from("docs"),
                String::from("docs/wiki"),
                String::from("docs/wiki/deep"),
            ],
            files: vec![
                file("a.md", WikiFileAction::Same, Some(10)),
                file("b.md", WikiFileAction::Copy, Some(2048)),
                file("deep/c.md", WikiFileAction::Copy, Some(3_500_000)),
            ],
            writes: 2,
            blocked: vec![CopyBlocked {
                path: String::from("link.md"),
                reason: CopyBlock::LinkLeavesWiki,
                line: Some(7),
                target: Some(String::from("../README.md")),
            }],
            blocked_total: 1,
            left: vec![String::from(".obsidian/workspace.json")],
            unresolved_links: vec![LinkNote {
                file: String::from("a.md"),
                line: 3,
                target: String::from("missing-note"),
            }],
            unresolved_total: 1,
            settings: Some(WikiSettingsPlanDto {
                config_path: String::from(".oah/config.toml"),
                changes: vec![WikiSettingChangeDto {
                    key: String::from("wiki.dir"),
                    before: text("notes"),
                    after: text("docs/wiki"),
                }],
                kept: vec![WikiSettingKeptDto {
                    key: String::from("impact.wiki_dirs"),
                    value: WikiSettingValue::List(vec![String::from("notes"), String::from("adr")]),
                    reason: String::from("explicit-wiki-dirs"),
                }],
                upgrade: None,
                backup: None,
            }),
        },
    );
}

fn settings_result(state: WikiSettingsState, error: Option<ErrorDto>) -> WikiSettingsResultDto {
    WikiSettingsResultDto {
        state,
        config_path: String::from(".oah/config.toml"),
        upgraded: false,
        backup: None,
        error,
    }
}

#[test]
fn apply_samples() {
    let request = WikiApplyReq {
        request: WikiActionReq::Setup {
            selected: Some(String::from("notes")),
            today: Some(String::from("2026-01-02")),
        },
        plan_id: PLAN.to_string(),
        approve_upgrade: Some(FINGERPRINT.to_string()),
    };
    assert_request(
        "apply_req_setup",
        WorkflowProjectReq {
            project_id: PROJECT.to_string(),
            input: request,
        },
    );
    assert_request(
        "apply_req_copy",
        WorkflowProjectReq {
            project_id: PROJECT.to_string(),
            input: WikiApplyReq {
                request: WikiActionReq::Copy {
                    source: String::from("notes"),
                },
                plan_id: PLAN.to_string(),
                approve_upgrade: None,
            },
        },
    );
    assert_sample(
        "apply_done",
        &WikiApplyDto {
            project_id: PROJECT.to_string(),
            plan_id: PLAN.to_string(),
            action: WikiActionKind::Setup,
            dir: String::from("notes"),
            source: None,
            folders_created: vec![String::from("notes/templates")],
            items: vec![
                item("index.md", ItemStatus::Kept),
                item("log.md", ItemStatus::Created),
                item("wiki-guide.md", ItemStatus::Created),
                item("templates/note.md", ItemStatus::Created),
                item("templates/decision.md", ItemStatus::Created),
            ],
            counts: WikiCountsDto {
                created: 4,
                kept: 1,
                failed: 0,
                pending: 0,
            },
            stop: None,
            settings: WikiSettingsResultDto {
                upgraded: true,
                backup: Some(String::from(".oah/config.toml.v0.4.bak")),
                ..settings_result(WikiSettingsState::Saved, None)
            },
            complete: true,
        },
    );
    assert_sample(
        "apply_stopped",
        &WikiApplyDto {
            project_id: PROJECT.to_string(),
            plan_id: PLAN.to_string(),
            action: WikiActionKind::Copy,
            dir: String::from("docs/wiki"),
            source: Some(String::from("notes")),
            folders_created: vec![String::from("docs"), String::from("docs/wiki")],
            items: vec![
                item("a.md", ItemStatus::Created),
                item("b.md", ItemStatus::Pending),
                item("deep/c.md", ItemStatus::Pending),
            ],
            counts: WikiCountsDto {
                created: 1,
                kept: 0,
                failed: 0,
                pending: 2,
            },
            stop: Some(ErrorDto {
                code: String::from("OAH_WIKI_STOPPED"),
                message: String::from(
                    "stopped before the next file; the files already written stay and the rest was not started",
                ),
                hint: Some(String::from(
                    "Preview again to finish; the files already written are kept.",
                )),
            }),
            settings: settings_result(WikiSettingsState::Skipped, None),
            complete: false,
        },
    );
    assert_sample(
        "apply_settings_stale",
        &WikiApplyDto {
            project_id: PROJECT.to_string(),
            plan_id: PLAN.to_string(),
            action: WikiActionKind::Copy,
            dir: String::from("docs/wiki"),
            source: Some(String::from("notes")),
            folders_created: Vec::new(),
            items: vec![item("a.md", ItemStatus::Created)],
            counts: WikiCountsDto {
                created: 1,
                kept: 0,
                failed: 0,
                pending: 0,
            },
            stop: None,
            settings: settings_result(
                WikiSettingsState::Stale,
                Some(ErrorDto {
                    code: String::from("OAH_PLAN_STALE"),
                    message: String::from(
                        "~/project/.oah/config.toml: the approved plan is not the plan for this project and file \
                         now; make a new plan and approve it; nothing was written",
                    ),
                    hint: Some(String::from(
                        "Files changed after the plan was made. Review the new plan, then apply it again.",
                    )),
                }),
            ),
            complete: false,
        },
    );
}

#[test]
fn search_samples() {
    assert_request(
        "search_req",
        WorkflowProjectReq {
            project_id: PROJECT.to_string(),
            input: WikiSearchReq {
                dir: Some(String::from("notes")),
                query: String::from("release plan"),
                max_hits: Some(50),
            },
        },
    );
    assert_sample(
        "search",
        &WikiSearchDto {
            project_id: PROJECT.to_string(),
            dir: String::from("notes"),
            hits: vec![
                WikiSearchHit {
                    file: String::from("decisions/2026-release.md"),
                    line: 12,
                    heading: Some(String::from("Release plan")),
                    context: String::from("The release plan moves to the first week of March."),
                },
                WikiSearchHit {
                    file: String::from("index.md"),
                    line: 4,
                    heading: None,
                    context: String::from("- [[2026-release]] release plan"),
                },
            ],
            truncated: true,
            files_searched: 42,
            skipped: vec![WikiSkip {
                path: String::from("attachments/big.md"),
                reason: SkipReason::Oversized,
            }],
            skipped_total: 1,
        },
    );
}
