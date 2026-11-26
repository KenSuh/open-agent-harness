// 팀 작업 흐름 시험: 배정 저장 뒤 블록만 바뀌고, 옛 설정은 승인·백업 뒤에만 올라가며, 규칙 거절은 저장된 배정을 남기고, 배너는 정해진 순서다.
#[path = "support/fake_agents.rs"]
mod fake_agents;

use fake_agents::FakeProbe;
use oah_core::agents::{CapabilityState, Kind, Os, Status};
use oah_core::app::{self, Session};
use oah_core::config::{AgentId, UPGRADE_WARNING};
use oah_core::dto::{
    AgentIdDto, AgentStatusDto, AgentsProbeReq, BannerDto, CapabilitiesDto, ConfigFormatDto, Empty,
    IndependenceReq, LoginHintDto, PresetApplyReq, SignInDto, StageChangeReq, StagesSetReq,
    WorkflowProjectReq,
};
use oah_core::paths::{self, Env};
use oah_core::tasks::{CancelToken, NoProgress};
use oah_core::workflow::{self, ConfigPathRefusal, WorkflowError, status};
use oah_core::{Coded, Error};
use std::fs;
use std::path::{Path, PathBuf};

const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
const LEGACY: &str =
    "# team settings\n[stages]\ninitial = \"claude\"\nreview = \"codex\"\ncorrection = \"codex\"\n";

/// A temporary project folder and a session whose home is a sibling folder.
struct Project {
    _dir: tempfile::TempDir,
    root: PathBuf,
    session: Session,
}

impl Project {
    fn new(files: &[(&str, &[u8])]) -> Project {
        let dir = tempfile::tempdir().unwrap();
        let base = paths::canonical_folder(&fs::canonicalize(dir.path()).unwrap()).unwrap();
        let (root, home) = (base.join("project"), base.join("home"));
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&home).unwrap();
        for (relative, bytes) in files {
            let path = root.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, bytes).unwrap();
        }
        let mut env = Env::new(&root).unwrap();
        if let Some(path) = std::env::var_os("PATH") {
            env.set("PATH", path);
        }
        if cfg!(windows) {
            for name in ["SystemRoot", "windir", "ComSpec", "PATHEXT", "TEMP", "TMP"] {
                if let Some(value) = std::env::var_os(name) {
                    env.set(name, value);
                }
            }
        }
        env.set(HOME_VAR, &home);
        env.set("GIT_CONFIG_NOSYSTEM", "1");
        env.set("GIT_CEILING_DIRECTORIES", &base);
        Project {
            _dir: dir,
            root,
            session: Session::new(env),
        }
    }

    fn read(&self, relative: &str) -> Option<Vec<u8>> {
        fs::read(self.root.join(relative)).ok()
    }

    fn text(&self, relative: &str) -> String {
        String::from_utf8(self.read(relative).unwrap()).unwrap()
    }

    fn set(
        &self,
        agents: &[AgentStatusDto],
        req: StagesSetReq,
    ) -> Result<oah_core::dto::StagesSetDto, Error> {
        let cancel = CancelToken::new();
        let ctx = self.session.ctx(&cancel, &NoProgress);
        workflow::set_stages(&ctx, &self.root, agents, req)
    }

    fn view(&self, agents: &[AgentStatusDto]) -> oah_core::dto::WorkflowDto {
        workflow::view(self.session.env(), &self.root, agents).unwrap()
    }

    /// Every file under the project with its bytes, sorted, for a before/after comparison.
    fn tree(&self) -> Vec<(PathBuf, Vec<u8>)> {
        fn walk(dir: &Path, out: &mut Vec<(PathBuf, Vec<u8>)>) {
            for entry in fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    walk(&path, out);
                } else {
                    out.push((path.clone(), fs::read(&path).unwrap()));
                }
            }
        }
        let mut out = Vec::new();
        walk(&self.root, &mut out);
        out.sort();
        out
    }
}

fn agent(id: AgentId, installed: bool, sign_in: SignInDto) -> AgentStatusDto {
    let state = CapabilityState::Unknown;
    AgentStatusDto {
        id: AgentIdDto(id),
        name: id.as_str().to_string(),
        kind: Kind::Cli,
        installed,
        status: match (installed, sign_in) {
            (false, _) => Status::Missing,
            (true, SignInDto::SignedIn) => Status::Ok,
            (true, SignInDto::SignedOut) => Status::NotSignedIn,
            (true, SignInDto::Unknown) => Status::SignInUnknown,
        },
        version: None,
        source: None,
        sign_in,
        opencode: None,
        login_hint: LoginHintDto {
            text: format!("{id} login"),
            is_command: true,
        },
        install_url: String::new(),
        can_take_section: id.can_own_section(),
        capabilities: CapabilitiesDto {
            detect: state,
            sign_in: state,
            skills_link: state,
            native_switch: state,
            model_write: state,
            model_read: state,
            hook_prompt: state,
            pack_update: state,
        },
        problem: None,
        checked_at: String::from("2026-11-21T00:00:00Z"),
    }
}

/// Claude Code and Codex installed and signed in, the others missing.
fn ready_agents() -> Vec<AgentStatusDto> {
    AgentId::ALL
        .into_iter()
        .map(|id| match id {
            AgentId::Claude | AgentId::Codex => agent(id, true, SignInDto::SignedIn),
            _ => agent(id, false, SignInDto::Unknown),
        })
        .collect()
}

fn change(section: &str, agent: AgentId) -> StagesSetReq {
    StagesSetReq {
        stages: vec![StageChangeReq {
            section: section.to_string(),
            agent: AgentIdDto(agent),
        }],
        ..StagesSetReq::default()
    }
}

/// The bytes before the begin marker line and after the end marker (its line ending included in the suffix).
fn outside(bytes: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let text = std::str::from_utf8(bytes).unwrap();
    let begin = text.find("<!-- OAH:RULES:BEGIN v1 -->").unwrap();
    let end = text.find("<!-- OAH:RULES:END -->").unwrap() + "<!-- OAH:RULES:END -->".len();
    (bytes[..begin].to_vec(), bytes[end..].to_vec())
}

#[test]
fn an_assignment_changes_stages_and_only_the_block_keeping_user_bytes_line_ends_and_final_newline()
{
    let project = Project::new(&[(".oah/config.toml", b"[stages]\nplan = \"claude\"\n")]);
    let agents = ready_agents();
    // A first sync puts the block in place; then the user writes text above and below it, CRLF, no final newline.
    project
        .set(&agents, change("plan", AgentId::Claude))
        .unwrap();
    let block = project.text("AGENTS.md");
    let crlf_block = block.trim_end_matches('\n').replace('\n', "\r\n");
    let user = format!(
        "# Hand-written\r\n\r\nKeep this.\r\n{crlf_block}\r\n\r\n## After\r\nTail without newline"
    );
    fs::write(project.root.join("AGENTS.md"), &user).unwrap();
    let before = project.read("AGENTS.md").unwrap();

    let outcome = project
        .set(&agents, change("review", AgentId::Claude))
        .unwrap();
    assert!(outcome.saved && outcome.synced, "{outcome:#?}");
    assert!(
        project
            .text(".oah/config.toml")
            .contains("review = \"claude\"")
    );
    let after = project.read("AGENTS.md").unwrap();
    assert_ne!(after, before, "the block changed");
    assert_eq!(
        outside(&after),
        outside(&before),
        "bytes outside the block are identical"
    );
    assert!(
        !after.ends_with(b"\n"),
        "the missing final newline stays missing"
    );
    let block = String::from_utf8(after).unwrap();
    let inside = &block
        [block.find("<!-- OAH:RULES:BEGIN").unwrap()..block.find("<!-- OAH:RULES:END").unwrap()];
    assert!(
        !inside.replace("\r\n", "").contains('\n'),
        "the block keeps CRLF"
    );
    assert!(inside.contains("| Code Review | Claude Code |"));
}

