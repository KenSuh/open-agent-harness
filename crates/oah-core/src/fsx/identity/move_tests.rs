// 자체 이동의 소유 증거가 대체 항목을 채택하지 않는지 실제 파일 시스템에서 확인한다.
use super::{EntryId, MoveProof, Now, now, rename_new};
use std::fs;

#[test]
fn operation_identity_moves_only_the_acquired_entry() {
    let root = tempfile::tempdir().unwrap();
    let from = root.path().join("before");
    let to = root.path().join("after-with-a-longer-name");
    fs::create_dir(&from).unwrap();
    let before = EntryId::of(&from).unwrap();
    let proof = MoveProof::acquire(&from, before).unwrap().unwrap();
    rename_new(&from, &to).unwrap();
    let after = proof.after(&to).unwrap().unwrap();
    assert_eq!(now(&to, after).unwrap(), Now::Same);
    assert_eq!(
        MoveProof::acquire(&to, before).unwrap().is_some(),
        before == after
    );

    fs::rename(&to, root.path().join("retained-original")).unwrap();
    fs::create_dir(&from).unwrap();
    fs::write(from.join("user.txt"), b"preserve replacement").unwrap();
    rename_new(&from, &to).unwrap();
    assert!(proof.after(&to).unwrap().is_none());
    assert_eq!(
        fs::read(to.join("user.txt")).unwrap(),
        b"preserve replacement"
    );
    assert!(root.path().join("retained-original").is_dir());
}
