// 적합성 실행기: 모든 영역의 매니페스트와 사례를 검사하고, 켜진 영역(config)만 실제 oah-core 로 실행해 기대값과 비교한다.
//! Runs the golden fixtures against the real `oah-core`.
//!
//! Every area's manifest and case folders are checked for completeness. Which areas run is decided here, in
//! [`ENABLED`], never by the frozen manifests (which all stay `pending`). A case of an enabled area is
//! materialized into a temporary folder, passed to the real core API its declared function names, and its
//! parsed result is compared with `expect.json`. Nothing is ever written under the fixture tree.

use oah_core::config::{ConfigFile, ProjectConfig, Table, Value, notice_codes};
use serde_json::{Map, Value as Json};
use std::collections::BTreeSet;
use std::fmt;
use std::path::{Component, Path, PathBuf};

/// The areas this build runs. Every other area is checked for completeness and reported as pending.
pub const ENABLED: &[&str] = &["config"];

/// The fixture cases whose v0.4.0 result 1.0 deliberately changes, and how this runner adapts each. Design §3.1:
/// "an unknown key inside a table 1.0 reads is a notice (`OAH_CONFIG_UNKNOWN_KEY`, non-fatal)". The frozen
/// expectation stays v0.4.0's refusal; the adaptation requires the notice with that refusal's exact message and
/// the section's defaults otherwise. A manifest deviation missing from this list fails the run.
pub const CONFIG_DEVIATIONS: &[&str] = &[
    "unknown-key-stages",
    "unknown-key-nested",
    "unknown-key-codex-permission",
];

/// Whether an area ran.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AreaState {
    Enabled,
    Pending,
}

impl AreaState {
    pub fn as_str(self) -> &'static str {
        match self {
            AreaState::Enabled => "enabled",
            AreaState::Pending => "pending",
        }
    }
}

/// One problem, naming the area and case (or the manifest).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    pub area: String,
    /// The case name, or `MANIFEST.json` for a problem with the area itself.
    pub case: String,
    pub detail: String,
}

impl fmt::Display for Failure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}/{}: {}", self.area, self.case, self.detail)
    }
}

/// The result of one area.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AreaReport {
    pub area: String,
    pub state: AreaState,
    /// Cases the manifest lists.
    pub cases: usize,
    /// Cases that ran and matched (enabled areas only).
    pub passed: usize,
    pub failures: Vec<Failure>,
    /// v0.4.0 cases whose result 1.0 keeps.
    pub unchanged: usize,
    /// Cases taken from the 1.0 design rather than from v0.4.0.
    pub design: usize,
    /// v0.4.0 cases 1.0 deliberately changes.
    pub deviations: usize,
}

impl AreaReport {
    pub fn is_ok(&self) -> bool {
        self.failures.is_empty()
    }

    /// One line for logs and for independent count checks.
    pub fn summary_line(&self) -> String {
        match self.state {
            AreaState::Enabled => format!(
                "CONFORMANCE area={} state=enabled cases={} passed={} failed={} unchanged={} design={} deviations={}",
                self.area,
                self.cases,
                self.passed,
                self.failures.len(),
                self.unchanged,
                self.design,
                self.deviations
            ),
            AreaState::Pending => format!(
                "CONFORMANCE area={} state=pending cases={} problems={}",
                self.area,
                self.cases,
                self.failures.len()
            ),
        }
    }
}

/// Runs every area under `root` (the `conformance/v0.4.0` folder), in name order.
pub fn run_all(root: &Path) -> Vec<AreaReport> {
    let mut reports: Vec<AreaReport> = crate::list_areas(root)
        .iter()
        .map(|area| run_area(root, area))
        .collect();
    for enabled in ENABLED {
        if !reports.iter().any(|report| report.area == *enabled) {
            reports.push(AreaReport {
                area: enabled.to_string(),
                state: AreaState::Enabled,
                cases: 0,
                passed: 0,
                failures: vec![Failure {
                    area: enabled.to_string(),
                    case: "MANIFEST.json".into(),
                    detail: "the enabled area has no folder".into(),
                }],
                unchanged: 0,
                design: 0,
                deviations: 0,
            });
        }
    }
    reports
}

struct Case {
    name: String,
    source: String,
    function: String,
}

struct Manifest {
    cases: Vec<Case>,
    deviations: Vec<String>,
}

