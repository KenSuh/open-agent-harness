// conformance/v0.4.0 에 영역 여덟 개가 정확히 있고, 각 영역이 pending 매니페스트를 갖고, 모든 사례 폴더에 기대값과 입력이 있는지 확인한다.
use oah_conformance::list_areas;
use std::path::{Path, PathBuf};

fn conformance_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("conformance")
}

/// Every file below `root`, sorted.
fn files_below(root: &Path) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(&folder) else {
            continue;
        };
        for entry in entries.filter_map(Result::ok) {
            let path = entry.path();
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

const AREAS: [&str; 8] = [
    "config",
    "doctor",
    "hashes",
    "license",
    "models",
    "review",
    "rules",
    "skills-roots",
];

#[test]
fn the_eight_areas_and_nothing_else() {
    let root = conformance_root().join("v0.4.0");
    assert_eq!(list_areas(&root), AREAS);
    let stray: Vec<_> = std::fs::read_dir(&root)
        .expect("read conformance/v0.4.0")
        .filter_map(Result::ok)
        .filter(|entry| !entry.path().is_dir())
        .map(|entry| entry.file_name())
        .collect();
    assert!(stray.is_empty(), "files beside the areas: {stray:?}");
}

#[test]
fn each_area_is_pending_and_every_case_is_complete() {
    let root = conformance_root().join("v0.4.0");
    for area in AREAS {
        let folder = root.join(area);
        let manifest = std::fs::read_to_string(folder.join("MANIFEST.json"))
            .unwrap_or_else(|error| panic!("{area}/MANIFEST.json: {error}"));
        assert!(
            manifest.contains("\"status\": \"pending\""),
            "{area} is not pending"
        );
        assert!(
            manifest.contains(&format!("\"area\": \"{area}\"")),
            "{area}/MANIFEST.json names another area"
        );
        let cases = list_areas(&folder);
        assert!(!cases.is_empty(), "{area} has no cases");
        for case in cases {
            let dir = folder.join(&case);
            assert!(
                case.bytes()
                    .next()
                    .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
                    && case
                        .bytes()
                        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-'),
                "{area}/{case}: case names match [a-z0-9][a-z0-9-]*"
            );
            assert!(
                dir.join("expect.json").is_file(),
                "{area}/{case} has no expect.json"
            );
            assert!(
                dir.join("input.json").is_file() || dir.join("input").is_dir(),
                "{area}/{case} has no input"
            );
            assert!(
                manifest.contains(&format!("\"name\": \"{case}\"")),
                "{area}/MANIFEST.json does not list {case}"
            );
        }
    }
}

/// No `.gitattributes` guards these files, so a byte Git could rewrite on checkout (a CR) or a file that is not
/// UTF-8 text must never be committed; raw input bytes belong in input.json as `bytes` (hex).
#[test]
fn every_file_is_utf8_without_a_cr_byte() {
    let files = files_below(&conformance_root());
    assert!(!files.is_empty(), "no files under conformance/");
    for path in files {
        let bytes = std::fs::read(&path).expect("read fixture");
        assert!(
            !bytes.contains(&b'\r'),
            "{} holds a CR byte",
            path.display()
        );
        assert!(
            std::str::from_utf8(&bytes).is_ok(),
            "{} is not UTF-8",
            path.display()
        );
    }
}
