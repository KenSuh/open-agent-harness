// conformance/v0.4.0/rules 의 43 사례가 실제 oah-core 의 규칙 계획 함수로 고정 기대값과 같게 나오는지 확인한다.
use oah_conformance::runner::{AreaState, run_area};
use std::path::{Path, PathBuf};

fn fixture_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("conformance")
        .join("v0.4.0")
}

#[test]
fn every_rules_case_passes_against_the_real_core() {
    let report = run_area(&fixture_root(), "rules");
    println!("{}", report.summary_line());
    for failure in &report.failures {
        println!("  {failure}");
    }
    assert_eq!(report.state, AreaState::Enabled);
    assert!(report.is_ok(), "rules failures: {:#?}", report.failures);
    assert_eq!(report.cases, 43);
    assert_eq!(report.passed, 43);
    assert_eq!(report.deviations, 0);
}
