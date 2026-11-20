// conformance/v0.4.0/doctor 의 여덟 사례가 실제 oah-core 의 doctor 함수로 고정 기대값과 같게 나오는지 확인한다.
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
fn every_doctor_case_passes_against_the_real_core() {
    let report = run_area(&fixture_root(), "doctor");
    println!("{}", report.summary_line());
    for failure in &report.failures {
        println!("  {failure}");
    }
    assert_eq!(report.state, AreaState::Enabled);
    assert!(report.is_ok(), "doctor failures: {:#?}", report.failures);
    assert_eq!(report.cases, 8);
    assert_eq!(report.passed, 8);
    assert_eq!(report.unchanged, 8);
    assert_eq!(report.design, 0);
    assert_eq!(report.deviations, 0);
}
