// 고정 v0.4.0 사례 대조: review·license 영역의 모든 사례를 옮긴 모듈로 다시 계산해 기대 결과와 JSON 으로 비교한다.
//! Every frozen v0.4.0 case of the `review` and `license` areas, recomputed by the ported modules and compared
//! with the recorded result as parsed JSON.

use crate::fixtures::{
    Case, case, difference, listed, ok, refused, review_case, rows, string_at, strings_at,
};
use crate::review::frontmatter::{frontmatter, parse_frontmatter};
use crate::review::identity::{hit_paths, identity};
use crate::review::license::{
    classify_text, declared_from_manifest, eligibility, parse_expression, verdict,
};
use crate::review::screening::{references, screening};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};

const REVIEW_TABLES: [&str; 6] = [
    "frontmatter",
    "parse-frontmatter",
    "screening",
    "references",
    "identity",
    "hit-paths",
];
const IDENTITY_REFUSALS: [&str; 4] = [
    "identity-not-github",
    "identity-no-owner",
    "identity-glob-path",
    "identity-parent-path",
];
const RECORDS: [&str; 11] = [
    "record-mit-reviewed",
    "record-screened-claude-only",
    "record-no-license",
    "record-apache-excluded",
    "record-apache-allowed",
    "record-name-mismatch",
    "record-malformed-frontmatter",
    "record-size-at-limit",
    "record-size-over-limit",
    "record-path-missing",
    "record-no-skill-file",
];
const LICENSE_CASES: [&str; 5] = [
    "classify",
    "classify-mit-utf16",
    "expression",
    "eligibility",
    "declared",
];

fn json_of(value: impl serde::Serialize) -> Value {
    serde_json::to_value(value).expect("a serializable value")
}

/// Panics with every case that differs, and where.
fn check(failures: Vec<String>) {
    assert!(
        failures.is_empty(),
        "{} case(s) differ from v0.4.0:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

/// One table case: each row through `run`, compared with the frozen results.
fn table(area: &str, name: &str, run: impl Fn(&Value) -> Value, failures: &mut Vec<String>) {
    let case: Case = case(area, name);
    let results: Map<String, Value> = rows(&case)
        .iter()
        .map(|(row, args)| (row.clone(), run(args)))
        .collect();
    if let Some(found) = difference(&ok(Value::Object(results)), &case.expect, "$") {
        failures.push(format!("{area}/{name}: {found}"));
    }
}

#[test]
fn every_frozen_case_has_a_check() {
    let review: BTreeSet<String> = REVIEW_TABLES
        .iter()
        .chain(&IDENTITY_REFUSALS)
        .chain(&RECORDS)
        .map(|name| name.to_string())
        .collect();
    assert_eq!(
        listed("review").into_iter().collect::<BTreeSet<_>>(),
        review
    );
    let license: BTreeSet<String> = LICENSE_CASES.iter().map(|name| name.to_string()).collect();
    assert_eq!(
        listed("license").into_iter().collect::<BTreeSet<_>>(),
        license
    );
}

#[test]
fn review_tables_match_v040() {
    let mut failures = Vec::new();
    table(
        "review",
        "frontmatter",
        |args| json_of(frontmatter(string_at(args, "text"))),
        &mut failures,
    );
    table(
        "review",
        "parse-frontmatter",
        |args| json_of(parse_frontmatter(string_at(args, "text"))),
        &mut failures,
    );
    table(
        "review",
        "screening",
        |args| json_of(screening(string_at(args, "text"))),
        &mut failures,
    );
    let refs = |args: &Value| {
        let present: BTreeSet<String> = strings_at(&args["present"]).into_iter().collect();
        json_of(references(
            string_at(args, "text"),
            string_at(args, "folder"),
            &present,
        ))
    };
    table("review", "references", refs, &mut failures);
    let ids = |args: &Value| match identity(string_at(args, "repo"), string_at(args, "path")) {
        Ok((found, _)) => json!([found.source_id, found.full, found.fields]),
        Err(error) => panic!("identity refused a kept row: {error}"),
    };
    table("review", "identity", ids, &mut failures);
    table(
        "review",
        "hit-paths",
        |args| json!(hit_paths(string_at(args, "name"))),
        &mut failures,
    );
    check(failures);
}

#[test]
fn identity_refusals_match_v040() {
    let mut failures = Vec::new();
    for name in IDENTITY_REFUSALS {
        let case = case("review", name);
        let got = match identity(
            string_at(&case.input, "repo"),
            string_at(&case.input, "path"),
        ) {
            Ok((found, _)) => ok(json!([found.source_id, found.full, found.fields])),
            Err(error) => refused(&error),
        };
        if let Some(found) = difference(&got, &case.expect, "$") {
            failures.push(format!("review/{name}: {found}"));
        }
    }
    check(failures);
}

#[test]
fn records_match_v040() {
    let mut failures = Vec::new();
    for name in RECORDS {
        let case = case("review", name);
        let got = match review_case(&case.input) {
            Ok(record) => ok(record.to_json()),
            Err(error) => refused(&error),
        };
        if let Some(found) = difference(&got, &case.expect, "$") {
            failures.push(format!("review/{name}: {found}"));
        }
    }
    check(failures);
}

fn from_hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&text[at..at + 2], 16).expect("hex"))
        .collect()
}

#[test]
fn license_cases_match_v040() {
    let mut failures = Vec::new();
    table(
        "license",
        "classify",
        |args| json_of(classify_text(string_at(args, "text").as_bytes())),
        &mut failures,
    );
    let utf16 = case("license", "classify-mit-utf16");
    let got = ok(json_of(classify_text(&from_hex(string_at(
        &utf16.input["bytes"],
        "LICENSE",
    )))));
    if let Some(found) = difference(&got, &utf16.expect, "$") {
        failures.push(format!("license/classify-mit-utf16: {found}"));
    }
    let expression = |args: &Value| match parse_expression(string_at(args, "text")) {
        Some((form, ids)) => json!([form.as_str(), ids]),
        None => Value::Null,
    };
    table("license", "expression", expression, &mut failures);
    let eligible = |args: &Value| {
        let blobs: BTreeMap<String, Vec<u8>> = args["blobs"]
            .as_object()
            .map(|blobs| {
                blobs
                    .iter()
                    .map(|(sha, text)| {
                        (
                            sha.clone(),
                            text.as_str().expect("text").as_bytes().to_vec(),
                        )
                    })
                    .collect()
            })
            .unwrap_or_default();
        let read = |sha256: &str| blobs.get(sha256).cloned();
        let found = verdict(args.get("evidence"), &read);
        let answers: Vec<Value> = args["allow"]
            .as_array()
            .expect("allow lists")
            .iter()
            .map(|allow| json_of(eligibility(&found, &strings_at(allow))))
            .collect();
        json!({
            "verdict": {"expression": found.expression, "form": found.form, "ids": found.ids,
                        "problems": found.problems, "strict": found.strict},
            "eligibility": answers,
        })
    };
    table("license", "eligibility", eligible, &mut failures);
    let declared = |args: &Value| {
        let (found, problem) =
            declared_from_manifest(string_at(args, "path"), string_at(args, "text").as_bytes());
        json!({"declared": found, "problem": problem})
    };
    table("license", "declared", declared, &mut failures);
    check(failures);
}