#[test]
fn a_legacy_project_reads_its_mapping_writes_nothing_and_upgrades_only_with_approval_and_a_backup()
{
    let project = Project::new(&[(".oah/config.toml", LEGACY.as_bytes())]);
    let agents = ready_agents();
    let before = project.tree();
    let view = project.view(&agents);
    assert_eq!(view.config_format, ConfigFormatDto::Legacy);
    assert_eq!(view.upgrade_warning.as_deref(), Some(UPGRADE_WARNING));
    let sources: Vec<(&str, &str)> = view
        .sections
        .iter()
        .map(|s| (s.id.as_str(), s.source.as_str()))
        .collect();
    assert_eq!(
        sources,
        [
            ("plan", "legacy:initial"),
            ("build", "legacy:initial"),
            ("review", "file"),
            ("fix", "legacy:correction")
        ]
    );
    assert_eq!(project.tree(), before, "opening and showing wrote nothing");

    for approval in [None, Some(String::from("0").repeat(64))] {
        let req = StagesSetReq {
            approve_upgrade: approval,
            ..change("review", AgentId::Claude)
        };
        let error = project.set(&agents, req).unwrap_err();
        assert_eq!(error.code(), "OAH_CONFIG_UPGRADE_REQUIRED");
        assert_eq!(project.tree(), before, "a refused upgrade wrote nothing");
    }

    let req = StagesSetReq {
        approve_upgrade: view.config_sha256.clone(),
        ..change("review", AgentId::Claude)
    };
    let outcome = project.set(&agents, req).unwrap();
    assert!(
        outcome.saved && outcome.upgraded && outcome.synced,
        "{outcome:#?}"
    );
    assert_eq!(outcome.backup.as_deref(), Some(".oah/config.toml.v0.4.bak"));
    assert_eq!(
        project.read(".oah/config.toml.v0.4.bak").unwrap(),
        LEGACY.as_bytes()
    );
    let config = project.text(".oah/config.toml");
    assert!(
        config.contains("review = \"claude\"") && !config.contains("initial"),
        "{config}"
    );
    assert_eq!(
        project.view(&agents).config_format,
        ConfigFormatDto::Current
    );
}

#[test]
fn a_rules_refusal_after_a_saved_config_keeps_the_assignment_and_offers_sync_now() {
    let big = format!("# Notes\n{}\n", "x".repeat(40 * 1024));
    let project = Project::new(&[
        (".oah/config.toml", b"[stages]\nplan = \"claude\"\n"),
        ("AGENTS.md", big.as_bytes()),
    ]);
    let agents = ready_agents();
    let outcome = project
        .set(&agents, change("fix", AgentId::Claude))
        .unwrap();
    assert!(outcome.saved);
    assert!(!outcome.synced);
    assert_eq!(
        outcome.sync_error.as_ref().map(|e| e.code.as_str()),
        Some("OAH_RULES_BUDGET")
    );
    assert_eq!(outcome.actions, ["rules_sync"]);
    assert!(
        project
            .text(".oah/config.toml")
            .contains("fix = \"claude\""),
        "the assignment stays saved"
    );
    assert_eq!(
        project.read("AGENTS.md").unwrap(),
        big.as_bytes(),
        "AGENTS.md is untouched"
    );
    assert!(
        project.read("CLAUDE.md").is_none(),
        "a refused plan writes no file at all"
    );
    let notice = outcome
        .workflow
        .notices
        .iter()
        .find(|n| n.code == "OAH_RULES_NOT_SYNCED")
        .unwrap();
    assert_eq!(notice.actions, ["rules_sync"]);
    assert!(notice.message.contains("OAH_RULES_BUDGET"));
    assert!(!outcome.workflow.rules.synced);
    assert_eq!(
        outcome.workflow.rules.error.as_ref().unwrap().code,
        "OAH_RULES_BUDGET"
    );
}

#[cfg(unix)]
#[test]
fn a_linked_local_rules_input_is_refused_consistently_across_read_check_save_and_sync() {
    use std::os::unix::fs::symlink;

    // 1. Leaf symlink under a real `.oah` directory: read, check, save, and sync all report OAH_RULES_LINK,
    // and a valid config save keeps the saved assignment with the existing retry notice.
    let project = Project::new(&[
        (".oah/config.toml", b"[stages]\nplan = \"claude\"\n"),
        ("inert.md", b"inert fixture\n"),
    ]);
    symlink(
        project.root.join("inert.md"),
        project.root.join(".oah/rules.local.md"),
    )
    .unwrap();
    let cancel = CancelToken::new();
    let ctx = project.session.ctx(&cancel, &NoProgress);
    let opened = app::project_open(&ctx, &project.root).unwrap();

    let shown = app::sections_show(&ctx, Empty {}).unwrap();
    assert!(!shown.rules.synced);
    assert_eq!(
        shown.rules.error.as_ref().map(|e| e.code.as_str()),
        Some("OAH_RULES_LINK")
    );
    assert!(
        !shown
            .rules
            .error
            .as_ref()
            .unwrap()
            .message
            .contains("inert.md")
    );
    let checked = app::rules_check(&ctx, Empty {}).unwrap();
    assert_eq!(checked, shown.rules);
    assert!(!project.root.join("AGENTS.md").exists());
    assert!(!project.root.join("CLAUDE.md").exists());

    let sync_err = app::rules_sync(&ctx, Empty {}).unwrap_err();
    assert_eq!(sync_err.code(), "OAH_RULES_LINK");
    assert!(!sync_err.message().contains("inert.md"));
    assert!(!project.root.join("AGENTS.md").exists());
    assert!(!project.root.join("CLAUDE.md").exists());

    let saved = app::stages_set(
        &ctx,
        WorkflowProjectReq {
            project_id: opened.id,
            input: change("fix", AgentId::Claude),
        },
    )
    .unwrap();
    assert!(saved.saved);
    assert!(!saved.synced);
    assert_eq!(
        saved.sync_error.as_ref().map(|e| e.code.as_str()),
        Some("OAH_RULES_LINK")
    );
    assert_eq!(saved.actions, ["rules_sync"]);
    assert!(
        project
            .text(".oah/config.toml")
            .contains("fix = \"claude\""),
        "the saved assignment stays intact"
    );
    let notice = saved
        .workflow
        .notices
        .iter()
        .find(|n| n.code == "OAH_RULES_NOT_SYNCED")
        .unwrap();
    assert_eq!(notice.actions, ["rules_sync"]);
    assert!(notice.message.contains("OAH_RULES_LINK"));
    assert_eq!(
        saved.workflow.rules.error.as_ref().map(|e| e.code.as_str()),
        Some("OAH_RULES_LINK")
    );
    assert!(!project.root.join("AGENTS.md").exists());
    assert!(!project.root.join("CLAUDE.md").exists());

    // 2. Traversed `.oah` ancestor symlink: read, check, and sync all report OAH_RULES_LINK without writing.
    let ancestor_project = Project::new(&[]);
    let inert_dir = ancestor_project.root.join("inert-dir");
    fs::create_dir(&inert_dir).unwrap();
    symlink(&inert_dir, ancestor_project.root.join(".oah")).unwrap();
    let ctx = ancestor_project.session.ctx(&cancel, &NoProgress);
    app::project_open(&ctx, &ancestor_project.root).unwrap();

    let shown = app::sections_show(&ctx, Empty {}).unwrap();
    assert!(!shown.rules.synced);
    assert_eq!(
        shown.rules.error.as_ref().map(|e| e.code.as_str()),
        Some("OAH_RULES_LINK")
    );
    assert!(
        !shown
            .rules
            .error
            .as_ref()
            .unwrap()
            .message
            .contains("inert-dir")
    );
    let checked = app::rules_check(&ctx, Empty {}).unwrap();
    assert_eq!(checked, shown.rules);
    let sync_err = app::rules_sync(&ctx, Empty {}).unwrap_err();
    assert_eq!(sync_err.code(), "OAH_RULES_LINK");
    assert!(!sync_err.message().contains("inert-dir"));
    assert!(!ancestor_project.root.join("AGENTS.md").exists());
    assert!(!ancestor_project.root.join("CLAUDE.md").exists());
}

