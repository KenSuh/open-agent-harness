// 실행기 음성 대조: 고정 트리의 임시 사본을 하나씩 망가뜨려 실행기가 그 영역·사례를 실패로 보고하는지, 원본 conformance/ 는 그대로인지 확인한다.
use oah_conformance::runner::{AreaReport, AreaState, ENABLED, run_all, run_area};
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
fn a_wrong_rules_expectation_fails_that_case_only() {
    let scratch = Scratch::new();
    scratch.edit_json("rules/no-files/expect.json", |json| {
        json["ok"]["block_bytes"] = Json::from(0);
    });
    let report = run_area(&scratch.root, "rules");
    assert_eq!(failed_cases(&report), ["no-files"]);
    assert_eq!(report.passed, 42);
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
fn design_expectation_contract_cannot_drop_assertions() {
    let manifest: Json = serde_json::from_slice(
        &std::fs::read(fixture_root().join("config/MANIFEST.json")).unwrap(),
    )
    .unwrap();
    let design: Vec<&str> = manifest["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|case| case["source"] == "design")
        .map(|case| case["name"].as_str().unwrap())
        .collect();
    assert_eq!(design.len(), 4);
    for name in design {
        for mutation in [
            "empty",
            "missing-notices",
            "bad-notices",
            "missing-preservation",
            "bad-preservation",
            "missing-semantic",
            "empty-semantic",
            "bad-semantic",
            "missing-unused",
        ] {
            let scratch = Scratch::new();
            scratch.edit_json(&format!("config/{name}/expect.json"), |expect| {
                let ok = expect["ok"].as_object_mut().unwrap();
                let semantic = if ok.contains_key("stages") {
                    "stages"
                } else {
                    "agents"
                };
                match mutation {
                    "empty" => ok.clear(),
                    "missing-notices" => {
                        ok.remove("notices");
                    }
                    "bad-notices" => {
                        ok.insert("notices".into(), Json::Null);
                    }
                    "missing-preservation" => {
                        ok.remove("file_unchanged");
                    }
                    "bad-preservation" => {
                        ok.insert("file_unchanged".into(), Json::Null);
                    }
                    "missing-semantic" => {
                        ok.remove(semantic);
                    }
                    "empty-semantic" => {
                        ok.insert(semantic.into(), serde_json::json!({}));
                    }
                    "bad-semantic" => {
                        ok.insert(semantic.into(), serde_json::json!([]));
                    }
                    "missing-unused" if semantic == "agents" => {
                        ok.remove("unused_kept_in_file");
                    }
                    "missing-unused" => {
                        ok.remove("notices");
                    }
                    _ => unreachable!(),
                }
            });
            let report = scratch.config();
            assert_eq!(
                failed_cases(&report),
                [name],
                "{mutation}: {:#?}",
                report.failures
            );
            assert_eq!(report.passed, 29, "{name}/{mutation}");
            assert!(
                report.failures[0]
                    .detail
                    .contains("expect.json design expectation requires"),
                "{}",
                report.failures[0]
            );
        }
    }
}

/// Turns the legacy-agents-win design case into one whose file names an agent with only a key 1.0 does not
/// use, with the expectation the core gives for it: that agent has no keys, and its key is kept unused.
fn empty_agent_case(scratch: &Scratch) {
    scratch.edit_json("config/legacy-agents-win/input.json", |input| {
        input["files"] = serde_json::json!({
            ".oah/config.toml": "[engines.codex]\npermission_mode = \"acceptEdits\"\n"
        });
    });
    scratch.edit_json("config/legacy-agents-win/expect.json", |expect| {
        *expect = serde_json::json!({"ok": {
            "agents": {"codex": {}},
            "file_unchanged": true,
            "notices": [],
            "unused_kept_in_file": ["permission_mode"]
        }});
    });
}

#[test]
fn empty_given_agent_projection_passes_when_it_is_the_core_reading() {
    let scratch = Scratch::new();
    empty_agent_case(&scratch);
    let report = scratch.config();
    assert!(report.is_ok(), "{:#?}", report.failures);
    assert_eq!(report.passed, 30);
}

#[test]
fn empty_given_agent_projection_keeps_every_other_requirement() {
    // Allowing an empty object for one agent relaxes nothing else: the agents map, the notices, the
    // preservation flag and the unused list stay required, and an agent value must still be an object.
    for (mutation, wanted) in [
        ("array-agent", "typed agents values"),
        ("string-agent", "typed agents values"),
        ("null-agent", "typed agents values"),
        ("empty-map", "a nonempty agents semantic projection"),
        ("missing-agents", "a nonempty agents semantic projection"),
        ("missing-unused", "an unused_kept_in_file list of strings"),
        ("missing-notices", "a notices list of strings"),
        ("missing-preservation", "boolean file_unchanged"),
    ] {
        let scratch = Scratch::new();
        empty_agent_case(&scratch);
        scratch.edit_json("config/legacy-agents-win/expect.json", |expect| {
            let ok = expect["ok"].as_object_mut().unwrap();
            match mutation {
                "array-agent" => ok["agents"]["codex"] = serde_json::json!([]),
                "string-agent" => ok["agents"]["codex"] = serde_json::json!("codex"),
                "null-agent" => ok["agents"]["codex"] = Json::Null,
                "empty-map" => ok["agents"] = serde_json::json!({}),
                "missing-agents" => drop(ok.remove("agents")),
                "missing-unused" => drop(ok.remove("unused_kept_in_file")),
                "missing-notices" => drop(ok.remove("notices")),
                "missing-preservation" => drop(ok.remove("file_unchanged")),
                _ => unreachable!(),
            }
        });
        let report = scratch.config();
        assert_eq!(
            failed_cases(&report),
            ["legacy-agents-win"],
            "{mutation}: {:#?}",
            report.failures
        );
        assert_eq!(report.passed, 29, "{mutation}");
        let detail = &report.failures[0].detail;
        assert!(
            detail.contains("expect.json design expectation requires") && detail.contains(wanted),
            "{mutation}: {detail}"
        );
    }
}

#[test]
fn empty_given_agent_projection_is_compared_like_any_other() {
    // An empty agent object is no wildcard: it fails where the core gives the agent keys, and a wrong unused
    // list, an unused key claimed as used, or another agent still fails where the agent is empty.
    for mutation in [
        "fixture-agent-emptied",
        "wrong-unused",
        "unused-key-as-given",
        "other-agent",
    ] {
        let scratch = Scratch::new();
        if mutation == "fixture-agent-emptied" {
            scratch.edit_json("config/legacy-agents-win/expect.json", |expect| {
                expect["ok"]["agents"]["codex"] = serde_json::json!({});
            });
        } else {
            empty_agent_case(&scratch);
            scratch.edit_json("config/legacy-agents-win/expect.json", |expect| {
                let ok = expect["ok"].as_object_mut().unwrap();
                match mutation {
                    "wrong-unused" => ok["unused_kept_in_file"] = serde_json::json!([]),
                    "unused-key-as-given" => {
                        ok["agents"]["codex"] =
                            serde_json::json!({"permission_mode": "acceptEdits"})
                    }
                    "other-agent" => ok["agents"] = serde_json::json!({"claude": {}}),
                    _ => unreachable!(),
                }
            });
        }
        let report = scratch.config();
        assert_eq!(
            failed_cases(&report),
            ["legacy-agents-win"],
            "{mutation}: {:#?}",
            report.failures
        );
        assert_eq!(report.passed, 29, "{mutation}");
        let detail = &report.failures[0].detail;
        assert!(
            !detail.contains("design expectation requires"),
            "{mutation} is a mismatch, not a malformed expectation: {detail}"
        );
    }
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
fn required_area_inventory_reports_missing_enabled_and_pending_inputs() {
    let required = oah_conformance::list_areas(&fixture_root());
    assert_eq!(required.len(), 8);
    for area in &required {
        for mutation in ["folder", "manifest", "case-list"] {
            let scratch = Scratch::new();
            match mutation {
                "folder" => std::fs::remove_dir_all(scratch.path(area)).unwrap(),
                "manifest" => {
                    std::fs::remove_file(scratch.path(&format!("{area}/MANIFEST.json"))).unwrap()
                }
                "case-list" => scratch.edit_json(&format!("{area}/MANIFEST.json"), |json| {
                    json["cases"].as_array_mut().unwrap().pop();
                }),
                _ => unreachable!(),
            }
            let reports = run_all(&scratch.root);
            assert_eq!(
                reports
                    .iter()
                    .map(|report| &report.area)
                    .collect::<Vec<_>>(),
                required.iter().collect::<Vec<_>>()
            );
            let report = reports.iter().find(|report| report.area == *area).unwrap();
            assert_eq!(
                report.state,
                if ENABLED.contains(&area.as_str()) {
                    AreaState::Enabled
                } else {
                    AreaState::Pending
                }
            );
            assert!(!report.is_ok(), "missing {area}/{mutation} went unreported");
            assert_eq!(reports.iter().filter(|report| !report.is_ok()).count(), 1);
        }
    }
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
    std::fs::remove_file(scratch.path("models/MANIFEST.json")).unwrap();
    let report = run_area(&scratch.root, "models");
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
