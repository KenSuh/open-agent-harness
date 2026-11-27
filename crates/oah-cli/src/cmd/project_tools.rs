// oah project-tools status: 앱과 같은 핵심 작업으로 위키·영향 지도·Jev 카드 상태를 글이나 JSON 으로 보이며 아무것도 쓰거나 실행하지 않는다.
use super::refuse;
use clap::{Arg, ArgAction, ArgMatches, Command};
use oah_core::Error;
use oah_core::app::{self, Session};
use oah_core::dto::{ProjectToolsDto, ToolPathBaseDto};
use oah_core::paths::Env;
use oah_core::tasks::{CancelToken, NoProgress};
use std::process::ExitCode;

pub fn command() -> Command {
    Command::new("project-tools")
        .about("Show what the project has for its wiki, impact map and Jev")
        .subcommand_required(true)
        .arg_required_else_help(true)
        .subcommand(
            Command::new("status")
                .about("Show the Project Wiki, Impact Map and Jev status; reads only")
                .arg(
                    Arg::new("json")
                        .long("json")
                        .action(ArgAction::SetTrue)
                        .help("Print the project tool cards as JSON"),
                ),
        )
}

pub fn run(matches: &ArgMatches) -> ExitCode {
    let Some(("status", matches)) = matches.subcommand() else {
        return ExitCode::from(super::USAGE);
    };
    let json = matches.get_flag("json");
    let env = match Env::from_process() {
        Ok(env) => env,
        Err(error) => return refuse(&super::fallback_env(), &Error::from(error), json),
    };
    let session = Session::new(env);
    match status(&session) {
        Ok(tools) => {
            print(&tools, json);
            ExitCode::SUCCESS
        }
        Err(error) => refuse(session.env(), &error, json),
    }
}

/// The same cards the app shows, for the project the invocation folder belongs to. The project is found without
/// starting Git or any other program, so this works on a machine without Git and with an empty `PATH`.
fn status(session: &Session) -> Result<ProjectToolsDto, Error> {
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    app::project_tools_status_in(&ctx, session.env().cwd())
}

fn print(tools: &ProjectToolsDto, json: bool) {
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(tools).expect("the project tool cards serialize")
        );
        return;
    }
    for tool in &tools.tools {
        println!("{}: {}", tool.name, tool.status);
        if let Some(reason) = &tool.reason {
            println!("  Why: {reason}");
        }
        println!("  Next step: {}", tool.next_step);
        for path in &tool.paths {
            let base = match path.base {
                ToolPathBaseDto::Project => "in the project",
                ToolPathBaseDto::GitCommonDir => "in the Git folder",
                ToolPathBaseDto::StateDir => "in the OAH state folder",
            };
            println!("  {}: {} ({base})", path.label, path.path);
        }
        for note in &tool.notes {
            println!("  Note: {note}");
        }
    }
}
