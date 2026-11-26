// oah doctor: 일곱 에이전트를 앱과 같은 핵심 작업으로 확인해 상태를 글이나 JSON 으로 보이고, 단계 에이전트가 모두 쓸 수 있을 때만 0 으로 끝낸다.
use super::refuse;
use clap::{Arg, ArgAction, ArgMatches, Command};
use oah_core::Error;
use oah_core::agents::Status;
use oah_core::app::{self, Doctor, Session};
use oah_core::dto::AgentStatusDto;
use oah_core::paths::Env;
use oah_core::tasks::{CancelToken, NoProgress};
use std::process::ExitCode;

pub fn command() -> Command {
    Command::new("doctor")
        .about("Show whether each agent is installed and signed in")
        .arg(
            Arg::new("json")
                .long("json")
                .action(ArgAction::SetTrue)
                .help("Print the statuses as JSON"),
        )
        .arg(
            Arg::new("use-project-commands")
                .long("use-project-commands")
                .action(ArgAction::SetTrue)
                .help("Use [agents.<id>] command overrides from this project's .oah/config.toml instead of installed machine lookups"),
        )
}

pub fn run(matches: &ArgMatches) -> ExitCode {
    let json = matches.get_flag("json");
    let use_project_commands = matches.get_flag("use-project-commands");
    let env = match Env::from_process() {
        Ok(env) => env,
        Err(error) => return refuse(&fallback_env(), &Error::from(error), json),
    };
    let session = Session::new(env);
    match doctor(&session, use_project_commands) {
        Ok(doctor) => {
            print(&doctor.agents, json);
            ExitCode::from(u8::try_from(doctor.exit).unwrap_or(super::REFUSED))
        }
        Err(error) => refuse(session.env(), &error, json),
    }
}

/// The probe of the app, with the project at the invocation folder open when it can be, so its stages
/// apply; `[agents.<id>] command` overrides apply only when `use_project_commands` is explicitly set.
/// A folder that cannot be opened leaves the built-in defaults.
fn doctor(session: &Session, use_project_commands: bool) -> Result<Doctor, Error> {
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let folder = session.env().cwd().to_path_buf();
    let _ = app::project_open(&ctx, &folder);
    if use_project_commands {
        app::doctor_project_commands(&ctx)
    } else {
        app::doctor(&ctx)
    }
}

/// An environment for showing an error when the process's own cannot be read: no home, so paths stay whole.
fn fallback_env() -> Env {
    Env::new(std::env::temp_dir()).expect("the temporary folder is absolute")
}

fn print(agents: &[AgentStatusDto], json: bool) {
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(agents).expect("agent statuses serialize")
        );
        return;
    }
    for agent in agents {
        println!("{}", line(agent));
    }
}

/// `Name: status (version, source); next step`, one agent per line.
fn line(agent: &AgentStatusDto) -> String {
    let status = serde_json::to_value(agent.status)
        .ok()
        .and_then(|value| value.as_str().map(str::to_string));
    let mut text = format!("{}: {}", agent.name, status.unwrap_or_default());
    let found: Vec<&str> = [&agent.version, &agent.source]
        .into_iter()
        .flatten()
        .map(String::as_str)
        .collect();
    if !found.is_empty() {
        text.push_str(&format!(" ({})", found.join(", ")));
    }
    if let Some(details) = &agent.opencode {
        if let Some(count) = details.saved_connections {
            text.push_str(&format!("; saved provider connections: {count}"));
        }
        if let Some(model) = &details.default_model {
            text.push_str(&format!("; default model: {model}"));
        }
    }
    match (agent.status, &agent.problem) {
        (_, Some(problem)) => text.push_str(&format!("; {}: {}", problem.code, problem.message)),
        (Status::Missing, None) => text.push_str(&format!("; install: {}", agent.install_url)),
        (Status::NotSignedIn, None) => {
            text.push_str(&format!("; sign in: {}", agent.login_hint.text))
        }
        _ => {}
    }
    text.replace('\n', " ")
}