/// Runs one area.
pub fn run_area(root: &Path, area: &str) -> AreaReport {
    let state = if ENABLED.contains(&area) {
        AreaState::Enabled
    } else {
        AreaState::Pending
    };
    let folder = root.join(area);
    let mut report = AreaReport {
        area: area.to_string(),
        state,
        cases: 0,
        passed: 0,
        failures: Vec::new(),
        unchanged: 0,
        design: 0,
        deviations: 0,
    };
    let fail = |report: &mut AreaReport, case: &str, detail: String| {
        report.failures.push(Failure {
            area: area.to_string(),
            case: case.to_string(),
            detail,
        });
    };
    let manifest = match read_manifest(&folder, area) {
        Ok(manifest) => manifest,
        Err(detail) => {
            fail(&mut report, "MANIFEST.json", detail);
            return report;
        }
    };
    report.cases = manifest.cases.len();
    report.design = manifest
        .cases
        .iter()
        .filter(|case| case.source == "design")
        .count();
    report.deviations = manifest.deviations.len();
    report.unchanged = manifest
        .cases
        .iter()
        .filter(|case| case.source == "v0.4.0" && !manifest.deviations.contains(&case.name))
        .count();

    let listed: BTreeSet<&str> = manifest
        .cases
        .iter()
        .map(|case| case.name.as_str())
        .collect();
    for folder_case in crate::list_areas(&folder) {
        if !listed.contains(folder_case.as_str()) {
            fail(
                &mut report,
                &folder_case,
                "a case folder the manifest does not list".into(),
            );
        }
    }
    if state == AreaState::Enabled && area == "config" {
        let registered: BTreeSet<&str> = CONFIG_DEVIATIONS.iter().copied().collect();
        let declared: BTreeSet<&str> = manifest.deviations.iter().map(String::as_str).collect();
        for case in declared.difference(&registered) {
            fail(
                &mut report,
                case,
                "the manifest declares a deviation this runner has no adaptation for".into(),
            );
        }
        for case in registered.difference(&declared) {
            fail(
                &mut report,
                case,
                "this runner adapts a deviation the manifest does not declare".into(),
            );
        }
    }

    for case in &manifest.cases {
        let dir = folder.join(&case.name);
        let loaded = load_case(&dir);
        let (input, expect) = match loaded {
            Ok(pair) => pair,
            Err(detail) => {
                fail(&mut report, &case.name, detail);
                continue;
            }
        };
        if state == AreaState::Pending {
            continue;
        }
        let deviation = manifest.deviations.contains(&case.name);
        let outcome = match area {
            "config" => run_config_case(case, deviation, &input, &expect),
            _ => Err(format!(
                "no operations are registered for the enabled area {area}"
            )),
        };
        match outcome {
            Ok(()) => report.passed += 1,
            Err(detail) => fail(&mut report, &case.name, detail),
        }
    }
    report
}

fn read_json(path: &Path) -> Result<Json, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|error| format!("cannot read {}: {error}", file_label(path)))?;
    serde_json::from_str(&text)
        .map_err(|error| format!("{} is not valid JSON: {error}", file_label(path)))
}

