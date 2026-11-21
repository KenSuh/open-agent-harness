// 로컬 수집기 사례: 태어나지 않은 HEAD, 저장소가 아닌 폴더, 심볼릭 링크·대소문자 충돌 항목, 1 MiB 초과 파일, 물려받은 GIT_* 변수를 무해한 픽스처로 확인한다.
//! The local collector on benign fixture repositories: its refusals, worded as v0.4.0's
//! `ReviewFailed("collector", ...)` for a fresh source, and its isolation from inherited git variables.

use crate::fixtures::{
    add_entry, case, commit, commit_fields, git, ok, request, review_at, review_with, stage,
    upstream,
};
use crate::registry::local::LocalGit;
use crate::review::identity::identity;
use crate::review::snapshot::{Collector, Observation};
use crate::review::{FailReason, ReviewError};
use serde_json::{Map, Value, json};
use std::path::Path;

const SKILL: &str =
    "---\nname: tidy\ndescription: tidy guidance for testing\n---\n\nKeep notes short.\n";

fn files(entries: &[(&str, Value)]) -> Map<String, Value> {
    entries
        .iter()
        .map(|(path, value)| (path.to_string(), value.clone()))
        .collect()
}

fn basic() -> Map<String, Value> {
    let license =
        case("review", "record-mit-reviewed").input["upstream"]["files"]["LICENSE"].clone();
    files(&[("LICENSE", license), ("skills/tidy/SKILL.md", json!(SKILL))])
}

/// The id v0.4.0's own review gives the clean `basic()` folder below: the frozen Python `review.review` run once over the same
/// one-commit fixture, read through a local transport, with the fixture clock and MIT allowed.
const BASIC_RECORD_ID: &str =
    "sha256:b161738f7caa0eed9d324f566f08f838b7d661dcea5e1a4c9255a6c1c703b2e6";

fn collector_error(repository: &Path) -> String {
    match review_at(repository, &request("acme/skills", "skills/tidy")) {
        Err(
            error @ ReviewError::Failed {
                reason: FailReason::Collector,
                ..
            },
        ) => {
            assert!(error.environment());
            error.to_string()
        }
        other => panic!("expected a collector failure, got {other:?}"),
    }
}

#[test]
fn a_clean_folder_reviews_as_v040_does() {
    let scratch = tempfile::tempdir().expect("scratch");
    let repository = scratch.path().join("upstream");
    stage(&repository, &commit_fields(), &basic());
    commit(&repository, &commit_fields());
    let record =
        review_at(&repository, &request("acme/skills", "skills/tidy")).expect("a reviewed record");
    assert_eq!(record.id, BASIC_RECORD_ID);
    assert_eq!(record.id, record.content_id());
}

#[test]
fn an_unborn_head_is_blocked_as_ref_missing() {
    let scratch = tempfile::tempdir().expect("scratch");
    let repository = scratch.path().join("upstream");
    stage(&repository, &commit_fields(), &Map::new());
    let (_, spec) = identity("acme/skills", "skills/tidy").expect("identity");
    let observation = LocalGit::new(&git().1, &repository, &spec.locator, &git().0).observe(&spec);
    assert_eq!(
        observation,
        Observation::Blocked {
            problems: vec!["ref-missing".to_string()]
        }
    );
    assert_eq!(
        observation.result(),
        ("blocked", vec!["ref-missing".to_string()])
    );
    assert_eq!(
        collector_error(&repository),
        "OAH_SKILL_REVIEW_FAILED(collector): the collector could not verify acme/skills (blocked: ref-missing)"
    );
}

#[test]
fn a_folder_that_is_not_a_repository_fails_without_reading_an_enclosing_one() {
    let scratch = tempfile::tempdir().expect("scratch");
    let enclosing = scratch.path().join("enclosing");
    upstream(
        &enclosing,
        &json!({"files": basic(), "branch": "main", "author": "Fixture", "email": "fixture",
                                  "date": "2026-01-01T00:00:00+00:00", "message": "skills"}),
    );
    let inner = enclosing.join("plain");
    std::fs::create_dir_all(&inner).expect("a plain folder");
    assert_eq!(
        collector_error(&inner),
        "OAH_SKILL_REVIEW_FAILED(collector): the collector could not verify acme/skills (failed: git-failed)"
    );
}

#[test]
fn a_symbolic_link_in_the_folder_is_an_unsafe_entry() {
    let scratch = tempfile::tempdir().expect("scratch");
    let repository = scratch.path().join("upstream");
    stage(&repository, &commit_fields(), &basic());
    add_entry(
        &repository,
        scratch.path(),
        "120000",
        "skills/tidy/link",
        b"SKILL.md",
    );
    commit(&repository, &commit_fields());
    assert_eq!(
        collector_error(&repository),
        "OAH_SKILL_REVIEW_FAILED(collector): the collector could not verify acme/skills (unverified: unsafe-entry)"
    );
}

#[test]
fn names_that_collide_on_a_case_insensitive_system_are_an_unsafe_entry() {
    let scratch = tempfile::tempdir().expect("scratch");
    let repository = scratch.path().join("upstream");
    stage(&repository, &commit_fields(), &basic());
    add_entry(
        &repository,
        scratch.path(),
        "100644",
        "skills/tidy/Notes.md",
        b"one\n",
    );
    add_entry(
        &repository,
        scratch.path(),
        "100644",
        "skills/tidy/notes.md",
        b"two\n",
    );
    commit(&repository, &commit_fields());
    assert_eq!(
        collector_error(&repository),
        "OAH_SKILL_REVIEW_FAILED(collector): the collector could not verify acme/skills (unverified: unsafe-entry)"
    );
}

#[test]
fn a_file_over_one_mib_is_oversize() {
    let scratch = tempfile::tempdir().expect("scratch");
    let repository = scratch.path().join("upstream");
    let mut entries = basic();
    entries.insert(
        "skills/tidy/data.txt".to_string(),
        json!({"text": "", "pad": "x", "size": (1 << 20) + 1}),
    );
    upstream(
        &repository,
        &json!({"files": entries, "branch": "main", "author": "Fixture", "email": "fixture",
                                   "date": "2026-01-01T00:00:00+00:00", "message": "skills"}),
    );
    assert_eq!(
        collector_error(&repository),
        "OAH_SKILL_REVIEW_FAILED(collector): the collector could not verify acme/skills (unverified: oversize)"
    );
}

#[test]
fn inherited_git_variables_do_not_redirect_the_collector() {
    let scratch = tempfile::tempdir().expect("scratch");
    let wanted = case("review", "record-mit-reviewed");
    let repository = scratch.path().join("upstream");
    upstream(&repository, &wanted.input["upstream"]);
    let decoy = scratch.path().join("decoy");
    upstream(
        &decoy,
        &json!({"files": basic(), "branch": "main", "author": "Fixture", "email": "fixture",
                              "date": "2026-01-01T00:00:00+00:00", "message": "decoy"}),
    );
    let decoy_git = decoy.join(".git");
    let env = git()
        .0
        .clone()
        .with("GIT_DIR", decoy_git.as_os_str())
        .with("GIT_WORK_TREE", decoy.as_os_str())
        .with(
            "GIT_OBJECT_DIRECTORY",
            decoy_git.join("objects").as_os_str(),
        )
        .with("GIT_CONFIG_PARAMETERS", "'core.hookspath'='/nonexistent'");
    let record =
        review_with(&repository, &wanted.input, &env).expect("the configured repository is read");
    assert_eq!(ok(record.to_json()), wanted.expect);
}
