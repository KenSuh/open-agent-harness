// 고정 픽스처 시험: conformance/v0.4.0/rules 의 43건을 그대로 읽어 실제 plan·find_block·render_block 결과와 대조한다.
use crate::rules::{BlockBodies, Eol, find_block, render_block, split_lines};
use crate::support::{plan_dir, write_files};
use serde_json::Value as Json;
use std::fs;
use std::path::PathBuf;

const PROJECT_FILES: [&str; 3] = ["AGENTS.md", "CLAUDE.md", ".oah/rules.local.md"];

fn area() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../conformance/v0.4.0/rules")
}

fn read_json(path: PathBuf) -> Json {
    serde_json::from_slice(&fs::read(&path).expect("a fixture file")).expect("fixture JSON")
}

fn hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&text[at..at + 2], 16).expect("lowercase hex"))
        .collect()
}

/// The case's project files: `files` as UTF-8 text and `bytes` as hex, only the three files `plan` reads.
fn case_files(input: &Json) -> Vec<(String, Vec<u8>)> {
    let mut out = Vec::new();
    for (key, decode) in [("files", false), ("bytes", true)] {
        for (path, value) in input[key].as_object().into_iter().flatten() {
            assert!(
                PROJECT_FILES.contains(&path.as_str()),
                "unexpected input file {path}"
            );
            let text = value.as_str().expect("a string");
            out.push((
                path.clone(),
                if decode {
                    hex(text)
                } else {
                    text.as_bytes().to_vec()
                },
            ));
        }
    }
    out
}

/// Runs one case; returns a failure description or `None`.
fn run_case(name: &str) -> Option<String> {
    let input = read_json(area().join(name).join("input.json"));
    let expect = read_json(area().join(name).join("expect.json"));
    let bodies = BlockBodies {
        en: input["block_bodies"]["en"]
            .as_str()
            .expect("en body")
            .to_string(),
        ko: input["block_bodies"]["ko"]
            .as_str()
            .expect("ko body")
            .to_string(),
    };
    let dir = tempfile::tempdir().expect("a temporary folder");
    let files = case_files(&input);
    let borrowed: Vec<(&str, &[u8])> = files
        .iter()
        .map(|(p, b)| (p.as_str(), b.as_slice()))
        .collect();
    write_files(dir.path(), &borrowed);
    let local = files
        .iter()
        .find(|(p, _)| p == ".oah/rules.local.md")
        .map(|(_, b)| b.clone());
    let result = plan_dir(dir.path(), input["lang"].as_str(), &bodies);
    match (result, expect.get("ok"), expect.get("error")) {
        (Ok(plan), Some(ok), None) => {
            let got = (
                plan.lang.as_str(),
                plan.agents_after.as_slice(),
                plan.claude_after.as_slice(),
                plan.block_bytes as u64,
            );
            let want = (
                ok["lang"].as_str().unwrap_or_default(),
                ok["agents_after"].as_str().unwrap_or_default().as_bytes(),
                ok["claude_after"].as_str().unwrap_or_default().as_bytes(),
                ok["block_bytes"].as_u64().unwrap_or(u64::MAX),
            );
            if got != want {
                return Some(format!("{name}: got {got:?}, want {want:?}"));
            }
            check_block_functions(name, &plan.agents_after, plan.lang.as_str(), &bodies, local)
        }
        (Err(error), None, Some(want)) if Some(error.code()) == want["code"].as_str() => None,
        (got, _, _) => Some(format!(
            "{name}: got {:?}, expected {expect}",
            got.map(|p| p.lang)
        )),
    }
}

/// Calls the other two manifest functions on an ok case: `find_block` locates the block in the planned file and
/// `render_block` reproduces exactly the bytes between its markers, with the marker line's ending.
fn check_block_functions(
    name: &str,
    after: &[u8],
    lang: &str,
    bodies: &BlockBodies,
    local: Option<Vec<u8>>,
) -> Option<String> {
    let text = std::str::from_utf8(after).expect("planned bytes are UTF-8");
    let lines = split_lines(text);
    let Ok(Some((begin, end))) = find_block(&lines) else {
        return Some(format!(
            "{name}: find_block found no block in the planned file"
        ));
    };
    let eol = if lines[begin].ends_with("\r\n") {
        Eol::Crlf
    } else {
        Eol::Lf
    };
    let local =
        String::from_utf8(local.unwrap_or_default()).expect("local rules are UTF-8 in ok cases");
    let block =
        render_block(lang, bodies, &local, eol).expect("render_block succeeds on an ok case");
    let span: String = lines[begin..=end].concat();
    let span = span.strip_suffix(eol.as_str()).unwrap_or(&span);
    (span != block).then(|| format!("{name}: render_block differs from the planned block"))
}

#[test]
fn every_frozen_rules_case_matches_the_real_planner() {
    let manifest = read_json(area().join("MANIFEST.json"));
    assert_eq!(
        manifest["status"], "pending",
        "the frozen manifest is never switched on here"
    );
    let names: Vec<String> = manifest["functions"]
        .as_array()
        .expect("functions")
        .iter()
        .map(|f| f["name"].as_str().expect("a name").to_string())
        .collect();
    assert_eq!(names, ["plan", "render_block", "find_block"]);
    let cases = manifest["cases"].as_array().expect("cases");
    let mut failures = Vec::new();
    for case in cases {
        assert_eq!(case["function"], "oah.rules.plan");
        failures.extend(run_case(case["name"].as_str().expect("a case name")));
    }
    let total = cases.len();
    println!(
        "RULES_FIXTURES pass={} fail={} total={total}",
        total - failures.len(),
        failures.len()
    );
    assert_eq!(total, 43, "the frozen area has 43 cases");
    assert!(
        failures.is_empty(),
        "failing cases:\n{}",
        failures.join("\n")
    );
}