#[test]
fn refused_saves_write_nothing_cursor_unknown_sections_and_unlisted_independence_ids() {
    let project = Project::new(&[(".oah/config.toml", b"[stages]\nplan = \"claude\"\n")]);
    let agents = ready_agents();
    let before = project.tree();
    let cases = [
        (change("review", AgentId::Cursor), "OAH_AGENT_DEPLOY_ONLY"),
        (change("docs", AgentId::Claude), "OAH_SECTION_UNKNOWN"),
        (
            StagesSetReq {
                independent_of: vec![IndependenceReq {
                    section: String::from("review"),
                    of: vec![String::from("nowhere")],
                }],
                ..StagesSetReq::default()
            },
            "OAH_SECTION_UNKNOWN",
        ),
    ];
    for (req, code) in cases {
        assert_eq!(project.set(&agents, req).unwrap_err().code(), code);
        assert_eq!(project.tree(), before);
    }
    let view = project.view(&agents);
    assert!(
        view.agents
            .iter()
            .all(|option| option.id.0 != AgentId::Cursor),
        "Cursor is never offered"
    );
    let order: Vec<AgentId> = view.agents.iter().map(|option| option.id.0).collect();
    assert_eq!(
        order[..2],
        [AgentId::Claude, AgentId::Codex],
        "installed agents first"
    );
    assert!(
        view.agents[2..]
            .iter()
            .all(|option| option.disabled_reason.is_some())
    );
}

#[test]
fn a_fifth_section_in_config_appears_as_a_card_and_a_block_row_without_code() {
    let config = "[sections]\norder = [\"plan\", \"build\", \"review\", \"fix\", \"docs\"]\n[sections.docs]\nlabel = \"Write Docs\"\n[stages]\ndocs = \"claude\"\n";
    let project = Project::new(&[(".oah/config.toml", config.as_bytes())]);
    let agents = ready_agents();
    let view = project.view(&agents);
    let ids: Vec<&str> = view.sections.iter().map(|s| s.id.as_str()).collect();
    assert_eq!(ids, ["plan", "build", "review", "fix", "docs"]);
    assert_eq!(view.sections[4].label, "Write Docs");
    assert_eq!(view.sections[4].agent, Some(AgentIdDto(AgentId::Claude)));
    project
        .set(&agents, change("docs", AgentId::Codex))
        .unwrap();
    assert!(project.text("AGENTS.md").contains("| Write Docs | Codex |"));
    let advanced = project.view(&agents).advanced;
    assert!(
        advanced.config_tables.contains("docs = \"codex\""),
        "{}",
        advanced.config_tables
    );
    assert!(advanced.block.unwrap().contains("| Write Docs | Codex |"));
}

#[test]
fn thin_files_follow_detection_and_keep_user_content_inside_the_project() {
    let project = Project::new(&[
        (".oah/config.toml", b"[stages]\nplan = \"claude\"\n"),
        ("CLAUDE.md", b"# My notes\r\nKeep me.\r\n"),
        (".cursor/rules/oah.mdc", b"user rule\n"),
    ]);
    let mut agents = ready_agents();
    // Gemini CLI detected: GEMINI.md is created; Cursor absent: the existing .mdc is left alone.
    agents.retain(|a| a.id.0 != AgentId::Gemini);
    agents.push(agent(AgentId::Gemini, true, SignInDto::SignedIn));
    project
        .set(&agents, change("plan", AgentId::Claude))
        .unwrap();
    assert_eq!(
        project.read("CLAUDE.md").unwrap(),
        b"# My notes\r\nKeep me.\r\n@AGENTS.md\r\n"
    );
    assert_eq!(project.read("GEMINI.md").unwrap(), b"@AGENTS.md\n");
    assert_eq!(
        project.read(".cursor/rules/oah.mdc").unwrap(),
        b"user rule\n"
    );
    // Cursor detected: OAH's rule replaces the .mdc; nothing is removed.
    agents.retain(|a| a.id.0 != AgentId::Cursor);
    agents.push(agent(AgentId::Cursor, true, SignInDto::Unknown));
    project
        .set(&agents, change("plan", AgentId::Claude))
        .unwrap();
    assert!(
        project
            .text(".cursor/rules/oah.mdc")
            .starts_with("---\ndescription: OAH team workflow\n")
    );
    assert!(project.read("GEMINI.md").is_some());
    let rules = project.view(&agents).rules;
    assert!(rules.synced && rules.drift.is_empty(), "{rules:#?}");
}

#[test]
fn an_ineligible_cursor_path_leaves_workflow_and_immediate_rules_available() {
    let project = Project::new(&[("GEMINI.md", b"user text\n")]);
    let cursor = project.root.join(".cursor/rules/oah.mdc");
    fs::create_dir_all(&cursor).unwrap();
    let cancel = CancelToken::new();
    let ctx = project.session.ctx(&cancel, &NoProgress);
    app::project_open(&ctx, &project.root).unwrap();
    let page = app::sections_show(&ctx, Empty {}).unwrap();
    assert!(page.rules.error.is_none());
    let check = app::rules_check(&ctx, Empty {}).unwrap();
    assert_eq!(check, page.rules);
    assert_eq!(
        check
            .writes
            .iter()
            .map(|write| write.path.as_str())
            .collect::<Vec<_>>(),
        ["AGENTS.md", "CLAUDE.md", "GEMINI.md"]
    );
    assert!(!project.root.join("AGENTS.md").exists());
    let sync = app::rules_sync(&ctx, Empty {}).unwrap();
    assert!(sync.synced);
    assert_eq!(
        project.read("GEMINI.md").unwrap(),
        b"user text\n@AGENTS.md\n"
    );
    assert!(cursor.is_dir());
}

#[test]
fn the_banner_takes_the_first_match_of_the_fixed_order() {
    let project = Project::new(&[(
        ".oah/config.toml",
        b"[stages]\nplan = \"gemini\"\nbuild = \"claude\"\nreview = \"codex\"\nfix = \"codex\"\n",
    )]);
    let config =
        oah_core::config::ProjectConfig::read(&project.root.join(".oah/config.toml")).unwrap();
    let effective = config.effective();
    let base = |gemini: AgentStatusDto, codex: SignInDto| {
        vec![
            agent(AgentId::Claude, true, SignInDto::SignedIn),
            agent(AgentId::Codex, true, codex),
            gemini,
        ]
    };
    let kind = |banner: &BannerDto| {
        serde_json::to_value(banner).unwrap()["kind"]
            .as_str()
            .unwrap()
            .to_string()
    };

    let missing = status(
        effective,
        &base(
            agent(AgentId::Gemini, false, SignInDto::Unknown),
            SignInDto::SignedOut,
        ),
    );
    assert_eq!(kind(&missing), "not-installed");
    let BannerDto::NotInstalled {
        sections,
        suggestion,
        ..
    } = &missing
    else {
        unreachable!()
    };
    assert_eq!(
        sections.iter().map(|s| s.id.as_str()).collect::<Vec<_>>(),
        ["plan"]
    );
    assert_eq!(
        *suggestion,
        Some(AgentIdDto(AgentId::Claude)),
        "the first installed, signed-in agent"
    );

    let signed_out = status(
        effective,
        &base(
            agent(AgentId::Gemini, true, SignInDto::Unknown),
            SignInDto::SignedOut,
        ),
    );
    assert_eq!(kind(&signed_out), "not-signed-in");
    let BannerDto::NotSignedIn { agents, message } = &signed_out else {
        unreachable!()
    };
    assert_eq!(agents.len(), 1);
    assert_eq!(agents[0].login_hint.text, "codex login");
    assert!(message.contains("codex login"));

    let unknown = status(
        effective,
        &base(
            agent(AgentId::Gemini, true, SignInDto::Unknown),
            SignInDto::SignedIn,
        ),
    );
    assert_eq!(kind(&unknown), "sign-in-unknown");
    assert!(
        serde_json::to_value(&unknown).unwrap()["message"]
            .as_str()
            .unwrap()
            .contains("OAH cannot check sign-in for Gemini CLI")
    );

    let all_in = base(
        agent(AgentId::Gemini, true, SignInDto::SignedIn),
        SignInDto::SignedIn,
    );
    assert_eq!(kind(&status(effective, &all_in)), "ready");

    let same = Project::new(&[(
        ".oah/config.toml",
        b"[stages]\nbuild = \"codex\"\nreview = \"codex\"\n",
    )]);
    let config =
        oah_core::config::ProjectConfig::read(&same.root.join(".oah/config.toml")).unwrap();
    let banner = status(config.effective(), &ready_agents());
    assert_eq!(kind(&banner), "not-independent");
    assert!(
        serde_json::to_value(&banner).unwrap()["message"]
            .as_str()
            .unwrap()
            .contains("Code Review is done by the same agent as Write Code")
    );
}

