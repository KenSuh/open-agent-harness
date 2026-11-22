// oah 명령줄 진입점: clap 으로 인자를 읽어 --version, doctor, sections, rules, skills, 옛 hook 답을 핵심 작업에 넘기고 종료 코드를 정한다.
mod cmd;

use clap::{Arg, ArgAction, Command};
use std::process::ExitCode;

/// The command line (CONTRACTS C5, C15): `oah --version`, `oah doctor [--json]`, `oah sections show [--json]`,
/// `oah rules check|sync [--json]`, `oah skills review --github OWNER/REPO --path FOLDER [--json]` and the hidden
/// legacy `oah hook`.
fn cli() -> Command {
    Command::new("oah")
        .about("Open Agent Harness")
        .disable_version_flag(true)
        .arg(
            Arg::new("version")
                .long("version")
                .action(ArgAction::SetTrue)
                .help("Print the version"),
        )
        .args_conflicts_with_subcommands(true)
        .arg_required_else_help(true)
        .subcommand(cmd::doctor::command())
        .subcommand(cmd::sections::command())
        .subcommand(cmd::rules::command())
        .subcommand(cmd::skills::command())
        .subcommand(cmd::hook::command())
}

fn main() -> ExitCode {
    let matches = match cli().try_get_matches() {
        Ok(matches) => matches,
        Err(error) => {
            let _ = error.print();
            return ExitCode::from(u8::try_from(error.exit_code()).unwrap_or(cmd::USAGE));
        }
    };
    match matches.subcommand() {
        Some(("doctor", matches)) => cmd::doctor::run(matches),
        Some(("sections", matches)) => cmd::sections::run(matches),
        Some(("rules", matches)) => cmd::rules::run(matches),
        Some(("skills", matches)) => cmd::skills::run(matches),
        Some(("hook", matches)) => cmd::hook::run(matches),
        _ => {
            println!("oah {}", oah_core::VERSION);
            ExitCode::SUCCESS
        }
    }
}
