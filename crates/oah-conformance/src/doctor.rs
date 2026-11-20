// doctor 영역 실행기: 고정 사례의 입력을 실제 oah-core doctor 함수 호출로 바꾸고 결과를 기대값과 JSON 으로 비교한다.
//! The `doctor` area. A case's `function` names the v0.4.0 function it pins; the matching real core function runs
//! with the case's inputs and its result must equal `expect.json`'s `ok` as parsed JSON. `_check_tool` rows run
//! against [`FixtureProbe`], which answers only the resolve and the command lines the row gives.

use oah_core::agents::doctor::{ToolStatus, check_tool, python_status, render_json};
use oah_core::agents::{
    Os, Probe, Resolution, Resolved, RunError, RunOutput, catalog, first_line, signed_in,
};
use oah_core::paths::Env;
use serde_json::{Map, Value as Json, json};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

/// What v0.4.0's fixture recorded as `sys.executable` (the case note says so).
const PYTHON_EXECUTABLE: &str = "<python-executable>";

pub(crate) fn run_case(
    function: &str,
    input: &Map<String, Json>,
    expect: &Json,
) -> Result<(), String> {
    let wanted = expect
        .get("ok")
        .ok_or("the case expects an error; the doctor functions return values")?;
    let actual = match function {
        "oah.setup.guide.signed_in" => table(input, |call| {
            Ok(json!(signed_in(exit_code(call)?, text(call, "stdout")?)))
        })?,
        "oah.doctor.first_line" => table(input, |call| Ok(json!(first_line(text(call, "text")?))))?,
        "oah.doctor._check_tool" => table(input, check_tool_row)?,
        "oah.doctor.python_status" => table(input, python_row)?,
        "oah.doctor.render_json" => rendered(input)?,
        other => return Err(format!("no core function is registered for {other}")),
    };
    if actual == *wanted {
        Ok(())
    } else {
        Err(format!("expected {wanted}\n    actual {actual}"))
    }
}

/// A table case: `calls` maps row names to arguments; the result maps the same names to results.
fn table(
    input: &Map<String, Json>,
    call: impl Fn(&Map<String, Json>) -> Result<Json, String>,
) -> Result<Json, String> {
    let calls = input
        .get("calls")
        .and_then(Json::as_object)
        .ok_or("input.json has no calls object")?;
    let mut results = Map::new();
    for (name, arguments) in calls {
        let arguments = arguments
            .as_object()
            .ok_or(format!("{name}: not an object"))?;
        let result = call(arguments).map_err(|detail| format!("{name}: {detail}"))?;
        results.insert(name.clone(), result);
    }
    Ok(Json::Object(results))
}

fn text<'a>(call: &'a Map<String, Json>, key: &str) -> Result<&'a str, String> {
    call.get(key)
        .and_then(Json::as_str)
        .ok_or(format!("{key} is not a string"))
}

/// A Python return code: an integer, or `null` for none.
fn exit_code(call: &Map<String, Json>) -> Result<Option<i32>, String> {
    match call.get("returncode") {
        Some(Json::Null) => Ok(None),
        Some(code) => code
            .as_i64()
            .and_then(|code| i32::try_from(code).ok())
            .map(Some)
            .ok_or(format!("returncode {code} is not an exit code")),
        None => Err("returncode is missing".into()),
    }
}

fn check_tool_row(spec: &Map<String, Json>) -> Result<Json, String> {
    let name = text(spec, "tool")?;
    let tool = catalog()
        .into_iter()
        .find(|tool| tool.name == name)
        .ok_or(format!("{name} is not a catalog tool"))?;
    let timeout = spec
        .get("timeout")
        .and_then(Json::as_f64)
        .ok_or("timeout is not a number")?;
    let gates = spec
        .get("gates")
        .and_then(Json::as_bool)
        .ok_or("gates is not a boolean")?;
    let probe = FixtureProbe::new(name, spec)?;
    let status = check_tool(&tool, &probe, Duration::from_secs_f64(timeout), gates);
    let unexpected = probe
        .unexpected
        .into_inner()
        .unwrap_or_else(|poison| poison.into_inner());
    if !unexpected.is_empty() {
        return Err(format!("unexpected probe use: {unexpected:?}"));
    }
    serde_json::to_value(status).map_err(|error| error.to_string())
}

fn python_row(call: &Map<String, Json>) -> Result<Json, String> {
    let version_info: Vec<u64> = call
        .get("version_info")
        .and_then(Json::as_array)
        .ok_or("version_info is not a list")?
        .iter()
        .map(|part| part.as_u64().ok_or(format!("{part} is not a version part")))
        .collect::<Result<_, _>>()?;
    serde_json::to_value(python_status(&version_info, PYTHON_EXECUTABLE))
        .map_err(|error| error.to_string())
}

