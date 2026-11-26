// oah rules check|sync: 앱과 같은 핵심 작업으로 AGENTS.md 블록과 얇은 파일의 어긋남을 보이거나(쓰지 않음) 다시 맞춘다.
use super::refuse;
use clap::{Arg, ArgAction, ArgMatches, Command};
use oah_core::Error;
use oah_core::app::{self, Session};
use oah_core::dto::{AgentsProbeReq, Empty, RulesStateDto, RulesSyncDto};
use oah_core::paths::Env;
use oah_core::tasks::{CancelToken, NoProgress};
use serde::Serialize;
use std::process::ExitCode;

fn json_flag() -> Arg {
    Arg::new("json")
        .long("json")
        .action(ArgAction::SetTrue)
        .help("Print the result as JSON")
}

pub fn command() -> Command {
    Command::new("rules")
        .about("Check or restore the OAH block in AGENTS.md and the thin rule files")
        .subcommand_required(true)
        .arg_required_else_help(true)
        .subcommand(
            Command::new("check")
                .about(
                    "Report drift without writing anything; exit 1 when a sync would change a file",
                )
                .arg(json_flag()),
        )
        .subcommand(
            Command::new("sync")
                .about("Write the OAH block and thin rule files from .oah/config.toml")
                .arg(json_flag()),
        )
}

pub fn run(matches: &ArgMatches) -> ExitCode {
    let (verb, matches) = match matches.subcommand() {
        Some((verb @ ("check" | "sync"), matches)) => (verb, matches),
        _ => return ExitCode::from(super::USAGE),
    };
    let json = matches.get_flag("json");
    let env = match Env::from_process() {
        Ok(env) => env,
        Err(error) => return refuse(&super::fallback_env(), &Error::from(error), json),
    };
    let session = Session::new(env);
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    if let Err(error) = open(&ctx) {
        return refuse(session.env(), &error, json);
    }
    if verb == "check" {
        match app::rules_check(&ctx, Empty {}) {
            Ok(state) => {
                emit(&state, json, || print_state(&state));
                exit(state.synced)
            }
            Err(error) => refuse(session.env(), &error, json),
        }
    } else {
        match app::rules_sync(&ctx, Empty {}) {
            Ok(result) => {
                emit(&result, json, || print_sync(&result));
                exit(result.synced)
            }
            Err(error) => refuse(session.env(), &error, json),
        }
    }
}

/// Opens the project at the invocation folder read-only and detects the agents (no sign-in), the input of the
/// thin-file rule; the app uses its own detection results the same way.
fn open(ctx: &app::Ctx<'_>) -> Result<(), Error> {
    let folder = ctx.env.cwd().to_path_buf();
    app::project_open(ctx, &folder)?;
    let req = AgentsProbeReq {
        agents: None,
        sign_in: false,
        focus: false,
        opencode_details: None,
    };
    app::agents_probe(ctx, req).map(drop)
}

fn exit(ok: bool) -> ExitCode {
    if ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(super::REFUSED)
    }
}

fn emit<T: Serialize>(value: &T, json: bool, text: impl FnOnce()) {
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(value).expect("the rules result serializes")
        );
    } else {
        text();
    }
}

/// v0.4.0's drift lines (`CODE: message`), the refusal a sync would hit, or that the rules are in sync.
pub fn print_state(state: &RulesStateDto) {
    if let Some(error) = &state.error {
        println!(
            "Rules: not synced. {}: {}",
            error.code,
            error.message.replace('\n', " ")
        );
        return;
    }
    if state.drift.is_empty() {
        println!("Rules: in sync");
        return;
    }
    println!("Rules: not synced");
    for line in &state.drift {
        println!("{}: {}", line.code, line.message);
    }
}

fn print_sync(result: &RulesSyncDto) {
    if result.written.is_empty() && result.synced {
        println!("Rules: already in sync");
        return;
    }
    for write in &result.written {
        let action = serde_json::to_value(write.action).ok();
        let status = serde_json::to_value(write.status).ok();
        println!(
            "{}: {} ({})",
            write.path,
            action.as_ref().and_then(|v| v.as_str()).unwrap_or_default(),
            status.as_ref().and_then(|v| v.as_str()).unwrap_or_default()
        );
    }
    if let Some(error) = &result.error {
        println!(
            "Rules: not synced. {}: {}",
            error.code,
            error.message.replace('\n', " ")
        );
    }
}