#[test]
fn a_common_replacement_must_keep_independence_between_two_missing_sections() {
    let project = Project::new(&[(
        ".oah/config.toml",
        b"[stages]\nbuild = \"gemini\"\nreview = \"codex\"\nfix = \"claude\"\n",
    )]);
    let agents = [
        agent(AgentId::Claude, true, SignInDto::SignedIn),
        agent(AgentId::Codex, false, SignInDto::Unknown),
        agent(AgentId::Gemini, false, SignInDto::Unknown),
    ];
    let banner = project.view(&agents).banner;
    let BannerDto::NotInstalled {
        sections,
        suggestion,
        ..
    } = banner
    else {
        panic!("expected the missing-agent banner, got {banner:?}");
    };
    assert_eq!(
        sections
            .iter()
            .map(|section| section.id.as_str())
            .collect::<Vec<_>>(),
        ["build", "review"]
    );
    assert_eq!(suggestion, None);
}

#[test]
fn a_signed_out_agent_can_be_assigned_and_the_independence_switch_writes_the_list() {
    let project = Project::new(&[(".oah/config.toml", b"[stages]\nplan = \"claude\"\n")]);
    let mut agents = ready_agents();
    agents.retain(|a| a.id.0 != AgentId::Codex);
    agents.push(agent(AgentId::Codex, true, SignInDto::SignedOut));
    let outcome = project
        .set(&agents, change("build", AgentId::Codex))
        .unwrap();
    assert!(outcome.synced);
    assert!(matches!(
        outcome.workflow.banner,
        BannerDto::NotSignedIn { .. }
    ));
    assert!(
        outcome
            .workflow
            .notices
            .iter()
            .any(|n| n.code == "OAH_REVIEW_NOT_INDEPENDENT")
    );
    let off = StagesSetReq {
        independent_of: vec![IndependenceReq {
            section: String::from("review"),
            of: Vec::new(),
        }],
        ..StagesSetReq::default()
    };
    let outcome = project.set(&agents, off).unwrap();
    assert!(
        project
            .text(".oah/config.toml")
            .contains("independent_of = []")
    );
    assert!(
        !outcome
            .workflow
            .notices
            .iter()
            .any(|n| n.code == "OAH_REVIEW_NOT_INDEPENDENT")
    );
    assert!(
        !project
            .text("AGENTS.md")
            .contains("is done by a different agent")
    );
}

#[test]
fn presets_cover_existing_sections_and_are_disabled_with_a_reason_for_a_missing_agent() {
    let project = Project::new(&[]);
    let view = project.view(&ready_agents());
    let balanced = view.presets.iter().find(|p| p.id == "balanced").unwrap();
    assert_eq!(balanced.disabled_reason, None);
    assert_eq!(balanced.stages.len(), 4);
    let research = view
        .presets
        .iter()
        .find(|p| p.id == "research-build")
        .unwrap();
    assert_eq!(
        research.disabled_reason.as_deref(),
        Some("Gemini CLI is not installed")
    );
    assert!(view.presets.iter().all(|p| p.id != "cursor-companion"));
}

/// The session commands on an opened folder: the same results as the explicit-root forms, `rules_check` writes
/// nothing, and `preset_apply` refuses a disabled preset without writing.
#[test]
fn the_session_commands_share_the_core_and_rules_check_writes_nothing() {
    let project = Project::new(&[]);
    let cancel = CancelToken::new();
    let ctx = project.session.ctx(&cancel, &NoProgress);
    assert_eq!(
        app::sections_show(&ctx, Empty {}).unwrap_err().code(),
        "OAH_PROJECT_NOT_OPEN"
    );
    let opened = app::project_open(&ctx, &project.root).unwrap();
    let before = project.tree();
    let check = app::rules_check(&ctx, Empty {}).unwrap();
    assert!(!check.synced);
    assert_eq!(
        check
            .drift
            .iter()
            .map(|d| d.path.as_str())
            .collect::<Vec<_>>(),
        ["AGENTS.md", "CLAUDE.md"]
    );
    assert_eq!(project.tree(), before, "rules_check wrote nothing");
    let preset = WorkflowProjectReq {
        project_id: opened.id.clone(),
        input: PresetApplyReq {
            preset: String::from("research-build"),
            approve_upgrade: None,
        },
    };
    assert_eq!(
        app::preset_apply(&ctx, preset).unwrap_err().code(),
        "OAH_PRESET_UNAVAILABLE"
    );
    assert_eq!(project.tree(), before);
    let synced = app::rules_sync(&ctx, Empty {}).unwrap();
    assert!(synced.synced);
    assert_eq!(synced.written.len(), 2);
    assert!(app::rules_check(&ctx, Empty {}).unwrap().synced);
    let shown = app::sections_show(&ctx, Empty {}).unwrap();
    assert_eq!(shown.config_format, ConfigFormatDto::Missing);
    assert!(matches!(
        app::workflow_status(&ctx, Empty {}).unwrap(),
        BannerDto::SignInUnknown { .. }
    ));
    let saved = app::stages_set(
        &ctx,
        WorkflowProjectReq {
            project_id: opened.id,
            input: change("build", AgentId::Codex),
        },
    )
    .unwrap();
    assert!(saved.saved && saved.synced);
    assert_eq!(
        app::sections_show(&ctx, Empty {}).unwrap().config_format,
        ConfigFormatDto::Current
    );
}

#[test]
fn a_project_switch_refuses_late_workflow_requests_even_for_identical_legacy_bytes() {
    let a = Project::new(&[(".oah/config.toml", LEGACY.as_bytes())]);
    let b = Project::new(&[(".oah/config.toml", LEGACY.as_bytes())]);
    let cancel = CancelToken::new();
    let ctx = a.session.ctx(&cancel, &NoProgress);
    let probe = FakeProbe::new(Os::Macos)
        .program("claude", "/bin/claude")
        .run(
            "/bin/claude --version",
            Some(0),
            "2.1.144 (Claude Code)\n",
            "",
        )
        .program("codex", "/bin/codex")
        .run("/bin/codex --version", Some(0), "codex-cli 0.163.0\n", "");
    app::agents_probe_with(
        &ctx,
        AgentsProbeReq {
            agents: Some(vec![
                AgentIdDto(AgentId::Claude),
                AgentIdDto(AgentId::Codex),
            ]),
            sign_in: false,
            focus: false,
            opencode_details: None,
        },
        &probe,
    )
    .unwrap();
    let opened_a = app::project_open(&ctx, &a.root).unwrap();
    let req_a = WorkflowProjectReq {
        project_id: opened_a.id,
        input: Empty {},
    };
    let view_a = app::sections_show_for_project(&ctx, req_a.clone()).unwrap();
    let opened_b = app::project_open(&ctx, &b.root).unwrap();
    let req_b = WorkflowProjectReq {
        project_id: opened_b.id.clone(),
        input: Empty {},
    };
    let view_b = app::sections_show_for_project(&ctx, req_b.clone()).unwrap();
    assert_eq!(view_a.config_sha256, view_b.config_sha256);
    let edit = StagesSetReq {
        approve_upgrade: view_a.config_sha256.clone(),
        ..change("build", AgentId::Codex)
    };
    let preset = PresetApplyReq {
        preset: String::from("balanced"),
        approve_upgrade: view_a.config_sha256,
    };
    let before_a = a.tree();
    let before_b = b.tree();
    let errors = [
        app::sections_show_for_project(&ctx, req_a.clone()).unwrap_err(),
        app::stages_set(
            &ctx,
            WorkflowProjectReq {
                project_id: req_a.project_id.clone(),
                input: edit.clone(),
            },
        )
        .unwrap_err(),
        app::preset_apply(
            &ctx,
            WorkflowProjectReq {
                project_id: req_a.project_id.clone(),
                input: preset.clone(),
            },
        )
        .unwrap_err(),
        app::rules_sync_for_project(&ctx, req_a).unwrap_err(),
    ];
    assert!(
        errors
            .iter()
            .all(|error| error.code() == "OAH_PROJECT_CHANGED")
    );
    assert_eq!(a.tree(), before_a);
    assert_eq!(b.tree(), before_b);

    assert!(app::rules_sync_for_project(&ctx, req_b).unwrap().synced);
    assert!(b.root.join("AGENTS.md").is_file());
    let saved = app::stages_set(
        &ctx,
        WorkflowProjectReq {
            project_id: opened_b.id.clone(),
            input: edit,
        },
    )
    .unwrap();
    assert!(saved.upgraded && saved.synced);
    assert_eq!(
        b.read(".oah/config.toml.v0.4.bak").unwrap(),
        LEGACY.as_bytes()
    );
    let preset = app::preset_apply(
        &ctx,
        WorkflowProjectReq {
            project_id: opened_b.id,
            input: preset,
        },
    )
    .unwrap();
    assert!(preset.saved && preset.synced);
    assert!(b.text(".oah/config.toml").contains("build = \"claude\""));
    assert_eq!(a.tree(), before_a);
}

