use super::refuse;
use clap::{Arg, ArgAction, ArgMatches, Command};
use oah_core::Error;
use oah_core::app::{self, Session};
use oah_core::config::ToolsChange;
use oah_core::dto::{
    ProjectToolsApplyDto, ProjectToolsApplyReq, ProjectToolsDto, ProjectToolsPlanDto,
    ProjectToolsPlanReq, ToolPathBaseDto, ToolSettingValue,
};
use oah_core::paths::Env;
use oah_core::tasks::{CancelToken, NoProgress};
use serde::Serialize;
use std::process::ExitCode;

pub fn command() -> Command {
    Command::new("project-tools")
        .about("Inspect project tools and review changes to their settings")
        .subcommand_required(true)
        .arg_required_else_help(true)
        .subcommand(
            Command::new("status")
                .about("Show the Project Wiki, Impact Map and Jev status; reads only")
                .arg(json_arg()),
        )
        .subcommand(
            settings_args(Command::new("plan"))
                .about("Show the current settings or preview changes without writing"),
        )
        .subcommand(
            settings_args(Command::new("apply"))
                .about("Save the same settings edits shown in an approved preview")
                .arg(Arg::new("plan-id").required(true).value_name("PLAN_ID"))
                .arg(
                    Arg::new("approve-upgrade")
                        .long("approve-upgrade")
                        .value_name("FINGERPRINT")
                        .help("Approve the one-time config upgrade with the fingerprint from the preview"),
                ),
        )
}

fn json_arg() -> Arg {
    Arg::new("json")
        .long("json")
        .action(ArgAction::SetTrue)
        .help("Print one JSON value")
}

fn settings_args(command: Command) -> Command {
    command
        .arg(json_arg())
        .arg(
            Arg::new("wiki-dir")
                .long("wiki-dir")
                .value_name("RELATIVE_FOLDER"),
        )
        .arg(
            Arg::new("impact-wiki-dir")
                .long("impact-wiki-dir")
                .action(ArgAction::Append)
                .value_name("RELATIVE_FOLDER")
                .help("Set a list of wiki folders for Impact Map; repeat to include several"),
        )
        .arg(
            Arg::new("inherit-wiki-dirs")
                .long("inherit-wiki-dirs")
                .conflicts_with("impact-wiki-dir")
                .action(ArgAction::SetTrue)
                .help("Remove the separate list of Impact Map wiki folders"),
        )
        .arg(
            Arg::new("graph-file")
                .long("graph-file")
                .value_name("RELATIVE_FILE"),
        )
        .arg(
            Arg::new("impact-rerank")
                .long("impact-rerank")
                .value_parser(clap::value_parser!(bool))
                .value_name("BOOL"),
        )
        .arg(
            Arg::new("jev-enabled")
                .long("jev-enabled")
                .value_parser(clap::value_parser!(bool))
                .value_name("BOOL")
                .help("Set the project's Jev flag"),
        )
}

pub fn run(matches: &ArgMatches) -> ExitCode {
    let Some((action, matches)) = matches.subcommand() else {
        return ExitCode::from(super::USAGE);
    };
    let json = matches.get_flag("json");
    let env = match Env::from_process() {
        Ok(env) => env,
        Err(error) => return refuse(&super::fallback_env(), &Error::from(error), json),
    };
    let session = Session::new(env);
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let folder = session.env().cwd();
    let outcome = match action {
        "status" => app::project_tools_status_in(&ctx, folder).map(|tools| print(&tools, json)),
        "plan" => app::project_tools_plan_in(
            &ctx,
            folder,
            ProjectToolsPlanReq {
                edits: edits(matches),
            },
        )
        .map(|plan| print_plan(&plan, json)),
        "apply" => app::project_tools_apply_in(
            &ctx,
            folder,
            ProjectToolsApplyReq {
                edits: edits(matches),
                plan_id: matches
                    .get_one::<String>("plan-id")
                    .expect("required plan id")
                    .clone(),
                approve_upgrade: matches.get_one::<String>("approve-upgrade").cloned(),
            },
        )
        .map(|saved| print_saved(&saved, json)),
        _ => return ExitCode::from(super::USAGE),
    };
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => refuse(session.env(), &error, json),
    }
}

fn edits(matches: &ArgMatches) -> Vec<ToolsChange> {
    let mut edits = Vec::new();
    if let Some(value) = matches.get_one::<String>("wiki-dir") {
        edits.push(ToolsChange::WikiDir(value.clone()));
    }
    if let Some(values) = matches.get_many::<String>("impact-wiki-dir") {
        edits.push(ToolsChange::ImpactWikiDirs(Some(values.cloned().collect())));
    } else if matches.get_flag("inherit-wiki-dirs") {
        edits.push(ToolsChange::ImpactWikiDirs(None));
    }
    if let Some(value) = matches.get_one::<String>("graph-file") {
        edits.push(ToolsChange::ImpactGraphFile(value.clone()));
    }
    if let Some(value) = matches.get_one::<bool>("impact-rerank") {
        edits.push(ToolsChange::ImpactRerank(*value));
    }
    if let Some(value) = matches.get_one::<bool>("jev-enabled") {
        edits.push(ToolsChange::JevEnabled(*value));
    }
    edits
}

fn print_json(value: &impl Serialize) {
    println!(
        "{}",
        serde_json::to_string_pretty(value).expect("project tools results serialize")
    );
}

fn value_text(value: Option<&ToolSettingValue>) -> String {
    match value {
        Some(value) => serde_json::to_string(value).expect("a tool setting serializes"),
        None => "(unset)".to_owned(),
    }
}

fn print_plan(plan: &ProjectToolsPlanDto, json: bool) {
    if json {
        print_json(plan);
        return;
    }
    println!("Plan {} for {}", plan.plan_id, plan.config_path);
    if plan.changes.is_empty() {
        println!("No edits to save. Current settings:");
        print_json(&plan.settings);
    }
    for change in &plan.changes {
        println!(
            "  {}: {} → {}",
            change.key,
            value_text(change.before.as_ref()),
            value_text(change.after.as_ref())
        );
    }
    for kept in &plan.kept {
        println!("  Kept {} ({})", kept.key, kept.reason);
    }
    if let Some(upgrade) = &plan.upgrade {
        println!("{}", upgrade.message);
        println!("Upgrade fingerprint: {}", upgrade.fingerprint);
    }
    if let Some(backup) = &plan.backup {
        println!("Backup {} ({:?})", backup.path, backup.state);
    }
    for notice in &plan.notices {
        println!("{}: {}", notice.code, notice.message);
    }
}

fn print_saved(saved: &ProjectToolsApplyDto, json: bool) {
    if json {
        print_json(saved);
        return;
    }
    if saved.saved {
        println!("Saved the project tool settings.");
    } else {
        println!("The settings already match the preview.");
    }
    if let Some(backup) = saved.backup {
        println!("Original settings kept in {backup}");
    }
}

fn print(tools: &ProjectToolsDto, json: bool) {
    if json {
        print_json(tools);
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
