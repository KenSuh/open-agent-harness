// conformance/ 폴더가 없을 때 영역 0개이고, 폴더가 있으면 하위 폴더만 나열하는지 확인한다.
use oah_conformance::list_areas;
use std::path::PathBuf;

#[test]
fn repository_has_no_conformance_areas_yet() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../conformance");
    if !root.exists() {
        assert!(list_areas(&root).is_empty());
    }
}

#[test]
fn lists_only_sub_folders_sorted() {
    let root = std::env::temp_dir().join(format!("oah-conformance-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(root.join("b")).unwrap();
    std::fs::create_dir_all(root.join("a")).unwrap();
    std::fs::write(root.join("note.txt"), "x").unwrap();
    assert_eq!(list_areas(&root), ["a", "b"]);
    std::fs::remove_dir_all(&root).unwrap();
    assert!(list_areas(&root).is_empty());
}