fn sample(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/dto_samples/workflow")
        .join(name)
}

/// The Team Workflow page of a fixed project is the shared sample, byte for byte (the UI tests load the same file).
/// `OAH_WRITE_DTO_SAMPLES=1` rewrites the samples.
#[test]
fn the_workflow_page_matches_the_shared_samples() {
    let project = Project::new(&[(".oah/config.toml", LEGACY.as_bytes())]);
    let mut agents = ready_agents();
    agents.retain(|a| a.id.0 != AgentId::Codex);
    agents.push(agent(AgentId::Codex, true, SignInDto::SignedOut));
    let page = serde_json::to_string_pretty(&project.view(&agents)).unwrap() + "\n";
    let req = StagesSetReq {
        approve_upgrade: project.view(&agents).config_sha256,
        ..change("review", AgentId::Claude)
    };
    let saved = serde_json::to_string_pretty(&project.set(&agents, req).unwrap()).unwrap() + "\n";
    for (name, text) in [
        ("sections_show_legacy.json", &page),
        ("stages_set_upgraded.json", &saved),
    ] {
        if std::env::var_os("OAH_WRITE_DTO_SAMPLES").is_some() {
            fs::write(sample(name), text).unwrap();
        }
        assert_eq!(&fs::read_to_string(sample(name)).unwrap(), text, "{name}");
    }
}

fn probe_ready_session(ctx: &app::Ctx<'_>) {
    let probe = FakeProbe::new(Os::Macos)
        .program("claude", "/bin/claude")
        .run(
            "/bin/claude --version",
            Some(0),
            "2.1.144 (Claude Code)\n",
            "",
        )
        .program("codex", "/bin/codex")
        .run("/bin/codex --version", Some(0), "codex-cli 0.163.0\n", "");
    app::agents_probe_with(
        ctx,
        AgentsProbeReq {
            agents: Some(vec![
                AgentIdDto(AgentId::Claude),
                AgentIdDto(AgentId::Codex),
            ]),
            sign_in: false,
            focus: false,
            opencode_details: None,
        },
        &probe,
    )
    .unwrap();
}

fn assert_unsafe_config_refusal(
    env: &Env,
    root: &Path,
    error: Error,
    expected_entry: &'static str,
    expected_reason: ConfigPathRefusal,
    hidden_tokens: &[&str],
) {
    assert_eq!(error.code(), "OAH_FILE_IO", "{error}");
    let dto = error.to_dto(env);
    assert_eq!(dto.code, "OAH_FILE_IO");
    assert!(dto.message.contains(expected_entry), "{}", dto.message);
    assert!(
        !dto.message.contains(&root.display().to_string()),
        "must not expose absolute project root: {}",
        dto.message
    );
    for token in hidden_tokens {
        assert!(
            !error.message().contains(token) && !dto.message.contains(token),
            "must not expose stored target {token:?}: {}",
            dto.message
        );
    }
    assert!(
        matches!(
            error,
            Error::Workflow(WorkflowError::UnsafeConfigPath { entry, reason })
                if entry == expected_entry && reason == expected_reason
        ),
        "expected UnsafeConfigPath({expected_entry:?}, {expected_reason:?}), got {error:?}"
    );
}

#[test]
fn workflow_config_saves_allow_absent_regular_and_legacy_backup_paths_and_preserve_error_semantics()
{
    let agents = ready_agents();

    // 1. Absent `.oah` directory works through `preset_apply` as well as `stages_set`.
    let absent = Project::new(&[]);
    let cancel = CancelToken::new();
    let ctx = absent.session.ctx(&cancel, &NoProgress);
    probe_ready_session(&ctx);
    let opened = app::project_open(&ctx, &absent.root).unwrap();
    let applied = app::preset_apply(
        &ctx,
        WorkflowProjectReq {
            project_id: opened.id,
            input: PresetApplyReq {
                preset: String::from("balanced"),
                approve_upgrade: None,
            },
        },
    )
    .unwrap();
    assert!(applied.saved && applied.synced);
    assert!(
        absent
            .text(".oah/config.toml")
            .contains("plan = \"claude\"")
    );

    // 2. Regular `.oah/config.toml` with a leftover regular `.oah/config.toml.lock` saves cleanly.
    let with_lock = Project::new(&[
        (".oah/config.toml", b"[stages]\nplan = \"claude\"\n"),
        (".oah/config.toml.lock", b""),
    ]);
    let saved = with_lock
        .set(&agents, change("review", AgentId::Claude))
        .unwrap();
    assert!(saved.saved && saved.synced);
    assert!(
        with_lock
            .text(".oah/config.toml")
            .contains("review = \"claude\"")
    );

    // 3. Regular identical `.oah/config.toml.v0.4.bak` is reused on approved legacy upgrade.
    let reused = Project::new(&[
        (".oah/config.toml", LEGACY.as_bytes()),
        (".oah/config.toml.v0.4.bak", LEGACY.as_bytes()),
    ]);
    let sha = reused.view(&agents).config_sha256;
    let outcome = reused
        .set(
            &agents,
            StagesSetReq {
                approve_upgrade: sha,
                ..change("review", AgentId::Claude)
            },
        )
        .unwrap();
    assert!(outcome.saved && outcome.upgraded && outcome.synced);
    assert_eq!(outcome.backup.as_deref(), Some(".oah/config.toml.v0.4.bak"));
    assert_eq!(
        reused.read(".oah/config.toml.v0.4.bak").unwrap(),
        LEGACY.as_bytes()
    );

    // 4. Regular conflicting `.oah/config.toml.v0.4.bak` preserves OAH_CONFIG_BACKUP_CONFLICT without overwriting.
    let conflict = Project::new(&[
        (".oah/config.toml", LEGACY.as_bytes()),
        (".oah/config.toml.v0.4.bak", b"other backup\n"),
    ]);
    let sha = conflict.view(&agents).config_sha256;
    let err = conflict
        .set(
            &agents,
            StagesSetReq {
                approve_upgrade: sha,
                ..change("review", AgentId::Claude)
            },
        )
        .unwrap_err();
    assert_eq!(err.code(), "OAH_CONFIG_BACKUP_CONFLICT");
    assert_eq!(
        conflict.read(".oah/config.toml").unwrap(),
        LEGACY.as_bytes()
    );
    assert_eq!(
        conflict.read(".oah/config.toml.v0.4.bak").unwrap(),
        b"other backup\n"
    );
}

