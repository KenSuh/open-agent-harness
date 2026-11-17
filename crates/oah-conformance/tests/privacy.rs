// conformance/ 아래 모든 파일과 동일한 규약 표에서 디코딩된 개인정보 층을 검사한다.
use std::path::{Path, PathBuf};

#[path = "support/privacy_probes.rs"]
mod probes;
#[path = "support/privacy_scan.rs"]
mod scanner;

use scanner::privacy_findings;

fn conformance_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("conformance")
}

fn files_below(root: &Path) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        for entry in std::fs::read_dir(&folder).expect("read conformance directory") {
            let path = entry.expect("read conformance entry").path();
            if path.is_dir() {
                pending.push(path);
            } else {
                found.push(path);
            }
        }
    }
    found.sort();
    found
}

#[test]
fn no_file_under_conformance_holds_private_data() {
    let root = conformance_root();
    let files = files_below(&root);
    assert!(!files.is_empty(), "no conformance files");
    let mut hits = Vec::new();
    for path in files {
        let bytes = std::fs::read(&path).expect("read fixture");
        for finding in
            scanner::file_privacy_findings(path.strip_prefix(&root).expect("fixture path"), &bytes)
        {
            hits.push(format!("{}: {finding}", path.display()));
        }
    }
    assert!(
        hits.is_empty(),
        "private data in fixtures:\n{}",
        hits.join("\n")
    );
}

#[test]
fn every_private_form_is_rejected_and_controls_are_allowed() {
    for probe in probes::samples() {
        let findings = if probe.path == "correction-probe.json" && probe.bytes.is_none() {
            privacy_findings(&probe.text)
        } else {
            scanner::file_privacy_findings(Path::new(probe.path), probe.data())
        };
        assert_eq!(
            !findings.is_empty(),
            probe.forbidden,
            "{}: {findings:?}",
            probe.name
        );
    }
}