fn file_label(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

fn read_manifest(folder: &Path, area: &str) -> Result<Manifest, String> {
    let json = read_json(&folder.join("MANIFEST.json"))?;
    let object = json
        .as_object()
        .ok_or("the manifest is not a JSON object")?;
    let text = |key: &str| -> Result<&str, String> {
        object
            .get(key)
            .and_then(Json::as_str)
            .ok_or_else(|| format!("the manifest has no string {key}"))
    };
    if text("area")? != area {
        return Err(format!(
            "the manifest names area {} instead of {area}",
            text("area")?
        ));
    }
    if text("status")? != "pending" {
        return Err(
            "the manifest status must stay pending; areas are switched on in the runner".into(),
        );
    }
    if text("oracle")? != "v0.4.0" {
        return Err("the manifest oracle is not v0.4.0".into());
    }
    if !object.get("functions").is_some_and(Json::is_array) {
        return Err("the manifest has no functions list".into());
    }
    let cases = object
        .get("cases")
        .and_then(Json::as_array)
        .ok_or("the manifest has no cases list")?;
    if cases.is_empty() {
        return Err("the manifest lists no cases".into());
    }
    let mut out = Vec::new();
    let mut names = BTreeSet::new();
    for (index, case) in cases.iter().enumerate() {
        let field = |key: &str| {
            case.get(key)
                .and_then(Json::as_str)
                .map(str::to_string)
                .ok_or_else(|| format!("case {index} has no string {key}"))
        };
        let name = field("name")?;
        if !is_case_name(&name) {
            return Err(format!("case {index} has the invalid name {name:?}"));
        }
        if !names.insert(name.clone()) {
            return Err(format!("the manifest lists case {name} twice"));
        }
        let source = field("source")?;
        if !["v0.4.0", "design", "public"].contains(&source.as_str()) {
            return Err(format!("case {name} has the unknown source {source:?}"));
        }
        out.push(Case {
            name,
            source,
            function: field("function")?,
        });
    }
    let deviations = match object.get("deviations") {
        None => Vec::new(),
        Some(Json::Array(items)) => {
            let mut list = Vec::new();
            for item in items {
                let case = item
                    .get("case")
                    .and_then(Json::as_str)
                    .ok_or("a deviation has no case")?;
                if !names.contains(case) {
                    return Err(format!("the deviation {case} is not a listed case"));
                }
                if item.get("design").and_then(Json::as_str).is_none() {
                    return Err(format!("the deviation {case} names no design section"));
                }
                list.push(case.to_string());
            }
            list
        }
        Some(_) => return Err("deviations is not a list".into()),
    };
    Ok(Manifest {
        cases: out,
        deviations,
    })
}

fn is_case_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    !bytes.is_empty()
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

fn load_case(dir: &Path) -> Result<(Map<String, Json>, Json), String> {
    let entries = std::fs::read_dir(dir)
        .map_err(|error| format!("the case folder cannot be read: {error}"))?;
    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    if names != ["expect.json", "input.json"] {
        return Err(format!(
            "the case folder must hold exactly expect.json and input.json, not {names:?}"
        ));
    }
    let input = read_json(&dir.join("input.json"))?;
    let input = input
        .as_object()
        .cloned()
        .ok_or("input.json is not a JSON object")?;
    let expect = read_json(&dir.join("expect.json"))?;
    let object = expect
        .as_object()
        .ok_or("expect.json is not a JSON object")?;
    let keys: Vec<&str> = object.keys().map(String::as_str).collect();
    match keys.as_slice() {
        ["ok"] => {}
        ["error"] => {
            let error = object["error"]
                .as_object()
                .ok_or("expect.json error is not an object")?;
            if !error.get("code").is_some_and(Json::is_string) {
                return Err("expect.json error has no code".into());
            }
            if error.keys().any(|key| key != "code" && key != "message")
                || error
                    .get("message")
                    .is_some_and(|message| !message.is_string())
            {
                return Err("expect.json error may hold only a code and a message".into());
            }
        }
        _ => return Err("expect.json must hold exactly ok or error".into()),
    }
    Ok((input, expect))
}

/// The input files of a case written into a fresh temporary folder.
struct Materialized {
    folder: tempfile::TempDir,
    files: Vec<(PathBuf, Vec<u8>)>,
}

impl Materialized {
    fn new(input: &Map<String, Json>) -> Result<Materialized, String> {
        let folder = tempfile::tempdir()
            .map_err(|error| format!("cannot create a temporary folder: {error}"))?;
        let mut files = Vec::new();
        if let Some(map) = input.get("files") {
            let map = map.as_object().ok_or("input files is not an object")?;
            for (relative, text) in map {
                let text = text
                    .as_str()
                    .ok_or_else(|| format!("input file {relative} is not text"))?;
                files.push((safe_relative(relative)?, text.as_bytes().to_vec()));
            }
        }
        if let Some(map) = input.get("bytes") {
            let map = map.as_object().ok_or("input bytes is not an object")?;
            for (relative, hex) in map {
                let hex = hex
                    .as_str()
                    .ok_or_else(|| format!("input bytes {relative} is not hex text"))?;
                files.push((safe_relative(relative)?, decode_hex(hex)?));
            }
        }
        for (relative, bytes) in &files {
            let path = folder.path().join(relative);
            if path.exists() {
                return Err(format!("input names {} twice", relative.display()));
            }
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|error| format!("cannot create input folders: {error}"))?;
            }
            std::fs::write(&path, bytes)
                .map_err(|error| format!("cannot write an input file: {error}"))?;
        }
        Ok(Materialized { folder, files })
    }

    fn path(&self, relative: &str) -> PathBuf {
        self.folder
            .path()
            .join(safe_relative(relative).expect("a fixed relative path"))
    }

    /// Whether every input file still holds the bytes it was written with.
    fn unchanged(&self) -> bool {
        self.files.iter().all(|(relative, bytes)| {
            std::fs::read(self.folder.path().join(relative))
                .ok()
                .as_ref()
                == Some(bytes)
        })
    }
}

