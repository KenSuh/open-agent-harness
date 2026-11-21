// 프로젝트 설정·진단 시험: 기본 앱·포커스·새로고침과 기본 doctor 확인은 연 프로젝트의 [agents.<id>] command 를 쓰지 않고, 명시적 프로젝트 명령 doctor 진입점만 설정한 명령을 적용하며, [stages] 종료 코드는 두 모드에서 모두 유지되는지 가짜 확인 통로로 본다.
use crate::fake_agents::FakeProbe;
use crate::session::{CLAUDE_VERSION, claude, list, probe, req, session};
use oah_core::agents::{Os, Status};
use oah_core::app::{self, Session};
use oah_core::config::AgentId;
use oah_core::paths::{self, Env};
use oah_core::tasks::{CancelToken, NoProgress};
use std::fs;

const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };

/// A session whose open project is a temporary folder with `config` as its `.oah/config.toml`. Git comes from
/// the process's `PATH` (opening asks it whether the folder is a repository); agents only through fakes.
fn opened(config: &str) -> (tempfile::TempDir, Session) {
    let dir = tempfile::tempdir().unwrap();
    let root = paths::canonical_folder(&fs::canonicalize(dir.path()).unwrap()).unwrap();
    let (home, project) = (root.join("home"), root.join("project"));
    fs::create_dir_all(project.join(".oah")).unwrap();
    fs::create_dir_all(&home).unwrap();
    fs::write(project.join(".oah").join("config.toml"), config).unwrap();
    let mut env = Env::new(&root).unwrap();
    for name in [
        "PATH",
        "SystemRoot",
        "windir",
        "ComSpec",
        "PATHEXT",
        "TEMP",
        "TMP",
    ] {
        if let Some(value) = std::env::var_os(name) {
            env.set(name, value);
        }
    }
    env.set(HOME_VAR, &home);
    env.set("GIT_CONFIG_NOSYSTEM", "1");
    env.set("GIT_CEILING_DIRECTORIES", &root);
    let session = Session::new(env);
    let cancel = CancelToken::new();
    app::project_open(&session.ctx(&cancel, &NoProgress), &project).unwrap();
    (dir, session)
}

fn claude_through_node() -> FakeProbe {
    FakeProbe::new(Os::Macos)
        .program("node", "/bin/node")
        .run(
            "/bin/node /opt/claude/cli.js --version",
            Some(0),
            "2.1.318 (Claude Code)\n",
            "",
        )
        .run(
            "/bin/node /opt/claude/cli.js auth status",
            Some(0),
            "{}",
            "",
        )
}

