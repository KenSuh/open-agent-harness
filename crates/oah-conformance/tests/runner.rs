// 실행기 음성 대조: 고정 트리의 임시 사본을 하나씩 망가뜨려 실행기가 그 영역·사례를 실패로 보고하는지, 원본 conformance/ 는 그대로인지 확인한다.
use oah_conformance::runner::{AreaReport, AreaState, run_all, run_area};
use serde_json::Value as Json;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

fn fixture_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("conformance")
        .join("v0.4.0")
}

/// Every file below `root` with its bytes, keyed by the path relative to `root`.
fn snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut out = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        for entry in std::fs::read_dir(&folder).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                pending.push(path);
            } else {
                let bytes = std::fs::read(&path).unwrap();
                out.insert(path.strip_prefix(root).unwrap().to_path_buf(), bytes);
            }
        }
    }
    out
}

/// A scratch copy of the fixture tree; the original is never touched.
struct Scratch {
    _dir: tempfile::TempDir,
    root: PathBuf,
}

impl Scratch {
    fn new() -> Scratch {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("v0.4.0");
        for (relative, bytes) in snapshot(&fixture_root()) {
            let path = root.join(relative);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, bytes).unwrap();
        }
        Scratch { _dir: dir, root }
    }

    fn path(&self, relative: &str) -> PathBuf {
        self.root.join(relative)
    }

    fn edit_json(&self, relative: &str, change: impl FnOnce(&mut Json)) {
        let path = self.path(relative);
        let mut json: Json =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        change(&mut json);
        std::fs::write(&path, serde_json::to_string_pretty(&json).unwrap()).unwrap();
    }

    fn config(&self) -> AreaReport {
        run_area(&self.root, "config")
    }
}

/// The failing case names, sorted.
fn failed_cases(report: &AreaReport) -> Vec<&str> {
    let mut cases: Vec<&str> = report
        .failures
        .iter()
        .map(|failure| failure.case.as_str())
        .collect();
    cases.sort_unstable();
    cases
}

#[test]
fn an_unmodified_scratch_copy_passes() {
    let scratch = Scratch::new();
    let report = scratch.config();
    assert!(report.is_ok(), "{:#?}", report.failures);
    assert_eq!(report.passed, 30);
}

#[test]
fn one_wrong_ok_expectation_fails_that_case_only() {
    let scratch = Scratch::new();
    scratch.edit_json("config/bom-stages/expect.json", |json| {
        let ok = json["ok"].as_object_mut().unwrap();
        let first = ok.keys().next().unwrap().clone();
        ok.insert(first, Json::String("someone-else".into()));
    });
    let report = scratch.config();
    assert_eq!(failed_cases(&report), ["bom-stages"]);
    assert_eq!(report.passed, 29);
    assert!(
        report.summary_line().contains("failed=1"),
        "{}",
        report.summary_line()
    );
}

#[test]
fn a_wrong_error_message_or_code_fails() {
    let scratch = Scratch::new();
    scratch.edit_json("config/type-section-not-table/expect.json", |json| {
        json["error"]["message"] = Json::String(".oah/config.toml: something else".into());
    });
    scratch.edit_json("config/invalid-toml/expect.json", |json| {
        json["error"]["code"] = Json::String("OAH_CONFIG_UNKNOWN_KEY".into());
    });
    let report = scratch.config();
    assert_eq!(
        failed_cases(&report),
        ["invalid-toml", "type-section-not-table"]
    );
}

#[test]
fn a_wrong_design_projection_fails() {
    let scratch = Scratch::new();
    scratch.edit_json("config/legacy-new-keys-win/expect.json", |json| {
        json["ok"]["notices"] = Json::Array(Vec::new());
    });
    scratch.edit_json("config/legacy-only-legacy-keys/expect.json", |json| {
        json["ok"]["file_unchanged"] = Json::Bool(false);
    });
    scratch.edit_json("config/legacy-agents-win/expect.json", |json| {
        json["ok"]["surprise"] = Json::Bool(true);
    });
    let report = scratch.config();
    assert_eq!(
        failed_cases(&report),
        [
            "legacy-agents-win",
            "legacy-new-keys-win",
            "legacy-only-legacy-keys"
        ]
    );
}

#[test]
fn a_deviation_must_carry_the_frozen_refusal_message() {
    let scratch = Scratch::new();
    scratch.edit_json("config/unknown-key-stages/expect.json", |json| {
        json["error"]["message"] =
            Json::String(".oah/config.toml: unknown key stages.other".into());
    });
    let report = scratch.config();
    assert_eq!(failed_cases(&report), ["unknown-key-stages"]);
    assert!(
        report.failures[0].detail.contains("notice says"),
        "{}",
        report.failures[0]
    );
}