/// A fixture path: relative, `/`-separated, with plain parts only.
fn safe_relative(relative: &str) -> Result<PathBuf, String> {
    let mut out = PathBuf::new();
    for part in relative.split('/') {
        let ok = !part.is_empty()
            && part != "."
            && part != ".."
            && !part.contains('\\')
            && !part.contains(':')
            && matches!(
                Path::new(part).components().next(),
                Some(Component::Normal(_))
            );
        if !ok {
            return Err(format!(
                "the input path {relative:?} is not a plain relative path"
            ));
        }
        out.push(part);
    }
    if out.as_os_str().is_empty() {
        return Err("an input path is empty".into());
    }
    Ok(out)
}

fn decode_hex(hex: &str) -> Result<Vec<u8>, String> {
    if !hex.len().is_multiple_of(2)
        || !hex
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("input bytes must be lowercase hex of even length".into());
    }
    (0..hex.len())
        .step_by(2)
        .map(|index| {
            u8::from_str_radix(&hex[index..index + 2], 16).map_err(|error| error.to_string())
        })
        .collect()
}

/// A fixture JSON value as a configuration value. `null` has no configuration form.
pub fn json_to_value(json: &Json) -> Result<Value, String> {
    Ok(match json {
        Json::Null => return Err("null is not a configuration value".into()),
        Json::Bool(flag) => Value::Boolean(*flag),
        Json::Number(number) => match number.as_i64() {
            Some(integer) => Value::Integer(integer),
            None if number.is_f64() => Value::Float(number.as_f64().expect("f64")),
            None => return Err(format!("the number {number} does not fit a TOML integer")),
        },
        Json::String(text) => Value::String(text.clone()),
        Json::Array(items) => {
            Value::Array(items.iter().map(json_to_value).collect::<Result<_, _>>()?)
        }
        Json::Object(map) => Value::Table(json_to_table(map)?),
    })
}

fn json_to_table(map: &Map<String, Json>) -> Result<Table, String> {
    let mut table = Table::new();
    for (key, value) in map {
        table.insert(key.clone(), json_to_value(value)?);
    }
    Ok(table)
}

/// A configuration value as JSON, for comparison with `expect.json`.
pub fn value_to_json(value: &Value) -> Json {
    match value {
        Value::String(text) => Json::String(text.clone()),
        Value::Integer(number) => Json::from(*number),
        Value::Float(number) => serde_json::Number::from_f64(*number)
            .map(Json::Number)
            .unwrap_or_else(|| Json::String(value.to_toml())),
        Value::Boolean(flag) => Json::Bool(*flag),
        Value::Datetime(datetime) => Json::String(datetime.text().to_string()),
        Value::Array(items) => Json::Array(items.iter().map(value_to_json).collect()),
        Value::Table(table) => table_to_json(table),
    }
}

fn table_to_json(table: &Table) -> Json {
    Json::Object(
        table
            .iter()
            .map(|(key, value)| (key.to_string(), value_to_json(value)))
            .collect(),
    )
}

const CONFIG_FILE: &str = ".oah/config.toml";

/// The normalization the config manifest's notes allow: the file is named `.oah/config.toml`, not by the
/// temporary path.
fn normalize_message(message: &str, path: &Path) -> String {
    message.replace(&path.display().to_string(), CONFIG_FILE)
}

fn run_config_case(
    case: &Case,
    deviation: bool,
    input: &Map<String, Json>,
    expect: &Json,
) -> Result<(), String> {
    match case.function.as_str() {
        "oah.config.Config.section" => run_section(case, deviation, input, expect),
        "design:config-legacy-read" => {
            if deviation {
                return Err("a design case cannot be a deviation".into());
            }
            run_legacy_read(input, expect)
        }
        other => Err(format!(
            "the enabled area has no operation for function {other:?}"
        )),
    }
}

