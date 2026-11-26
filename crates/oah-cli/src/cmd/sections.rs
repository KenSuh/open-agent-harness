// oah sections show: 앱과 같은 핵심 작업으로 섹션 배정·배너·규칙 상태를 글이나 JSON 으로 보이며 아무것도 쓰지 않는다.
use super::refuse;
use clap::{Arg, ArgAction, ArgMatches, Command};
use oah_core::Error;
use oah_core::app::{self, Session};
use oah_core::dto::{AgentsProbeReq, BannerDto, Empty, WorkflowDto};
use oah_core::paths::Env;
use oah_core::tasks::{CancelToken, NoProgress};
use std::process::ExitCode;

pub fn command() -> Command {
    Command::new("sections")
        .about("Show who does which section of the team workflow")
        .subcommand_required(true)
        .arg_required_else_help(true)
        .subcommand(
            Command::new("show")
                .about("Show the sections, their agents, the status banner and the rules state")
                .arg(
                    Arg::new("json")
                        .long("json")
                        .action(ArgAction::SetTrue)
                        .help("Print the Team Workflow page as JSON"),
                ),
        )
}

pub fn run(matches: &ArgMatches) -> ExitCode {
    let Some(("show", matches)) = matches.subcommand() else {
        return ExitCode::from(super::USAGE);
    };
    let json = matches.get_flag("json");
    let env = match Env::from_process() {
        Ok(env) => env,
        Err(error) => return refuse(&super::fallback_env(), &Error::from(error), json),
    };
    let session = Session::new(env);
    match show(&session) {
        Ok(page) => {
            print(&page, json);
            ExitCode::SUCCESS
        }
        Err(error) => refuse(session.env(), &error, json),
    }
}

/// The app's operations on the project at the invocation folder: open it read-only, check every agent with
/// sign-in (the banner needs it), then `sections_show`.
fn show(session: &Session) -> Result<WorkflowDto, Error> {
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let folder = session.env().cwd().to_path_buf();
    app::project_open(&ctx, &folder)?;
    let req = AgentsProbeReq {
        agents: None,
        sign_in: true,
        focus: false,
        opencode_details: None,
    };
    app::agents_probe(&ctx, req)?;
    app::sections_show(&ctx, Empty {})
}

fn banner_message(banner: &BannerDto) -> &str {
    match banner {
        BannerDto::NotInstalled { message, .. }
        | BannerDto::NotSignedIn { message, .. }
        | BannerDto::SignInUnknown { message, .. }
        | BannerDto::NotIndependent { message, .. }
        | BannerDto::Ready { message } => message,
    }
}

fn print(page: &WorkflowDto, json: bool) {
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(page).expect("the workflow page serializes")
        );
        return;
    }
    let format = serde_json::to_value(page.config_format)
        .ok()
        .and_then(|value| value.as_str().map(str::to_string))
        .unwrap_or_default();
    println!("Team workflow (config: {format})");
    for section in &page.sections {
        let agent = section
            .agent
            .map_or("not assigned", |agent| agent.0.as_str());
        println!(
            "  {} [{}]: {agent} ({})",
            section.label, section.id, section.source
        );
    }
    println!("Status: {}", banner_message(&page.banner));
    for note in &page.notes {
        println!("Note: {note}");
    }
    super::rules::print_state(&page.rules);
}
