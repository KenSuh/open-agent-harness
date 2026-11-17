// conformance/v0.4.0 의 모든 영역을 실행해 config 30개 사례가 실제 oah-core 로 통과하고 나머지 일곱 영역은 pending 으로 남는지 확인한다.
use oah_conformance::runner::{AreaReport, AreaState, run_all};
use std::path::{Path, PathBuf};

fn fixture_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("conformance")
        .join("v0.4.0")
}

fn reports() -> Vec<AreaReport> {
    let reports = run_all(&fixture_root());
    for report in &reports {
        println!("{}", report.summary_line());
        for failure in &report.failures {
            println!("  {failure}");
        }
    }
    reports
}

#[test]
fn every_config_case_passes_against_the_real_core() {
    let reports = reports();
    let config = reports
        .iter()
        .find(|report| report.area == "config")
        .expect("the config area ran");
    assert_eq!(config.state, AreaState::Enabled);
    assert!(config.is_ok(), "config failures: {:#?}", config.failures);
    assert_eq!(config.cases, 30);
    assert_eq!(config.passed, 30);
    assert_eq!(config.unchanged, 23);
    assert_eq!(config.design, 4);
    assert_eq!(config.deviations, 3);
}

#[test]
fn the_other_seven_areas_stay_pending_and_complete() {
    let reports = reports();
    let pending: Vec<&str> = reports
        .iter()
        .filter(|report| report.state == AreaState::Pending)
        .map(|report| report.area.as_str())
        .collect();
    assert_eq!(
        pending,
        [
            "doctor",
            "hashes",
            "license",
            "models",
            "review",
            "rules",
            "skills-roots"
        ]
    );
    for report in reports
        .iter()
        .filter(|report| report.state == AreaState::Pending)
    {
        assert!(report.is_ok(), "{}: {:#?}", report.area, report.failures);
        assert!(report.cases > 0, "{} has no cases", report.area);
        assert_eq!(report.passed, 0, "{} ran cases while pending", report.area);
    }
}