fn run_section(
    case: &Case,
    deviation: bool,
    input: &Map<String, Json>,
    expect: &Json,
) -> Result<(), String> {
    for key in input.keys() {
        if !["section", "defaults", "files", "bytes"].contains(&key.as_str()) {
            return Err(format!(
                "input.json has the unknown key {key:?} for a section read"
            ));
        }
    }
    let section = input
        .get("section")
        .and_then(Json::as_str)
        .ok_or("input.json has no section name")?;
    let defaults = input
        .get("defaults")
        .and_then(Json::as_object)
        .ok_or("input.json has no defaults table")?;
    let defaults = json_to_table(defaults)?;
    let files = Materialized::new(input)?;
    let path = files.path(CONFIG_FILE);
    let result = ConfigFile::read(&path).and_then(|file| file.section(section, &defaults));
    if !files.unchanged() {
        return Err("reading changed an input file".into());
    }
    if deviation {
        return check_unknown_key_deviation(case, expect, &defaults, &path, result);
    }
    match (expect.get("ok"), expect.get("error"), result) {
        (Some(wanted), None, Ok(merged)) => {
            let got = table_to_json(&merged.table);
            if &got != wanted {
                return Err(format!("expected {wanted}, got {got}"));
            }
            if let Some(notice) = merged.notices.first() {
                return Err(format!("an unchanged case reported the notice {notice}"));
            }
            Ok(())
        }
        (None, Some(wanted), Err(error)) => compare_error(
            wanted,
            error.code(),
            &normalize_message(error.message(), &path),
        ),
        (Some(wanted), None, Err(error)) => {
            Err(format!("expected {wanted}, got the error {error}"))
        }
        (None, Some(wanted), Ok(merged)) => Err(format!(
            "expected the error {wanted}, got {}",
            table_to_json(&merged.table)
        )),
        _ => Err("expect.json must hold exactly ok or error".into()),
    }
}

fn compare_error(wanted: &Json, code: &str, message: &str) -> Result<(), String> {
    let wanted_code = wanted
        .get("code")
        .and_then(Json::as_str)
        .unwrap_or_default();
    if wanted_code != code {
        return Err(format!(
            "expected error code {wanted_code}, got {code}: {message}"
        ));
    }
    if let Some(wanted_message) = wanted.get("message").and_then(Json::as_str)
        && wanted_message != message
    {
        return Err(format!(
            "expected message {wanted_message:?}, got {message:?}"
        ));
    }
    Ok(())
}

fn check_unknown_key_deviation(
    case: &Case,
    expect: &Json,
    defaults: &Table,
    path: &Path,
    result: Result<oah_core::config::Merged, oah_core::config::ConfigError>,
) -> Result<(), String> {
    if !CONFIG_DEVIATIONS.contains(&case.name.as_str()) {
        return Err("no adaptation is registered for this deviation".into());
    }
    let frozen = expect
        .get("error")
        .ok_or("the frozen v0.4.0 expectation is not a refusal")?;
    if frozen.get("code").and_then(Json::as_str) != Some(notice_codes::UNKNOWN_KEY) {
        return Err("the frozen v0.4.0 refusal is not an unknown-key refusal".into());
    }
    let frozen_message = frozen
        .get("message")
        .and_then(Json::as_str)
        .ok_or("the frozen v0.4.0 refusal has no message")?;
    let merged = result.map_err(|error| format!("1.0 must keep reading, but refused: {error}"))?;
    let got = table_to_json(&merged.table);
    let wanted = table_to_json(defaults);
    if got != wanted {
        return Err(format!("1.0 must keep the defaults {wanted}, got {got}"));
    }
    match merged.notices.as_slice() {
        [notice] if notice.code() == notice_codes::UNKNOWN_KEY => {
            let message = normalize_message(notice.message(), path);
            if message != frozen_message {
                return Err(format!(
                    "the notice says {message:?}, not {frozen_message:?}"
                ));
            }
            Ok(())
        }
        other => Err(format!(
            "1.0 must report exactly one unknown-key notice, got {:?}",
            other.iter().map(ToString::to_string).collect::<Vec<_>>()
        )),
    }
}

const PROJECTION_KEYS: [&str; 5] = [
    "stages",
    "agents",
    "unused_kept_in_file",
    "notices",
    "file_unchanged",
];