#[test]
fn an_unregistered_deviation_fails_the_area() {
    let scratch = Scratch::new();
    scratch.edit_json("config/MANIFEST.json", |json| {
        json["deviations"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({
                "case": "bom-stages",
                "change": "a future change",
                "design": "3.1"
            }));
    });
    let report = scratch.config();
    assert_eq!(report.state, AreaState::Enabled);
    assert!(
        failed_cases(&report).contains(&"bom-stages"),
        "{:#?}",
        report.failures
    );
    assert!(!report.is_ok());
}

#[test]
fn a_malformed_enabled_manifest_fails_instead_of_turning_pending() {
    let scratch = Scratch::new();
    std::fs::write(scratch.path("config/MANIFEST.json"), "{ not json").unwrap();
    let report = scratch.config();
    assert_eq!(report.state, AreaState::Enabled);
    assert_eq!(failed_cases(&report), ["MANIFEST.json"]);

    let scratch = Scratch::new();
    scratch.edit_json("config/MANIFEST.json", |json| {
        json["status"] = Json::String("enabled".into());
    });
    let report = scratch.config();
    assert_eq!(failed_cases(&report), ["MANIFEST.json"]);
    assert!(
        report.failures[0].detail.contains("pending"),
        "{}",
        report.failures[0]
    );
}

#[test]
fn a_missing_enabled_area_is_reported_not_skipped() {
    let scratch = Scratch::new();
    std::fs::remove_dir_all(scratch.path("config")).unwrap();
    let reports = run_all(&scratch.root);
    let config = reports
        .iter()
        .find(|report| report.area == "config")
        .expect("still reported");
    assert_eq!(config.state, AreaState::Enabled);
    assert!(!config.is_ok());
}

#[test]
fn missing_duplicate_and_unlisted_cases_fail() {
    let scratch = Scratch::new();
    std::fs::remove_dir_all(scratch.path("config/crlf-stages")).unwrap();
    std::fs::remove_file(scratch.path("config/skills-merge/expect.json")).unwrap();
    std::fs::create_dir(scratch.path("config/stray-case")).unwrap();
    let report = scratch.config();
    assert_eq!(
        failed_cases(&report),
        ["crlf-stages", "skills-merge", "stray-case"]
    );

    let scratch = Scratch::new();
    scratch.edit_json("config/MANIFEST.json", |json| {
        let cases = json["cases"].as_array_mut().unwrap();
        let first = cases[0].clone();
        cases.push(first);
    });
    let report = scratch.config();
    assert_eq!(failed_cases(&report), ["MANIFEST.json"]);
    assert!(
        report.failures[0].detail.contains("twice"),
        "{}",
        report.failures[0]
    );
}

#[test]
fn an_unknown_operation_in_an_enabled_area_fails() {
    let scratch = Scratch::new();
    scratch.edit_json("config/MANIFEST.json", |json| {
        let case = json["cases"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|case| case["name"] == "skills-merge")
            .unwrap();
        case["function"] = Json::String("oah.config.Config.save".into());
    });
    let report = scratch.config();
    assert_eq!(failed_cases(&report), ["skills-merge"]);
    assert!(
        report.failures[0].detail.contains("no operation"),
        "{}",
        report.failures[0]
    );
}

#[test]
fn an_input_path_outside_the_case_folder_is_refused() {
    let scratch = Scratch::new();
    scratch.edit_json("config/crlf-stages/input.json", |json| {
        let files = json["files"].as_object_mut().unwrap();
        let text = files.values().next().unwrap().clone();
        files.insert("../escaped.toml".into(), text);
    });
    let report = scratch.config();
    assert_eq!(failed_cases(&report), ["crlf-stages"]);
    assert!(
        report.failures[0].detail.contains("plain relative path"),
        "{}",
        report.failures[0]
    );
}

#[test]
fn a_pending_area_with_a_broken_case_is_still_reported() {
    let scratch = Scratch::new();
    std::fs::remove_file(scratch.path("hashes/MANIFEST.json")).unwrap();
    let report = run_area(&scratch.root, "hashes");
    assert_eq!(report.state, AreaState::Pending);
    assert_eq!(failed_cases(&report), ["MANIFEST.json"]);
}

#[test]
fn running_never_writes_under_the_fixture_tree() {
    let before = snapshot(&fixture_root());
    let reports = run_all(&fixture_root());
    assert!(reports.iter().all(AreaReport::is_ok));
    assert_eq!(snapshot(&fixture_root()), before);
}
