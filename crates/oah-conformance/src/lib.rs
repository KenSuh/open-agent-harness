// 적합성 시험용 크레이트: conformance/ 아래 영역(폴더) 목록을 읽는다.
//! Test-only crate for the golden fixtures a later slice adds under `conformance/`.

use std::path::Path;

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