fn run_legacy_read(input: &Map<String, Json>, expect: &Json) -> Result<(), String> {
    for key in input.keys() {
        if !["read", "files", "bytes"].contains(&key.as_str()) {
            return Err(format!(
                "input.json has the unknown key {key:?} for a legacy read"
            ));
        }
    }
    if input.get("read").and_then(Json::as_str) != Some("legacy-to-1.0") {
        return Err("input.json does not ask for the legacy-to-1.0 read".into());
    }
    let files = Materialized::new(input)?;
    let path = files.path(CONFIG_FILE);
    let result = ProjectConfig::read(&path);
    let unchanged = files.unchanged();
    match (expect.get("ok"), expect.get("error"), result) {
        (Some(wanted), None, Ok(config)) => {
            let wanted = wanted
                .as_object()
                .ok_or("expect.json ok is not an object")?;
            if let Some(key) = wanted
                .keys()
                .find(|key| !PROJECTION_KEYS.contains(&key.as_str()))
            {
                return Err(format!(
                    "expect.json names {key:?}, which a legacy read does not produce"
                ));
            }
            let got = projection(&config, unchanged);
            for (key, value) in wanted {
                let mine = got.get(key).cloned().unwrap_or(Json::Null);
                if &mine != value {
                    return Err(format!("{key}: expected {value}, got {mine}"));
                }
            }
            Ok(())
        }
        (None, Some(wanted), Err(error)) => compare_error(
            wanted,
            error.code(),
            &normalize_message(error.message(), &path),
        ),
        (Some(wanted), None, Err(error)) => {
            Err(format!("expected {wanted}, got the error {error}"))
        }
        (None, Some(wanted), Ok(_)) => Err(format!("expected the error {wanted}, got a reading")),
        _ => Err("expect.json must hold exactly ok or error".into()),
    }
}

/// The effective reading in the shape of the design cases: assignments by section, the agent keys the file
/// gives that 1.0 uses, the keys kept unused (sorted, without the agent prefix), the notice codes, and whether
/// the file kept its bytes.
fn projection(config: &ProjectConfig, unchanged: bool) -> Map<String, Json> {
    let effective = config.effective();
    let mut out = Map::new();
    out.insert(
        "stages".into(),
        Json::Object(
            effective
                .stages
                .iter()
                .map(|assignment| {
                    (
                        assignment.section.clone(),
                        Json::String(assignment.agent.as_str().into()),
                    )
                })
                .collect(),
        ),
    );
    let mut agents = Map::new();
    let mut unused = BTreeSet::new();
    for agent in &effective.agents {
        if agent.source == oah_core::config::AgentSource::Defaults {
            continue;
        }
        agents.insert(agent.id.as_str().into(), table_to_json(&agent.given));
        unused.extend(agent.unused_kept_in_file.iter().cloned());
    }
    out.insert("agents".into(), Json::Object(agents));
    out.insert(
        "unused_kept_in_file".into(),
        Json::Array(unused.into_iter().map(Json::String).collect()),
    );
    out.insert(
        "notices".into(),
        Json::Array(
            config
                .notices()
                .iter()
                .map(|notice| Json::String(notice.code().into()))
                .collect(),
        ),
    );
    out.insert("file_unchanged".into(), Json::Bool(unchanged));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixture_paths_must_stay_inside_the_folder() {
        assert!(safe_relative(".oah/config.toml").is_ok());
        for bad in ["../x", "/etc/x", "a//b", "a/./b", "C:/x", "a\\b", ""] {
            assert!(safe_relative(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn hex_must_be_lowercase_and_even() {
        assert_eq!(decode_hex("c4da").unwrap(), vec![0xc4, 0xda]);
        assert!(decode_hex("C4").is_err());
        assert!(decode_hex("abc").is_err());
    }

    #[test]
    fn json_numbers_keep_integer_and_float_apart() {
        let integer: Json = serde_json::from_str("2").unwrap();
        let float: Json = serde_json::from_str("2.0").unwrap();
        assert_eq!(json_to_value(&integer).unwrap(), Value::Integer(2));
        assert_eq!(json_to_value(&float).unwrap(), Value::Float(2.0));
        assert_ne!(value_to_json(&Value::Float(2.0)), integer);
        assert!(json_to_value(&Json::Null).is_err());
    }
}
