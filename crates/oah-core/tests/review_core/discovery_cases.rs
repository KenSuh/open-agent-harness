// 라이선스 탐색 차등 사례: 고정 사례가 빼 둔 discover 경로(NOTICE·중첩·LICENSES·1 MiB·심볼릭 링크 등)를 v0.4.0 이 같은 픽스처에 준 기록 ID·적격 사유와 비교한다.
//! License discovery beyond the frozen cases (the frozen license area omits `discover`), against what the frozen
//! v0.4.0 Python review answered for the same fixture repositories, recorded in `discovery.json`. The record id covers
//! the whole record, snapshot manifest and license evidence included; the eligibility reason covers the verdict's
//! problem texts, which the record does not keep.

use crate::fixtures::{
    add_entry, case, commit, commit_fields, git, materialize, read_json, request, review_at, stage,
    string_at, strings_at,
};
use crate::registry::local::LocalGit;
use crate::review::identity::identity;
use crate::review::license::{eligibility, verdict};
use crate::review::snapshot::{Collector, Observation};
use serde_json::{Map, Value, json};
use std::path::Path;

const SKILL: &str =
    "---\nname: tidy\ndescription: tidy guidance for testing\n---\n\nKeep notes short.\n";
const ISC_BLOB: &str = "25988a7c9bb2d1bbb44f1abbdb08c536aeffa0b5d825caffc9595cc1c3546b94";

/// The named texts a fixture value may use: the frozen fixtures' MIT and ISC texts, MIT with CRLF, a SKILL.md.
fn named() -> Map<String, Value> {
    let mit = string_at(
        &case("review", "record-mit-reviewed").input["upstream"]["files"],
        "LICENSE",
    )
    .to_string();
    let isc = string_at(
        &case("license", "eligibility").input["calls"]["choice"]["blobs"],
        ISC_BLOB,
    )
    .to_string();
    let crlf = mit.replace('\n', "\r\n");
    [
        ("@MIT", mit),
        ("@ISC", isc),
        ("@MIT-CRLF", crlf),
        ("@SKILL", SKILL.to_string()),
    ]
    .into_iter()
    .map(|(name, text)| (name.to_string(), json!(text)))
    .collect()
}

fn resolve(value: &Value, named: &Map<String, Value>) -> Value {
    value
        .as_str()
        .and_then(|text| named.get(text))
        .cloned()
        .unwrap_or_else(|| value.clone())
}

/// The license eligibility reason of a fresh collection of `repository`.
fn reason_of(repository: &Path, repo: &str, path: &str, allow: &[String]) -> String {
    let (_, spec) = identity(repo, path).expect("identity");
    let Observation::Snapshot(snapshot) =
        LocalGit::new(&git().1, repository, &spec.locator, &git().0).observe(&spec)
    else {
        panic!("a reviewed folder has a snapshot");
    };
    let read = |sha256: &str| snapshot.blob(sha256).map(<[u8]>::to_vec);
    eligibility(&verdict(snapshot.manifest.get("license"), &read), allow).reason
}

#[test]
fn discovery_matches_v040() {
    let spec =
        read_json(&Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/review_core/discovery.json"));
    let named = named();
    let (repo, path) = (string_at(&spec, "repo"), string_at(&spec, "path"));
    let cases = spec["cases"].as_object().expect("cases");
    assert!(cases.len() >= 16, "the discovery fixtures are all there");
    let mut failures = Vec::new();
    for (name, case) in cases {
        let scratch = tempfile::tempdir().expect("scratch");
        let repository = scratch.path().join("upstream");
        let files = case["files"].as_object().expect("files");
        let files: Map<String, Value> = files
            .iter()
            .map(|(at, value)| (at.clone(), resolve(value, &named)))
            .collect();
        stage(&repository, &commit_fields(), &files);
        for entry in case["entries"].as_array().expect("entries") {
            let data = materialize(&resolve(&entry[2], &named));
            add_entry(
                &repository,
                scratch.path(),
                entry[0].as_str().expect("mode"),
                entry[1].as_str().expect("path"),
                &data,
            );
        }
        commit(&repository, &commit_fields());
        let allow = strings_at(&case["allow"]);
        let mut input = request(repo, path);
        input["allow"] = case["allow"].clone();
        let (result, reason) = match review_at(&repository, &input) {
            Ok(record) => (
                record.id.clone(),
                Some(reason_of(&repository, repo, path, &allow)),
            ),
            Err(error) => (error.to_string(), None),
        };
        let want = &case["v040"];
        if result != string_at(want, "result") || reason.as_deref() != want["reason"].as_str() {
            failures.push(format!("{name}: got ({result}, {reason:?}), want {want}"));
        }
    }
    assert!(
        failures.is_empty(),
        "{} discovery case(s) differ from v0.4.0:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
