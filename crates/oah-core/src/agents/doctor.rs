// doctor(v0.4.0 이식): 도구 하나의 상태 결정, 파이썬 행, 종료 코드와 JSON 출력을 고정 기대값 그대로 낸다.
//! The v0.4.0 `doctor` functions the `doctor` conformance area pins (v0.4:src/oah/doctor.py:28-118, 159-160,
//! 516-541), ported as given (DECISIONS F-10). 1.0's `oah doctor` reports agents; these stay the frozen oracle
//! and share their probe steps with the agent adapters.

use super::catalog::{Requirement, Tool, catalog};
use super::detection::Status;
use super::probe::Probe;
use super::problem::{AgentError, exit_text, seconds};
use super::steps::{SignInRun, VersionRun, run_sign_in, run_version, semver};
use crate::error::{Coded, hint_for};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::time::Duration;

/// One row of v0.4.0's doctor output.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolStatus {
    pub name: String,
    pub requirement: Requirement,
    pub status: Status,
    pub version: Option<String>,
    pub source: Option<String>,
    pub note: String,
}

/// A row name `render_json` has no catalog entry for (v0.4.0 raised `KeyError`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownTool(pub String);

/// The status decision for one catalog tool: resolve, version run, version floor, then the sign-in status
/// command (v0.4:src/oah/doctor.py:76-118). `gates` says whether the project uses gates.
pub fn check_tool(tool: &Tool, probe: &dyn Probe, timeout: Duration, gates: bool) -> ToolStatus {
    let (requirement, purpose) = requirement(tool, gates);
    let row = |status, version, source, note| ToolStatus {
        name: tool.name.to_string(),
        requirement,
        status,
        version,
        source,
        note,
    };
    let (program, version) = match run_version(probe, tool.name, &["--version"], timeout) {
        VersionRun::Missing => return row(Status::Missing, None, None, purpose),
        VersionRun::UnsupportedShim { path, reason } => {
            let source = path.display().to_string();
            let note = shim_note(&AgentError::UnsupportedShim { path, reason });
            return row(Status::UnsupportedShim, None, Some(source), note);
        }
        VersionRun::Launch { program, reason } => {
            let note = format!("{purpose}; --version could not launch: {reason}");
            return row(Status::Error, None, Some(shown(&program.source)), note);
        }
        VersionRun::TimedOut { program } => {
            let note = format!("{purpose}; --version timed out after {}s", seconds(timeout));
            return row(Status::Error, None, Some(shown(&program.source)), note);
        }
        VersionRun::Exited {
            program,
            exit,
            version,
        } if exit != Some(0) => {
            let note = format!("{purpose}; --version exited with code {}", exit_text(exit));
            return row(Status::Error, version, Some(shown(&program.source)), note);
        }
        VersionRun::Exited {
            program, version, ..
        } => (program, version),
    };
    let source = Some(shown(&program.source));
    if let Some(wanted) = tool.min_version
        && semver(version.as_deref().unwrap_or("")).is_none_or(|found| found < wanted)
    {
        let [major, minor, patch] = wanted;
        let note = format!("{purpose}; needs {major}.{minor}.{patch} or newer");
        return row(Status::TooOld, version, source, note);
    }
    let Some(sign_in) = tool.sign_in else {
        return row(Status::Ok, version, source, purpose);
    };
    let command = format!("'{} {}'", tool.name, sign_in.args.join(" "));
    match run_sign_in(probe, &program, sign_in.args, timeout) {
        SignInRun::Launch(reason) => {
            let note = format!("{purpose}; {command} could not launch: {reason}");
            row(Status::Error, version, source, note)
        }
        SignInRun::TimedOut => {
            let note = format!("{purpose}; {command} timed out");
            row(Status::Error, version, source, note)
        }
        SignInRun::SignedOut => {
            let note = format!("{purpose}; not signed in, run: {}", sign_in.login);
            row(Status::NotSignedIn, version, source, note)
        }
        SignInRun::SignedIn => row(Status::Ok, version, source, format!("{purpose}; signed in")),
    }
}

/// The Python row: `ok` from 3.11, else `too-old` (v0.4:src/oah/doctor.py:47-51). `source` is the running
/// interpreter, which v0.4.0 read from `sys.executable`.
pub fn python_status(version_info: &[u64], source: &str) -> ToolStatus {
    let version: Vec<String> = version_info.iter().take(3).map(u64::to_string).collect();
    let supported = version_info[..version_info.len().min(2)] >= [3, 11][..];
    let (status, note) = if supported {
        (Status::Ok, "runs oah")
    } else {
        (Status::TooOld, "oah needs Python 3.11 or newer")
    };
    ToolStatus {
        name: String::from("python"),
        requirement: Requirement::Required,
        status,
        version: Some(version.join(".")),
        source: Some(source.to_string()),
        note: note.to_string(),
    }
}

/// `0` when every required row is `ok`, else `1` (v0.4:src/oah/doctor.py:159-160).
pub fn exit_code(statuses: &[ToolStatus]) -> i32 {
    let usable = statuses
        .iter()
        .filter(|status| status.requirement == Requirement::Required)
        .all(|status| status.status == Status::Ok);
    if usable { 0 } else { 1 }
}

/// v0.4.0's `doctor --json` document for `statuses` without notices: each row with its install texts and page
/// for `platform` (v0.4:src/oah/doctor.py:516-541).
pub fn render_json(statuses: &[ToolStatus], platform: &str) -> Result<String, UnknownTool> {
    let tools = catalog();
    let mut rows = Vec::with_capacity(statuses.len());
    for status in statuses {
        let tool = tools
            .iter()
            .find(|tool| tool.name == status.name)
            .ok_or_else(|| UnknownTool(status.name.clone()))?;
        let guide = tool.guide(platform);
        let mut row = json!(status);
        row["install"] = json!(guide.install);
        row["url"] = Value::from(guide.url);
        rows.push(row);
    }
    let payload = json!({
        "ok": exit_code(statuses) == 0,
        "platform": platform,
        "tools": rows,
        "mcp_notices": [],
        "model_notices": [],
        "model_choices": [],
    });
    Ok(format!("{payload:#}"))
}

fn requirement(tool: &Tool, gates: bool) -> (Requirement, String) {
    if tool.required_for_gates && gates && tool.requirement != Requirement::Required {
        let purpose = format!("{}; required because this project uses gates", tool.purpose);
        return (Requirement::Required, purpose);
    }
    (tool.requirement, tool.purpose.to_string())
}

/// v0.4.0's text of an unsupported batch shim: `CODE: path: reason. hint`.
fn shim_note(error: &AgentError) -> String {
    let hint = hint_for(error.code()).unwrap_or_default();
    format!("{}: {}. {hint}", error.code(), error.message())
}

fn shown(path: &std::path::Path) -> String {
    path.display().to_string()
}