#[cfg(unix)]
#[test]
fn workflow_config_mutations_refuse_linked_config_paths_without_writing_or_exposing_targets() {
    use std::os::unix::fs::symlink;

    let agents = ready_agents();
    let cancel = CancelToken::new();

    // 1. `.oah` symlink (live in-project directory and dangling in-project target).
    for (label, create_target) in [("inert-dir", true), ("inert-missing-dir", false)] {
        let project = Project::new(&[]);
        let target = project.root.join(label);
        if create_target {
            fs::create_dir(&target).unwrap();
        }
        symlink(&target, project.root.join(".oah")).unwrap();
        let ctx = project.session.ctx(&cancel, &NoProgress);
        probe_ready_session(&ctx);
        let opened = app::project_open(&ctx, &project.root).unwrap();

        let direct =
            workflow::set_stages(&ctx, &project.root, &agents, change("plan", AgentId::Codex))
                .unwrap_err();
        assert_unsafe_config_refusal(
            project.session.env(),
            &project.root,
            direct,
            ".oah",
            ConfigPathRefusal::LinkOrReparse,
            &[label],
        );

        let via_stages = app::stages_set(
            &ctx,
            WorkflowProjectReq {
                project_id: opened.id.clone(),
                input: change("plan", AgentId::Codex),
            },
        )
        .unwrap_err();
        assert_unsafe_config_refusal(
            project.session.env(),
            &project.root,
            via_stages,
            ".oah",
            ConfigPathRefusal::LinkOrReparse,
            &[label],
        );

        let via_preset = app::preset_apply(
            &ctx,
            WorkflowProjectReq {
                project_id: opened.id,
                input: PresetApplyReq {
                    preset: String::from("balanced"),
                    approve_upgrade: None,
                },
            },
        )
        .unwrap_err();
        assert_unsafe_config_refusal(
            project.session.env(),
            &project.root,
            via_preset,
            ".oah",
            ConfigPathRefusal::LinkOrReparse,
            &[label],
        );

        if create_target {
            assert!(fs::read_dir(&target).unwrap().next().is_none());
        } else {
            assert!(!target.exists());
        }
        assert!(!project.root.join("AGENTS.md").exists());
    }

    // 2. Leaf symlinks inside a real `.oah` folder: config.toml, config.toml.lock, and config.toml.v0.4.bak
    //    (both live in-project targets and dangling in-project targets, including legacy upgrade with approval).
    struct LeafCase {
        entry: &'static str,
        target_name: &'static str,
        create_target: bool,
        target_bytes: &'static [u8],
        legacy_config: bool,
    }
    let cases = [
        LeafCase {
            entry: ".oah/config.toml",
            target_name: "inert-config.toml",
            create_target: true,
            target_bytes: b"[stages]\nplan = \"claude\"\n",
            legacy_config: false,
        },
        LeafCase {
            entry: ".oah/config.toml",
            target_name: "inert-missing-config.toml",
            create_target: false,
            target_bytes: b"",
            legacy_config: false,
        },
        LeafCase {
            entry: ".oah/config.toml.lock",
            target_name: "inert-lock.txt",
            create_target: true,
            target_bytes: b"inert lock target\n",
            legacy_config: false,
        },
        LeafCase {
            entry: ".oah/config.toml.lock",
            target_name: "inert-missing.lock",
            create_target: false,
            target_bytes: b"",
            legacy_config: false,
        },
        LeafCase {
            entry: ".oah/config.toml.v0.4.bak",
            target_name: "inert-backup.txt",
            create_target: true,
            target_bytes: LEGACY.as_bytes(),
            legacy_config: true,
        },
        LeafCase {
            entry: ".oah/config.toml.v0.4.bak",
            target_name: "inert-missing.bak",
            create_target: false,
            target_bytes: b"",
            legacy_config: true,
        },
    ];

    for case in cases {
        let initial_config: &[u8] = if case.legacy_config {
            LEGACY.as_bytes()
        } else {
            b"[stages]\nplan = \"claude\"\n"
        };
        let files: Vec<(&str, &[u8])> = if case.entry == ".oah/config.toml" {
            Vec::new()
        } else {
            vec![(".oah/config.toml", initial_config)]
        };
        let project = Project::new(&files);
        fs::create_dir_all(project.root.join(".oah")).unwrap();
        let target = project.root.join(case.target_name);
        if case.create_target {
            fs::write(&target, case.target_bytes).unwrap();
        }
        symlink(&target, project.root.join(case.entry)).unwrap();
        let ctx = project.session.ctx(&cancel, &NoProgress);
        probe_ready_session(&ctx);
        let opened = app::project_open(&ctx, &project.root).unwrap();
        let approval = if case.legacy_config {
            project.view(&agents).config_sha256
        } else {
            None
        };

        let stage_req = StagesSetReq {
            approve_upgrade: approval.clone(),
            ..change("review", AgentId::Claude)
        };
        let direct =
            workflow::set_stages(&ctx, &project.root, &agents, stage_req.clone()).unwrap_err();
        assert_unsafe_config_refusal(
            project.session.env(),
            &project.root,
            direct,
            case.entry,
            ConfigPathRefusal::LinkOrReparse,
            &[case.target_name],
        );

        let via_stages = app::stages_set(
            &ctx,
            WorkflowProjectReq {
                project_id: opened.id.clone(),
                input: stage_req,
            },
        )
        .unwrap_err();
        assert_unsafe_config_refusal(
            project.session.env(),
            &project.root,
            via_stages,
            case.entry,
            ConfigPathRefusal::LinkOrReparse,
            &[case.target_name],
        );

        let via_preset = app::preset_apply(
            &ctx,
            WorkflowProjectReq {
                project_id: opened.id,
                input: PresetApplyReq {
                    preset: String::from("balanced"),
                    approve_upgrade: approval,
                },
            },
        )
        .unwrap_err();
        assert_unsafe_config_refusal(
            project.session.env(),
            &project.root,
            via_preset,
            case.entry,
            ConfigPathRefusal::LinkOrReparse,
            &[case.target_name],
        );

        if case.create_target {
            assert_eq!(fs::read(&target).unwrap(), case.target_bytes);
        } else {
            assert!(!target.exists());
        }
        if case.entry != ".oah/config.toml" {
            assert_eq!(project.read(".oah/config.toml").unwrap(), initial_config);
        }
        assert!(!project.root.join("AGENTS.md").exists());
    }
}

