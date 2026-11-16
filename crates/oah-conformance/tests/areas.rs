// conformance/ 폴더가 없을 때 영역 0개이고, 폴더가 있으면 하위 폴더만 나열하는지 확인한다.
use oah_conformance::list_areas;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

struct Fixture(PathBuf);

impl Fixture {
    fn new() -> Self {
        static NEXT_ID: AtomicU64 = AtomicU64::new(0);
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "oah-conformance-{}-{timestamp}-{}",
            std::process::id(),
            NEXT_ID.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).unwrap();
        Self(root)
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        if let Err(error) = std::fs::remove_dir_all(&self.0) {
            eprintln!("remove fixture {}: {error}", self.0.display());
        }
    }
}

#[test]
fn missing_folder_has_no_areas() {
    let fixture = Fixture::new();
    assert!(list_areas(&fixture.0.join("absent")).is_empty());
}

#[test]
fn lists_only_sub_folders_sorted() {
    let fixture = Fixture::new();
    let root = &fixture.0;
    std::fs::create_dir(root.join("b")).unwrap();
    std::fs::create_dir(root.join("a")).unwrap();
    std::fs::write(root.join("note.txt"), "x").unwrap();
    assert_eq!(list_areas(root), ["a", "b"]);
}
