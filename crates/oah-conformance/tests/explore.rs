// 탐색 영역 적합성: review 21·license 5·hashes 3 사례가 실제 oah-core 로 통과하고, 임시 사본의 기대값·입력 하나를 바꾸면 그 사례만 실패하는지 확인한다.
use oah_conformance::runner::{AreaReport, AreaState, ENABLED, run_area};
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

    fn edit_json(&self, relative: &str, change: impl FnOnce(&mut Json)) {
        let path = self.root.join(relative);
        let mut json: Json =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        change(&mut json);
        std::fs::write(&path, serde_json::to_string_pretty(&json).unwrap()).unwrap();
    }
}

fn report(root: &Path, area: &str) -> AreaReport {
    let report = run_area(root, area);
    println!("{}", report.summary_line());
    for failure in &report.failures {
        println!("  {failure}");
    }
    report
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

/// (area, cases, unchanged v0.4.0 cases): every case runs and passes, with no design case or deviation.
const AREAS: [(&str, usize, usize); 3] = [("review", 21, 21), ("license", 5, 5), ("hashes", 3, 0)];

#[test]
fn every_explore_case_passes_against_the_real_core() {
    for (area, cases, unchanged) in AREAS {
        assert!(ENABLED.contains(&area), "{area} is not enabled");
        let report = report(&fixture_root(), area);
        assert_eq!(report.state, AreaState::Enabled, "{area}");
        assert!(report.is_ok(), "{area} failures: {:#?}", report.failures);
        assert_eq!(report.cases, cases, "{area}");
        assert_eq!(report.passed, cases, "{area}");
        assert_eq!(report.unchanged, unchanged, "{area}");
        assert_eq!(report.design, 0, "{area}");
        assert_eq!(report.deviations, 0, "{area}");
    }
}

#[test]
fn a_wrong_hash_expectation_fails_that_case_only() {
    let scratch = Scratch::new();
    let other = "b22c4d22aa319d2924312fa18fcbfedc88fa89c74dbd8f298c683e1caf7b7f24";
    scratch.edit_json("hashes/one-file/expect.json", |json| {
        json["ok"] = Json::String(other.into());
    });
    let report = report(&scratch.root, "hashes");
    assert_eq!(failed_cases(&report), ["one-file"]);
    assert_eq!(report.passed, 2);
    let detail = &report.failures[0].detail;
    assert!(
        detail.contains("93c36952d8843b33e68ae970ebabcc03e1687c9bb0dd1ecaf357060318952f55")
            && detail.contains(other),
        "{detail}"
    );
}

#[test]
fn a_wrong_license_row_fails_that_case_only() {
    let scratch = Scratch::new();
    scratch.edit_json("license/expression/expect.json", |json| {
        json["ok"]["choice"][0] = Json::String("conjunction".into());
    });
    let report = report(&scratch.root, "license");
    assert_eq!(failed_cases(&report), ["expression"]);
    assert_eq!(report.passed, 4);
    assert!(
        report.failures[0].detail.contains("$.ok.choice[0]"),
        "{}",
        report.failures[0]
    );
}

#[test]
fn a_wrong_record_commit_fails_that_case_only() {
    let scratch = Scratch::new();
    scratch.edit_json("review/record-mit-reviewed/expect.json", |json| {
        json["ok"]["source"]["commit"] = Json::String("0".repeat(40));
    });
    let report = report(&scratch.root, "review");
    assert_eq!(failed_cases(&report), ["record-mit-reviewed"]);
    assert_eq!(report.passed, 20);
    let detail = &report.failures[0].detail;
    assert!(
        detail.contains("$.ok.source.commit")
            && detail.contains("0260774fc7db54f6a482fd1e9e4a1fe1b40a7f97"),
        "{detail}"
    );
}

#[test]
fn a_record_is_read_from_the_repository_the_input_describes() {
    // Another commit message gives another commit, which the collector reads from the new repository; the
    // unchanged expectation then no longer matches.
    let scratch = Scratch::new();
    scratch.edit_json("review/record-mit-reviewed/input.json", |json| {
        json["upstream"]["message"] = Json::String("other skills".into());
    });
    let report = report(&scratch.root, "review");
    assert_eq!(failed_cases(&report), ["record-mit-reviewed"]);
    assert!(
        report.failures[0].detail.contains("$.ok.id")
            || report.failures[0].detail.contains("$.ok.source"),
        "{}",
        report.failures[0]
    );
}

#[test]
fn an_unknown_operation_or_input_key_in_an_explore_area_fails() {
    let scratch = Scratch::new();
    scratch.edit_json("license/MANIFEST.json", |json| {
        let case = json["cases"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|case| case["name"] == "declared")
            .unwrap();
        case["function"] = Json::String("oah.design.sources.license.discover".into());
    });
    scratch.edit_json("license/classify/input.json", |json| {
        json["extra"] = Json::Bool(true);
    });
    let report = report(&scratch.root, "license");
    assert_eq!(failed_cases(&report), ["classify", "declared"]);
    assert!(
        report
            .failures
            .iter()
            .any(|failure| failure.detail.contains("no operation")),
        "{:#?}",
        report.failures
    );
    assert!(
        report
            .failures
            .iter()
            .any(|failure| failure.detail.contains("unknown key")),
        "{:#?}",
        report.failures
    );
}

#[test]
fn running_the_explore_areas_never_writes_under_the_fixture_tree() {
    let before = snapshot(&fixture_root());
    for (area, _, _) in AREAS {
        assert!(run_area(&fixture_root(), area).is_ok(), "{area}");
    }
    assert_eq!(snapshot(&fixture_root()), before);
}