#[test]
fn workflow_config_mutations_refuse_unsupported_non_link_metadata_without_writing() {
    let agents = ready_agents();
    let cancel = CancelToken::new();

    // 1. `.oah` is a regular file instead of a directory.
    let oah_file = Project::new(&[(".oah", b"not a directory\n")]);
    let ctx = oah_file.session.ctx(&cancel, &NoProgress);
    probe_ready_session(&ctx);
    let opened = app::project_open(&ctx, &oah_file.root).unwrap();
    for error in [
        workflow::set_stages(
            &ctx,
            &oah_file.root,
            &agents,
            change("plan", AgentId::Codex),
        )
        .unwrap_err(),
        app::stages_set(
            &ctx,
            WorkflowProjectReq {
                project_id: opened.id.clone(),
                input: change("plan", AgentId::Codex),
            },
        )
        .unwrap_err(),
        app::preset_apply(
            &ctx,
            WorkflowProjectReq {
                project_id: opened.id,
                input: PresetApplyReq {
                    preset: String::from("balanced"),
                    approve_upgrade: None,
                },
            },
        )
        .unwrap_err(),
    ] {
        assert_unsafe_config_refusal(
            oah_file.session.env(),
            &oah_file.root,
            error,
            ".oah",
            ConfigPathRefusal::NotDirectory,
            &[],
        );
    }
    assert_eq!(oah_file.read(".oah").unwrap(), b"not a directory\n");

    // 2. Directories where regular config, lock, or relevant legacy backup files belong.
    for (entry, legacy_config) in [
        (".oah/config.toml", false),
        (".oah/config.toml.lock", false),
        (".oah/config.toml.v0.4.bak", true),
    ] {
        let initial_config: &[u8] = if legacy_config {
            LEGACY.as_bytes()
        } else {
            b"[stages]\nplan = \"claude\"\n"
        };
        let files: Vec<(&str, &[u8])> = if entry == ".oah/config.toml" {
            Vec::new()
        } else {
            vec![(".oah/config.toml", initial_config)]
        };
        let project = Project::new(&files);
        fs::create_dir_all(project.root.join(entry)).unwrap();
        let ctx = project.session.ctx(&cancel, &NoProgress);
        probe_ready_session(&ctx);
        let opened = app::project_open(&ctx, &project.root).unwrap();
        let approval = if legacy_config {
            project.view(&agents).config_sha256
        } else {
            None
        };
        let stage_req = StagesSetReq {
            approve_upgrade: approval.clone(),
            ..change("review", AgentId::Claude)
        };
        for error in [
            workflow::set_stages(&ctx, &project.root, &agents, stage_req.clone()).unwrap_err(),
            app::stages_set(
                &ctx,
                WorkflowProjectReq {
                    project_id: opened.id.clone(),
                    input: stage_req,
                },
            )
            .unwrap_err(),
            app::preset_apply(
                &ctx,
                WorkflowProjectReq {
                    project_id: opened.id,
                    input: PresetApplyReq {
                        preset: String::from("balanced"),
                        approve_upgrade: approval,
                    },
                },
            )
            .unwrap_err(),
        ] {
            assert_unsafe_config_refusal(
                project.session.env(),
                &project.root,
                error,
                entry,
                ConfigPathRefusal::NotRegularFile,
                &[],
            );
        }
        assert!(project.root.join(entry).is_dir());
        if entry != ".oah/config.toml" {
            assert_eq!(project.read(".oah/config.toml").unwrap(), initial_config);
        }
        assert!(!project.root.join("AGENTS.md").exists());
    }

    // 3. Unix FIFOs at config, lock, or relevant legacy backup paths are refused immediately without blocking.
    #[cfg(unix)]
    for (entry, legacy_config) in [
        (".oah/config.toml", false),
        (".oah/config.toml.lock", false),
        (".oah/config.toml.v0.4.bak", true),
    ] {
        let initial_config: &[u8] = if legacy_config {
            LEGACY.as_bytes()
        } else {
            b"[stages]\nplan = \"claude\"\n"
        };
        let files: Vec<(&str, &[u8])> = if entry == ".oah/config.toml" {
            Vec::new()
        } else {
            vec![(".oah/config.toml", initial_config)]
        };
        let project = Project::new(&files);
        let ctx = project.session.ctx(&cancel, &NoProgress);
        probe_ready_session(&ctx);
        let opened = app::project_open(&ctx, &project.root).unwrap();
        let approval = if legacy_config {
            project.view(&agents).config_sha256
        } else {
            None
        };
        fs::create_dir_all(project.root.join(".oah")).unwrap();
        let fifo = project.root.join(entry);
        let status = std::process::Command::new("mkfifo")
            .arg(&fifo)
            .status()
            .expect("run mkfifo");
        assert!(status.success(), "mkfifo failed: {status}");
        let stage_req = StagesSetReq {
            approve_upgrade: approval.clone(),
            ..change("review", AgentId::Claude)
        };
        for error in [
            workflow::set_stages(&ctx, &project.root, &agents, stage_req.clone()).unwrap_err(),
            app::stages_set(
                &ctx,
                WorkflowProjectReq {
                    project_id: opened.id.clone(),
                    input: stage_req,
                },
            )
            .unwrap_err(),
            app::preset_apply(
                &ctx,
                WorkflowProjectReq {
                    project_id: opened.id,
                    input: PresetApplyReq {
                        preset: String::from("balanced"),
                        approve_upgrade: approval,
                    },
                },
            )
            .unwrap_err(),
        ] {
            assert_unsafe_config_refusal(
                project.session.env(),
                &project.root,
                error,
                entry,
                ConfigPathRefusal::NotRegularFile,
                &[],
            );
        }
        if entry != ".oah/config.toml" {
            assert_eq!(project.read(".oah/config.toml").unwrap(), initial_config);
        }
        assert!(!project.root.join("AGENTS.md").exists());
    }
}

#[test]
fn workflow_current_format_save_ignores_unused_legacy_backup_and_preserves_rules_refusal() {
    let agents = ready_agents();
    let cancel = CancelToken::new();

    // 1. Cross-platform: an unused directory at `.oah/config.toml.v0.4.bak` does not block current-format saves
    //    via `set_stages`, `stages_set`, or `preset_apply`, nor does it trigger a post-save reread failure.
    let dir_bak = Project::new(&[(".oah/config.toml", b"[stages]\nplan = \"claude\"\n")]);
    fs::create_dir_all(dir_bak.root.join(".oah/config.toml.v0.4.bak")).unwrap();
    let ctx = dir_bak.session.ctx(&cancel, &NoProgress);
    probe_ready_session(&ctx);
    let opened = app::project_open(&ctx, &dir_bak.root).unwrap();

    let direct = workflow::set_stages(
        &ctx,
        &dir_bak.root,
        &agents,
        change("review", AgentId::Claude),
    )
    .unwrap();
    assert!(direct.saved && !direct.upgraded && direct.backup.is_none() && direct.synced);
    assert!(
        dir_bak
            .text(".oah/config.toml")
            .contains("review = \"claude\"")
    );
    assert!(dir_bak.root.join(".oah/config.toml.v0.4.bak").is_dir());

    let via_stages = app::stages_set(
        &ctx,
        WorkflowProjectReq {
            project_id: opened.id.clone(),
            input: change("fix", AgentId::Claude),
        },
    )
    .unwrap();
    assert!(
        via_stages.saved
            && !via_stages.upgraded
            && via_stages.backup.is_none()
            && via_stages.synced
    );
    assert!(
        dir_bak
            .text(".oah/config.toml")
            .contains("fix = \"claude\"")
    );

    let via_preset = app::preset_apply(
        &ctx,
        WorkflowProjectReq {
            project_id: opened.id,
            input: PresetApplyReq {
                preset: String::from("balanced"),
                approve_upgrade: None,
            },
        },
    )
    .unwrap();
    assert!(
        via_preset.saved
            && !via_preset.upgraded
            && via_preset.backup.is_none()
            && via_preset.synced
    );
    assert!(
        dir_bak
            .text(".oah/config.toml")
            .contains("review = \"codex\"")
    );
    assert!(dir_bak.root.join(".oah/config.toml.v0.4.bak").is_dir());

    // 2. Unix: an unused live symlink, dangling symlink, or FIFO at `.oah/config.toml.v0.4.bak` does not block
    //    current-format saves and its in-project target stays untouched.
    #[cfg(unix)]
    {
        use std::os::unix::fs::symlink;

        for (target_name, create_target) in [("inert-bak.txt", true), ("inert-missing.bak", false)]
        {
            let project = Project::new(&[(".oah/config.toml", b"[stages]\nplan = \"claude\"\n")]);
            let target = project.root.join(target_name);
            if create_target {
                fs::write(&target, b"unused backup target\n").unwrap();
            }
            symlink(&target, project.root.join(".oah/config.toml.v0.4.bak")).unwrap();
            let outcome = project
                .set(&agents, change("review", AgentId::Claude))
                .unwrap();
            assert!(
                outcome.saved && !outcome.upgraded && outcome.backup.is_none() && outcome.synced
            );
            assert!(
                project
                    .text(".oah/config.toml")
                    .contains("review = \"claude\"")
            );
            if create_target {
                assert_eq!(fs::read(&target).unwrap(), b"unused backup target\n");
            } else {
                assert!(!target.exists());
            }
        }

        let fifo_project = Project::new(&[(".oah/config.toml", b"[stages]\nplan = \"claude\"\n")]);
        let fifo = fifo_project.root.join(".oah/config.toml.v0.4.bak");
        let status = std::process::Command::new("mkfifo")
            .arg(&fifo)
            .status()
            .expect("run mkfifo");
        assert!(status.success(), "mkfifo failed: {status}");
        let outcome = fifo_project
            .set(&agents, change("review", AgentId::Claude))
            .unwrap();
        assert!(outcome.saved && !outcome.upgraded && outcome.backup.is_none() && outcome.synced);
        assert!(
            fifo_project
                .text(".oah/config.toml")
                .contains("review = \"claude\"")
        );
    }

    // 3. Valid current-format save with an unused legacy backup still stays saved if Rules refuses to sync (AC6).
    let big = format!("# Notes\n{}\n", "x".repeat(40 * 1024));
    let rules_refused = Project::new(&[
        (".oah/config.toml", b"[stages]\nplan = \"claude\"\n"),
        ("AGENTS.md", big.as_bytes()),
    ]);
    fs::create_dir_all(rules_refused.root.join(".oah/config.toml.v0.4.bak")).unwrap();
    let outcome = rules_refused
        .set(&agents, change("fix", AgentId::Claude))
        .unwrap();
    assert!(outcome.saved && !outcome.upgraded && outcome.backup.is_none() && !outcome.synced);
    assert_eq!(
        outcome.sync_error.as_ref().map(|e| e.code.as_str()),
        Some("OAH_RULES_BUDGET")
    );
    assert_eq!(outcome.actions, ["rules_sync"]);
    assert!(
        rules_refused
            .text(".oah/config.toml")
            .contains("fix = \"claude\"")
    );
    assert_eq!(rules_refused.read("AGENTS.md").unwrap(), big.as_bytes());
    assert!(
        rules_refused
            .root
            .join(".oah/config.toml.v0.4.bak")
            .is_dir()
    );
}

