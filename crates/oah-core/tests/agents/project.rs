// 프로젝트 설정·진단 시험: 연 프로젝트의 [agents.<id>] command 와 [stages], 기본 단계가 확인 명령과 진단 종료 코드를 정하는지 가짜 확인 통로로 본다.
use crate::fake_agents::FakeProbe;
use crate::session::{CLAUDE_VERSION, claude, probe, req, session};
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
fn the_open_projects_command_replaces_the_lookup_of_that_agent_only() {
    let (_dir, session) = opened("[agents.claude]\ncommand = [\"node\", \"/opt/claude/cli.js\"]\n");
    let cancel = CancelToken::new();
    let probe = claude_through_node();
    let all = oah_core::dto::AgentsProbeReq {
        agents: None,
        sign_in: true,
        focus: false,
    };
    let rows = app::agents_probe_with(&session.ctx(&cancel, &NoProgress), all, &probe).unwrap();
    assert_eq!(rows[0].status, Status::Ok);
    let asked = probe.asked();
    assert!(
        !asked.contains(&String::from("resolve claude")),
        "{asked:?}"
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
    let doctor = app::doctor_with(&ctx, &claude_through_node()).unwrap();
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
        app::doctor_with(&ctx, &signed_out).unwrap().exit,
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
