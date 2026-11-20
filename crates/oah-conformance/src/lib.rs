// 적합성 시험용 크레이트: conformance/ 아래 영역(폴더) 목록을 읽고, 켜진 영역을 실제 oah-core 로 실행한다.
//! Test-only crate that runs the golden fixtures under `conformance/` against `oah-core` (see [`runner`]).

mod doctor;
pub mod runner;

use std::path::Path;

pub const REQUIRED_AREAS: &[&str] = &[
    "config",
    "doctor",
    "hashes",
    "license",
    "models",
    "review",
    "rules",
    "skills-roots",
];

/// Names of the areas (sub-folders) under `root`, sorted; empty when `root` is absent.
pub fn list_areas(root: &Path) -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    let mut areas: Vec<String> = entries
        .filter_map(Result::ok)
        .filter(|entry| entry.path().is_dir())
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect();
    areas.sort();
    areas
}