#[test]
fn workflow_noop_and_unapproved_legacy_save_do_not_require_unused_lock_or_backup() {
    let agents = ready_agents();
    let cancel = CancelToken::new();

    // 1. Current-format no-op (both `stages_set` and `preset_apply`) succeeds even when unused lock and backup
    //    sidecars are non-regular entries (directories, and on Unix symlinks).
    let balanced_toml =
        b"[stages]\nplan = \"claude\"\nbuild = \"claude\"\nreview = \"codex\"\nfix = \"codex\"\n";
    let noop = Project::new(&[(".oah/config.toml", balanced_toml)]);
    fs::create_dir_all(noop.root.join(".oah/config.toml.lock")).unwrap();
    fs::create_dir_all(noop.root.join(".oah/config.toml.v0.4.bak")).unwrap();
    let ctx = noop.session.ctx(&cancel, &NoProgress);
    probe_ready_session(&ctx);
    let opened = app::project_open(&ctx, &noop.root).unwrap();

    let stage_noop = app::stages_set(
        &ctx,
        WorkflowProjectReq {
            project_id: opened.id.clone(),
            input: change("plan", AgentId::Claude),
        },
    )
    .unwrap();
    assert!(
        !stage_noop.saved
            && !stage_noop.upgraded
            && stage_noop.backup.is_none()
            && stage_noop.synced
    );
    assert_eq!(noop.read(".oah/config.toml").unwrap(), balanced_toml);

    let preset_noop = app::preset_apply(
        &ctx,
        WorkflowProjectReq {
            project_id: opened.id,
            input: PresetApplyReq {
                preset: String::from("balanced"),
                approve_upgrade: None,
            },
        },
    )
    .unwrap();
    assert!(
        !preset_noop.saved
            && !preset_noop.upgraded
            && preset_noop.backup.is_none()
            && preset_noop.synced
    );
    assert_eq!(noop.read(".oah/config.toml").unwrap(), balanced_toml);
    assert!(noop.root.join(".oah/config.toml.lock").is_dir());
    assert!(noop.root.join(".oah/config.toml.v0.4.bak").is_dir());

    #[cfg(unix)]
    {
        use std::os::unix::fs::symlink;

        let link_noop = Project::new(&[(".oah/config.toml", balanced_toml)]);
        let lock_target = link_noop.root.join("inert-noop-lock.txt");
        let bak_target = link_noop.root.join("inert-noop-bak.txt");
        fs::write(&lock_target, b"lock target\n").unwrap();
        fs::write(&bak_target, b"bak target\n").unwrap();
        symlink(&lock_target, link_noop.root.join(".oah/config.toml.lock")).unwrap();
        symlink(
            &bak_target,
            link_noop.root.join(".oah/config.toml.v0.4.bak"),
        )
        .unwrap();
        let outcome = link_noop
            .set(&agents, change("plan", AgentId::Claude))
            .unwrap();
        assert!(!outcome.saved && !outcome.upgraded && outcome.backup.is_none() && outcome.synced);
        assert_eq!(fs::read(&lock_target).unwrap(), b"lock target\n");
        assert_eq!(fs::read(&bak_target).unwrap(), b"bak target\n");
    }

    // 2. Legacy-format no-op (`review = "codex"` already matches) does not require approval, lock, or backup.
    let legacy_noop = Project::new(&[(".oah/config.toml", LEGACY.as_bytes())]);
    fs::create_dir_all(legacy_noop.root.join(".oah/config.toml.lock")).unwrap();
    fs::create_dir_all(legacy_noop.root.join(".oah/config.toml.v0.4.bak")).unwrap();
    let outcome = legacy_noop
        .set(&agents, change("review", AgentId::Codex))
        .unwrap();
    assert!(!outcome.saved && !outcome.upgraded && outcome.backup.is_none() && outcome.synced);
    assert_eq!(
        legacy_noop.read(".oah/config.toml").unwrap(),
        LEGACY.as_bytes()
    );

    // 3. Legacy-format real change WITHOUT matching approval returns OAH_CONFIG_UPGRADE_REQUIRED first (matching
    //    `config::save_project`), even if unused lock/backup sidecars are non-regular entries. Once exact approval
    //    is provided, the relevant backup path is refused before writing.
    let legacy_unapproved = Project::new(&[(".oah/config.toml", LEGACY.as_bytes())]);
    fs::create_dir_all(legacy_unapproved.root.join(".oah/config.toml.v0.4.bak")).unwrap();
    let sha = legacy_unapproved.view(&agents).config_sha256;
    for bad_approval in [None, Some(String::from("0").repeat(64))] {
        let err = legacy_unapproved
            .set(
                &agents,
                StagesSetReq {
                    approve_upgrade: bad_approval,
                    ..change("review", AgentId::Claude)
                },
            )
            .unwrap_err();
        assert_eq!(err.code(), "OAH_CONFIG_UPGRADE_REQUIRED");
        assert_eq!(
            legacy_unapproved.read(".oah/config.toml").unwrap(),
            LEGACY.as_bytes()
        );
    }
    let approved_err = legacy_unapproved
        .set(
            &agents,
            StagesSetReq {
                approve_upgrade: sha,
                ..change("review", AgentId::Claude)
            },
        )
        .unwrap_err();
    assert_unsafe_config_refusal(
        legacy_unapproved.session.env(),
        &legacy_unapproved.root,
        approved_err,
        ".oah/config.toml.v0.4.bak",
        ConfigPathRefusal::NotRegularFile,
        &[],
    );
    assert_eq!(
        legacy_unapproved.read(".oah/config.toml").unwrap(),
        LEGACY.as_bytes()
    );
    assert!(!legacy_unapproved.root.join("AGENTS.md").exists());
}

#[test]
fn workflow_post_save_diagnostics_report_committed_state_without_exposing_targets() {
    let project = Project::new(&[]);
    for (entry, reason) in [
        (".oah", ConfigPathRefusal::LinkOrReparse),
        (".oah", ConfigPathRefusal::NotDirectory),
        (".oah/config.toml", ConfigPathRefusal::LinkOrReparse),
        (".oah/config.toml", ConfigPathRefusal::NotRegularFile),
    ] {
        let error: Error = WorkflowError::UnsafeConfigPathAfterSave { entry, reason }.into();
        assert_eq!(error.code(), "OAH_FILE_IO");
        let dto = error.to_dto(project.session.env());
        assert_eq!(dto.code, "OAH_FILE_IO");
        assert!(dto.message.contains(entry), "{}", dto.message);
        assert!(
            dto.message.contains("the assignment was saved"),
            "post-save diagnostic must report committed write: {}",
            dto.message
        );
        assert!(
            !dto.message.contains("nothing was written"),
            "post-save diagnostic must not claim nothing was written: {}",
            dto.message
        );
        assert!(
            !dto.message.contains(&project.root.display().to_string()),
            "must not expose absolute project root: {}",
            dto.message
        );
    }
}