#[test]
fn app_probing_ignores_the_open_projects_configured_commands() {
    let (_dir, session) = opened(
        "[agents.claude]\ncommand = [\"node\", \"/opt/claude/cli.js\"]\n\
         [agents.codex]\ncommand = [\"./bin/synthetic-codex\"]\n",
    );
    let cancel = CancelToken::new();
    let machine_and_node = claude(CLAUDE_VERSION, true)
        .program("node", "/bin/node")
        .run(
            "/bin/node /opt/claude/cli.js --version",
            Some(0),
            "2.1.318 (Claude Code)\n",
            "",
        )
        .run(
            "/bin/node /opt/claude/cli.js auth status",
            Some(0),
            "{}",
            "",
        );
    let all = oah_core::dto::AgentsProbeReq {
        agents: None,
        sign_in: true,
        focus: false,
    };
    let rows =
        app::agents_probe_with(&session.ctx(&cancel, &NoProgress), all, &machine_and_node).unwrap();
    assert_eq!(rows[0].status, Status::Ok);
    assert_eq!(rows[0].source.as_deref(), Some("/bin/claude"));
    let codex = rows.iter().find(|row| row.id.0 == AgentId::Codex).unwrap();
    assert_eq!(codex.status, Status::Missing);
    let asked = machine_and_node.asked();
    assert!(
        asked.contains(&String::from("resolve claude")),
        "default app probe uses machine provider lookup: {asked:?}"
    );
    assert!(
        asked.contains(&String::from("resolve codex")),
        "path-shaped project command is ignored: {asked:?}"
    );
    assert!(
        asked.contains(&String::from("resolve agy")),
        "built-in provider names stay unchanged: {asked:?}"
    );
    assert!(
        !asked.contains(&String::from("resolve node")),
        "app probe must never resolve a project-configured command: {asked:?}"
    );
    assert!(
        !asked.contains(&String::from("resolve antigravity")),
        "a default command is not a configured one: {asked:?}"
    );
    assert!(
        asked.iter().all(
            |entry| !entry.contains("/opt/claude/cli.js") && !entry.contains("synthetic-codex")
        ),
        "project commands must never be asked or run: {asked:?}"
    );

    let focus = oah_core::dto::AgentsProbeReq {
        agents: None,
        sign_in: true,
        focus: true,
    };
    let node_only = claude_through_node();
    let focus_rows =
        app::agents_probe_with(&session.ctx(&cancel, &NoProgress), focus, &node_only).unwrap();
    assert_eq!(focus_rows[0].status, Status::Missing);
    let focus_asked = node_only.asked();
    assert!(
        focus_asked.contains(&String::from("resolve claude")),
        "{focus_asked:?}"
    );
    assert!(
        !focus_asked.contains(&String::from("resolve node")),
        "focus probe must never resolve a project-configured command: {focus_asked:?}"
    );
    assert!(
        node_only.ran().is_empty(),
        "focus probe must never execute a project-configured command: {:?}",
        node_only.ran()
    );

    // Held-agent refresh (`app::refresh` -> `reprobe_held`) enters the same `agents_probe_with` path with the
    // session's held subset and `sign_in = true, focus = false`.
    let (_held_dir, held_session) = opened(
        "[agents.claude]\ncommand = [\"node\", \"/opt/claude/cli.js\"]\n\
         [agents.codex]\ncommand = [\"./bin/synthetic-codex\"]\n",
    );
    let seed_probe = claude(CLAUDE_VERSION, true).program("node", "/bin/node");
    let held_subset = req(&[AgentId::Claude, AgentId::Codex], false, false);
    probe(&held_session, held_subset, &seed_probe);

    let refresh_probe = claude(CLAUDE_VERSION, true)
        .program("node", "/bin/node")
        .run(
            "/bin/node /opt/claude/cli.js --version",
            Some(0),
            "2.1.318 (Claude Code)\n",
            "",
        )
        .run(
            "/bin/node /opt/claude/cli.js auth status",
            Some(0),
            "{}",
            "",
        );
    let refresh_req = req(&[AgentId::Claude, AgentId::Codex], true, false);
    probe(&held_session, refresh_req, &refresh_probe);
    let refresh_asked = refresh_probe.asked();
    assert!(
        refresh_asked.contains(&String::from("resolve claude")),
        "{refresh_asked:?}"
    );
    assert!(
        refresh_asked.contains(&String::from("resolve codex")),
        "{refresh_asked:?}"
    );
    assert!(
        refresh_asked.contains(&String::from("run /bin/claude --version")),
        "{refresh_asked:?}"
    );
    assert!(
        refresh_asked.contains(&String::from("run /bin/claude auth status")),
        "{refresh_asked:?}"
    );
    assert!(
        refresh_asked.iter().all(|entry| !entry.contains("node")
            && !entry.contains("/opt/claude/cli.js")
            && !entry.contains("synthetic-codex")),
        "held-agent refresh path must never ask for configured project commands: {refresh_asked:?}"
    );
    let held_rows = list(&held_session);
    assert_eq!(held_rows.len(), 2);
    assert_eq!(held_rows[0].id.0, AgentId::Claude);
    assert_eq!(held_rows[0].status, Status::Ok);
    assert_eq!(held_rows[0].source.as_deref(), Some("/bin/claude"));
    assert_eq!(held_rows[1].id.0, AgentId::Codex);
    assert_eq!(held_rows[1].status, Status::Missing);
}

#[test]
fn default_doctor_ignores_project_commands_while_keeping_project_stages() {
    let stages =
        "[stages]\nplan = \"claude\"\nbuild = \"claude\"\nreview = \"claude\"\nfix = \"claude\"\n";
    let (_dir, session) = opened(&format!(
        "{stages}[agents.claude]\ncommand = [\"node\", \"/opt/claude/cli.js\"]\n\
         [agents.codex]\ncommand = [\"./bin/synthetic-codex\"]\n"
    ));
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);

    let machine_and_node = claude(CLAUDE_VERSION, true)
        .program("node", "/bin/node")
        .run(
            "/bin/node /opt/claude/cli.js --version",
            Some(0),
            "2.1.318 (Claude Code)\n",
            "",
        )
        .run(
            "/bin/node /opt/claude/cli.js auth status",
            Some(0),
            "{}",
            "",
        );
    let doctor = app::doctor_with(&ctx, &machine_and_node).unwrap();
    assert_eq!(doctor.agents[0].status, Status::Ok);
    assert_eq!(doctor.agents[0].source.as_deref(), Some("/bin/claude"));
    let codex = doctor
        .agents
        .iter()
        .find(|row| row.id.0 == AgentId::Codex)
        .unwrap();
    assert_eq!(
        (codex.status, doctor.exit),
        (Status::Missing, 0),
        "default doctor still uses project [stages] for readiness exit"
    );
    let asked = machine_and_node.asked();
    assert!(asked.contains(&String::from("resolve claude")), "{asked:?}");
    assert!(asked.contains(&String::from("resolve codex")), "{asked:?}");
    assert!(asked.contains(&String::from("resolve agy")), "{asked:?}");
    assert!(
        !asked.contains(&String::from("resolve node")),
        "default doctor must ignore project command overrides: {asked:?}"
    );
    assert!(
        asked.iter().all(
            |entry| !entry.contains("/opt/claude/cli.js") && !entry.contains("synthetic-codex")
        ),
        "default doctor must never ask or run configured project commands: {asked:?}"
    );

    let node_only = claude_through_node();
    let missing_machine = app::doctor_with(&ctx, &node_only).unwrap();
    assert_eq!(missing_machine.agents[0].status, Status::Missing);
    assert_eq!(missing_machine.exit, 1);
    assert!(
        !node_only.asked().contains(&String::from("resolve node")),
        "{:?}",
        node_only.asked()
    );
    assert!(
        node_only.ran().is_empty(),
        "default doctor must never execute project commands: {:?}",
        node_only.ran()
    );
}

