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
use oah_core::workflow::{self, status};
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
    let checked = app::rules_check(&ctx, Empty {}).unwrap();
    assert_eq!(checked, shown.rules);
    assert!(!project.root.join("AGENTS.md").exists());
    assert!(!project.root.join("CLAUDE.md").exists());

    let sync_err = app::rules_sync(&ctx, Empty {}).unwrap_err();
    assert_eq!(sync_err.code(), "OAH_RULES_LINK");
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
    let checked = app::rules_check(&ctx, Empty {}).unwrap();
    assert_eq!(checked, shown.rules);
    let sync_err = app::rules_sync(&ctx, Empty {}).unwrap_err();
    assert_eq!(sync_err.code(), "OAH_RULES_LINK");
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