fn rendered(input: &Map<String, Json>) -> Result<Json, String> {
    let statuses: Vec<ToolStatus> = input
        .get("statuses")
        .cloned()
        .map(serde_json::from_value)
        .ok_or("statuses is missing")?
        .map_err(|error| format!("statuses: {error}"))?;
    let platform = text(input, "platform")?;
    let document = render_json(&statuses, platform)
        .map_err(|unknown| format!("no catalog row {}", unknown.0))?;
    serde_json::from_str(&document)
        .map_err(|error| format!("render_json printed invalid JSON: {error}"))
}

/// A probe that answers one `_check_tool` row as v0.4.0's extractor did: `resolve` for the tool's own name, and
/// `runs` keyed by the arguments after the resolved program, joined with spaces. Anything else is recorded and
/// fails the row.
struct FixtureProbe {
    env: Env,
    command: String,
    resolve: Json,
    runs: Map<String, Json>,
    unexpected: Mutex<Vec<String>>,
}

impl FixtureProbe {
    fn new(command: &str, spec: &Map<String, Json>) -> Result<FixtureProbe, String> {
        Ok(FixtureProbe {
            env: Env::new(std::env::temp_dir()).map_err(|error| format!("{error:?}"))?,
            command: command.to_string(),
            resolve: spec.get("resolve").cloned().ok_or("resolve is missing")?,
            runs: spec
                .get("runs")
                .and_then(Json::as_object)
                .cloned()
                .ok_or("runs is not an object")?,
            unexpected: Mutex::new(Vec::new()),
        })
    }

    fn note(&self, what: String) {
        self.unexpected
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .push(what);
    }

    /// The resolved program the row gives (`kind` is not read: v0.4.0's `_check_tool` never read it either).
    fn resolved(&self) -> Option<Resolved> {
        let argv = self.resolve.get("argv")?.as_array()?;
        Some(Resolved {
            argv: argv
                .iter()
                .map(|part| OsString::from(part.as_str().unwrap_or_default()))
                .collect(),
            source: PathBuf::from(self.resolve.get("source")?.as_str()?),
        })
    }
}

impl Probe for FixtureProbe {
    fn env(&self) -> &Env {
        &self.env
    }

    fn os(&self) -> Os {
        Os::host()
    }

    fn resolve_in(&self, command: &str, folder: &Path) -> Resolution {
        self.note(format!("resolve {command} in {}", folder.display()));
        Resolution::Missing
    }

    fn resolve(&self, command: &str) -> Resolution {
        if command != self.command {
            self.note(format!("resolve {command}"));
        }
        if self.resolve.is_null() {
            return Resolution::Missing;
        }
        if let Some(shim) = self.resolve.get("unsupported_shim") {
            let field = |key: &str| {
                shim.get(key)
                    .and_then(Json::as_str)
                    .unwrap_or_default()
                    .to_string()
            };
            return Resolution::UnsupportedShim {
                path: PathBuf::from(field("path")),
                reason: field("reason"),
            };
        }
        self.resolved()
            .map_or(Resolution::Missing, Resolution::Found)
    }

    fn run(
        &self,
        program: &Resolved,
        args: &[&str],
        _deadline: Duration,
    ) -> Result<RunOutput, RunError> {
        let line = args.join(" ");
        let answer = self.runs.get(&line);
        if Some(program) != self.resolved().as_ref() || answer.is_none() {
            self.note(format!("run {:?} {line}", program.argv));
            return Err(RunError::Launch(String::from("not in the fixture")));
        }
        let answer = answer.cloned().unwrap_or_default();
        if let Some(reason) = answer.get("oserror").and_then(Json::as_str) {
            return Err(RunError::Launch(reason.to_string()));
        }
        if answer.get("timed_out").and_then(Json::as_bool) == Some(true) {
            return Err(RunError::TimedOut);
        }
        let field = |key: &str| {
            answer
                .get(key)
                .and_then(Json::as_str)
                .unwrap_or_default()
                .to_string()
        };
        Ok(RunOutput {
            exit: answer
                .get("returncode")
                .and_then(Json::as_i64)
                .and_then(|code| i32::try_from(code).ok()),
            stdout: field("stdout"),
            stderr: field("stderr"),
        })
    }

    fn exists(&self, path: &Path) -> bool {
        self.note(format!("exists {}", path.display()));
        false
    }
}