#[test]
fn explicit_doctor_applies_the_open_projects_command_for_that_agent_only() {
    let (_dir, session) = opened("[agents.claude]\ncommand = [\"node\", \"/opt/claude/cli.js\"]\n");
    let cancel = CancelToken::new();
    let probe = claude_through_node();
    let doctor =
        app::doctor_project_commands_with(&session.ctx(&cancel, &NoProgress), &probe).unwrap();
    assert_eq!(doctor.agents[0].status, Status::Ok);
    let asked = probe.asked();
    assert!(
        !asked.contains(&String::from("resolve claude")),
        "{asked:?}"
    );
    assert!(
        asked.contains(&String::from("resolve node")),
        "explicit doctor entry applies the configured command: {asked:?}"
    );
    assert!(
        asked.contains(&String::from("resolve agy")),
        "an agent without a command keeps its own name"
    );
    assert!(
        !asked.contains(&String::from("resolve antigravity")),
        "a default command is not a configured one"
    );
}

#[test]
fn doctor_requires_only_the_agents_the_projects_stages_use() {
    let stages =
        "[stages]\nplan = \"claude\"\nbuild = \"claude\"\nreview = \"claude\"\nfix = \"claude\"\n";
    let (_dir, session) = opened(&format!(
        "{stages}[agents.claude]\ncommand = [\"node\", \"/opt/claude/cli.js\"]\n"
    ));
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let doctor = app::doctor_project_commands_with(&ctx, &claude_through_node()).unwrap();
    let codex = doctor
        .agents
        .iter()
        .find(|row| row.id.0 == AgentId::Codex)
        .unwrap();
    assert_eq!(
        (codex.status, doctor.exit),
        (Status::Missing, 0),
        "no stage uses Codex"
    );
    let signed_out = FakeProbe::new(Os::Macos)
        .program("node", "/bin/node")
        .run(
            "/bin/node /opt/claude/cli.js --version",
            Some(0),
            "2.1.318 (Claude Code)\n",
            "",
        )
        .run("/bin/node /opt/claude/cli.js auth status", Some(1), "", "");
    assert_eq!(
        app::doctor_project_commands_with(&ctx, &signed_out)
            .unwrap()
            .exit,
        1,
        "the one stage agent is signed out"
    );
}

#[test]
fn doctor_exits_zero_only_when_every_agent_the_stages_use_is_usable() {
    let both = |codex_exit: i32| {
        claude(CLAUDE_VERSION, true)
            .program("codex", "/bin/codex")
            .run("/bin/codex --version", Some(0), "codex-cli 0.163.0\n", "")
            .run("/bin/codex login status", Some(codex_exit), "", "")
    };
    let cancel = CancelToken::new();
    let session = session();
    let ctx = session.ctx(&cancel, &NoProgress);
    let ready = app::doctor_with(&ctx, &both(0)).unwrap();
    assert_eq!(
        (ready.agents.len(), ready.exit),
        (7, 0),
        "the default stages use Claude Code and Codex"
    );
    assert_eq!(
        app::doctor_with(&ctx, &both(1)).unwrap().exit,
        1,
        "Codex is signed out"
    );
    let gemini = FakeProbe::new(Os::Macos)
        .program("gemini", "/bin/gemini")
        .run("/bin/gemini --version", Some(0), "0.9.0", "");
    let rows = probe(&session, req(&[AgentId::Gemini], true, false), &gemini);
    assert_eq!(
        app::doctor_exit(&rows, &[AgentId::Gemini]),
        0,
        "sign-in-unknown is usable"
    );
    assert_eq!(
        app::doctor_exit(&rows, &[AgentId::Copilot]),
        1,
        "an agent never probed is not usable"
    );
    assert_eq!(app::doctor_exit(&rows, &[]), 0);
}
