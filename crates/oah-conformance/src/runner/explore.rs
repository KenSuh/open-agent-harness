// 탐색 핵심 적합성 어댑터: review·license·hashes 고정 사례를 실제 oah-core 검토·라이선스·해시 API 로 다시 계산해 기대 JSON 과 대조한다.
//! Adapters for the three Explore areas, `review`, `license` and `hashes`, mapped from the direct review core
//! harness (`oah-core/tests/review_core/conformance.rs`) onto the public `oah-core` API.
//!
//! Each case's declared function picks one production operation. A table case (`calls`) runs every row and
//! collects `{row: result}`; a single case gives one result. Either way the whole recomputed `{"ok": …}` or
//! `{"error": {code, message}}` is compared with `expect.json` as parsed JSON, so a changed expected value fails
//! the case. Skill text and metadata are only data here: nothing is executed, fetched or installed.

use super::Case;
use oah_core::Coded;
use oah_core::review::ReviewError;
use serde_json::{Map, Value as Json, json};

mod hashes;
mod license;
mod record;
mod review;

/// `serde_json::to_value` of a core result, as an adapter error when it cannot be serialized.
macro_rules! json_of {
    ($value:expr) => {
        serde_json::to_value($value)
            .map_err(|error| format!("cannot serialize a core result: {error}"))?
    };
}
pub(crate) use json_of;

pub(super) fn run_case(
    area: &str,
    case: &Case,
    deviation: bool,
    input: &Map<String, Json>,
    expect: &Json,
) -> Result<(), String> {
    if deviation {
        return Err(format!("the {area} area has no deviation adapters"));
    }
    let got = match area {
        "review" => review::run(case, input)?,
        "license" => license::run(case, input)?,
        "hashes" => hashes::run(case, input)?,
        other => return Err(format!("no Explore operations are registered for {other}")),
    };
    match difference(&got, expect, "$") {
        None => Ok(()),
        Some(found) => Err(found),
    }
}

fn ok(value: Json) -> Json {
    json!({ "ok": value })
}

/// A refusal as the fixtures record it: the code and the whole v0.4.0 exception text.
fn refused(error: &ReviewError) -> Json {
    json!({ "error": { "code": error.code(), "message": error.to_string() } })
}

/// Refuses keys an operation does not read, so a fixture cannot carry input the adapter silently ignores.
fn only_keys(input: &Map<String, Json>, allowed: &[&str], what: &str) -> Result<(), String> {
    match input.keys().find(|key| !allowed.contains(&key.as_str())) {
        Some(key) => Err(format!("{what} input has the unknown key {key:?}")),
        None => Ok(()),
    }
}

fn text<'a>(input: &'a Map<String, Json>, key: &str) -> Result<&'a str, String> {
    input
        .get(key)
        .and_then(Json::as_str)
        .ok_or_else(|| format!("input has no string {key}"))
}

fn texts(value: Option<&Json>, key: &str) -> Result<Vec<String>, String> {
    let items = value
        .and_then(Json::as_array)
        .ok_or_else(|| format!("input {key} is not a list"))?;
    items
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_string)
                .ok_or_else(|| format!("input {key} holds a value that is not a string"))
        })
        .collect()
}

/// A table case: every row of `calls` through `run`, as `{"ok": {row: result}}`.
fn table(
    input: &Map<String, Json>,
    row_keys: &[&str],
    run: impl Fn(&Map<String, Json>) -> Result<Json, String>,
) -> Result<Json, String> {
    only_keys(input, &["calls"], "a table case")?;
    let calls = input
        .get("calls")
        .and_then(Json::as_object)
        .ok_or("a table case has no calls object")?;
    let mut results = Map::new();
    for (row, args) in calls {
        let args = args
            .as_object()
            .ok_or_else(|| format!("row {row} is not an object"))?;
        only_keys(args, row_keys, &format!("row {row}"))?;
        results.insert(
            row.clone(),
            run(args).map_err(|error| format!("row {row}: {error}"))?,
        );
    }
    Ok(ok(Json::Object(results)))
}

/// The first place two JSON values differ, for a readable failure.
fn difference(got: &Json, want: &Json, at: &str) -> Option<String> {
    match (got, want) {
        (Json::Object(left), Json::Object(right)) => {
            let mut keys: Vec<&String> = left.keys().chain(right.keys()).collect();
            keys.sort();
            keys.dedup();
            keys.into_iter()
                .find_map(|key| match (left.get(key), right.get(key)) {
                    (Some(left), Some(right)) => difference(left, right, &format!("{at}.{key}")),
                    (left, right) => Some(format!("{at}.{key}: got {left:?}, want {right:?}")),
                })
        }
        (Json::Array(left), Json::Array(right)) if left.len() == right.len() => left
            .iter()
            .zip(right)
            .enumerate()
            .find_map(|(index, (left, right))| difference(left, right, &format!("{at}[{index}]"))),
        _ if got == want => None,
        _ => Some(format!("{at}: got {got}, want {want}")),
    }
}
