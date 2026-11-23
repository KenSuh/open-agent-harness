use oah_core::config::AgentId;
use oah_core::error::{Coded, Error, codes};
use oah_core::net::RecordingTransport;
use oah_core::registry::github::{GitHub, Recorded};
use oah_core::review::delivery::{DeliveryError, GitHubSkillSource, ReviewApproval, authorize};
use oah_core::review::{
    GateResult, ReasonKind, ReviewOutcome, ReviewRequest, ReviewedSkillV1, Store, StoreError,
    review_and_store,
};
use serde::Serialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::time::UNIX_EPOCH;

const REPO: &str = "acme/skills";
const FOLDER: &str = "skills/tidy";
const COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";
const NEXT_COMMIT: &str = "fedcba9876543210fedcba9876543210fedcba98";
const SKILL: &str = "---\r\nname: tidy\r\ndescription: Tidy notes\r\n---\r\nKeep notes short.\r\n";
const MIT: &str = "MIT";

fn allow() -> [String; 1] {
    [MIT.into()]
}

fn fixture(commit: &str, folder: &str, skill: &str) -> Recorded {
    let license: Value = serde_json::from_str(include_str!(
        "../../../conformance/v0.4.0/review/record-mit-reviewed/input.json"
    ))
    .unwrap();
    Recorded::new(REPO, commit)
        .file(
            "LICENSE",
            license["upstream"]["files"]["LICENSE"].as_str().unwrap(),
        )
        .file(&format!("{folder}/SKILL.md"), skill)
}

fn save(store: &Store, repo: &str, folder: &str, fixture: &Recorded) -> ReviewOutcome {
    let transport = RecordingTransport::new();
    fixture.script(&transport);
    let request = ReviewRequest {
        repo,
        path: folder,
        candidate: &Map::new(),
        allow: &allow(),
        now: UNIX_EPOCH,
    };
    review_and_store(
        store,
        &GitHub::new(&transport, None),
        &request,
        &AgentId::ALL,
    )
    .expect("store a real collected review and snapshot")
}

fn approval(review_id: &str, acknowledge_warnings: bool) -> ReviewApproval<'_> {
    ReviewApproval {
        review_id,
        acknowledge_warnings,
    }
}

fn reseal_record(store: &Store, mut record: ReviewedSkillV1) -> String {
    record.id = record.content_id();
    let hex = record.id.strip_prefix("sha256:").unwrap();
    fs::write(
        store.reviews_dir().join(format!("{hex}.json")),
        serde_json::to_vec(&record).unwrap(),
    )
    .unwrap();
    assert_eq!(store.review(&record.id).unwrap(), Some(record.clone()));
    record.id
}

fn reseal_snapshot(store: &Store, manifest: &Value) -> String {
    let mut bytes = Vec::new();
    let formatter = serde_json::ser::PrettyFormatter::with_indent(b" ");
    manifest
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes, formatter,
        ))
        .unwrap();
    bytes.push(b'\n');
    let hex: String = Sha256::digest(&bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    fs::write(store.snapshots_dir().join(format!("{hex}.json")), bytes).unwrap();
    format!("sha256:{hex}")
}

#[test]
fn admits_a_complete_inventory_from_the_manifest_despite_the_trimmed_record() {
    let state = tempfile::tempdir().unwrap();
    let store = Store::new(state.path());
    let binary = [0x00, 0xff, 0xc0, 0xfe];
    let mut fixture = fixture(COMMIT, FOLDER, SKILL)
        .file("skills/tidy/assets/raw.bin", binary)
        .file("skills/tidy-sibling/keep.md", "a different folder");
    let mut expected = BTreeMap::from([
        ("SKILL.md".into(), SKILL.as_bytes().to_vec()),
        ("assets/raw.bin".into(), binary.to_vec()),
    ]);
    for index in 0..257 {
        let relative = format!("references/note-{index:03}.txt");
        let data = format!("Note {index}\n").into_bytes();
        fixture = fixture.file(&format!("{FOLDER}/{relative}"), data.clone());
        expected.insert(relative, data);
    }
    let saved = save(&store, "ACME/Skills.Git", FOLDER, &fixture);
    assert_eq!(saved.gate.result, GateResult::Pass);
    assert_eq!(saved.record.source.files.len(), 256);

    let payload = authorize(
        &store,
        &allow(),
        AgentId::Codex,
        approval(&saved.record.id, false),
    )
    .unwrap();
    assert_eq!(payload.review(), &saved.record);
    assert_eq!(payload.snapshot().id, saved.record.source.snapshot);
    assert_eq!(
        (payload.source().repo(), payload.source().path()),
        (REPO, FOLDER)
    );
    assert_eq!((payload.target(), payload.name()), (AgentId::Codex, "tidy"));
    let actual: BTreeMap<_, _> = payload.files().collect();
    let expected: BTreeMap<_, _> = expected
        .iter()
        .map(|(path, data)| (path.as_str(), data.as_slice()))
        .collect();
    assert_eq!(actual, expected);
}

#[test]
fn compares_two_stored_reviews_by_exact_source_across_commits() {
    let state = tempfile::tempdir().unwrap();
    let store = Store::new(state.path());
    let old = save(
        &store,
        "ACME/Skills.git",
        FOLDER,
        &fixture(COMMIT, FOLDER, SKILL),
    );
    let old = store.review(&old.record.id).unwrap().unwrap();
    let old_source = GitHubSkillSource::from_record(&old).unwrap();

    let revised_skill = SKILL.replace("Keep notes short", "Keep notes clear");
    let next = save(
        &store,
        REPO,
        FOLDER,
        &fixture(NEXT_COMMIT, FOLDER, &revised_skill),
    );
    let next = authorize(
        &store,
        &allow(),
        AgentId::Claude,
        approval(&next.record.id, false),
    )
    .unwrap();
    assert_ne!(old.source.snapshot, next.snapshot().id);
    assert_eq!(old_source, *next.source());

    let other_folder = "Skills/tidy";
    let other = save(
        &store,
        REPO,
        other_folder,
        &fixture(NEXT_COMMIT, other_folder, SKILL),
    );
    let other = authorize(
        &store,
        &allow(),
        AgentId::Claude,
        approval(&other.record.id, false),
    )
    .unwrap();
    assert_ne!(next.source(), other.source());
}

#[test]
fn reapplies_current_license_policy_and_refuses_quarantined_reviews() {
    let state = tempfile::tempdir().unwrap();
    let store = Store::new(state.path());
    let saved = save(&store, REPO, FOLDER, &fixture(COMMIT, FOLDER, SKILL));
    assert_eq!(saved.gate.result, GateResult::Pass);
    let marker = format!("{SKILL}\n<!-- oah fixture -->\n");
    let blocked = save(&store, REPO, FOLDER, &fixture(NEXT_COMMIT, FOLDER, &marker));
    assert_eq!(blocked.gate.result, GateResult::Block);
    for (id, policy, expected_reason) in [
        (
            saved.record.id,
            vec![MIT.into(), "Apache-2.0".into()],
            ReasonKind::License,
        ),
        (
            blocked.record.id,
            allow().to_vec(),
            ReasonKind::MarkerCollision,
        ),
    ] {
        match authorize(&store, &policy, AgentId::Claude, approval(&id, true)) {
            Err(DeliveryError::Blocked(decision)) => assert!(
                decision
                    .reasons
                    .iter()
                    .any(|reason| reason.kind == expected_reason)
            ),
            _ => panic!("expected the current gate to block {expected_reason:?}"),
        }
    }
}

#[test]
fn a_warn_review_requires_an_acknowledgement_for_its_named_id() {
    let state = tempfile::tempdir().unwrap();
    let store = Store::new(state.path());
    let fixture = fixture(COMMIT, FOLDER, SKILL)
        .executable("skills/tidy/scripts/inert.txt", "Read me as data.\n");
    let saved = save(&store, REPO, FOLDER, &fixture);
    assert_eq!(saved.gate.result, GateResult::Warn);

    let refusal = authorize(
        &store,
        &allow(),
        AgentId::Codex,
        approval(&saved.record.id, false),
    )
    .err()
    .expect("approval refusal");
    let error = Error::from(refusal);
    assert!(matches!(
        error,
        Error::SkillDelivery(DeliveryError::ApprovalRequired)
    ));
    assert_eq!(error.code(), codes::SKILL_APPROVAL_REQUIRED);
    let payload = authorize(
        &store,
        &allow(),
        AgentId::Codex,
        approval(&saved.record.id, true),
    )
    .unwrap();
    assert_eq!(payload.review().id, saved.record.id);
    assert!(
        payload
            .files()
            .any(|(path, data)| path == "scripts/inert.txt" && data == b"Read me as data.\n")
    );
    assert!(matches!(
        authorize(
            &store,
            &allow(),
            AgentId::Codex,
            approval("../different-review", true)
        ),
        Err(DeliveryError::ReviewMissing)
    ));
}

#[test]
fn requires_a_supported_and_compatible_target() {
    let state = tempfile::tempdir().unwrap();
    let store = Store::new(state.path());
    let skill = SKILL.replace("description:", "allowed-tools: TodoWrite\r\ndescription:");
    let saved = save(&store, REPO, FOLDER, &fixture(COMMIT, FOLDER, &skill));
    assert_eq!(saved.gate.result, GateResult::Pass);
    let approve = approval(&saved.record.id, false);
    assert!(matches!(
        authorize(&store, &allow(), AgentId::Codex, approve),
        Err(DeliveryError::IncompatibleTarget(AgentId::Codex))
    ));
    assert!(matches!(
        authorize(&store, &allow(), AgentId::Gemini, approve),
        Err(DeliveryError::UnsupportedTarget(AgentId::Gemini))
    ));
    assert!(authorize(&store, &allow(), AgentId::Claude, approve).is_ok());
}

#[test]
fn rejects_records_with_valid_digests_but_mismatched_snapshot_or_source() {
    let state = tempfile::tempdir().unwrap();
    let store = Store::new(state.path());
    let saved = save(&store, REPO, FOLDER, &fixture(COMMIT, FOLDER, SKILL));
    let other_folder = "other/tidy";
    let other = save(
        &store,
        REPO,
        other_folder,
        &fixture(NEXT_COMMIT, other_folder, SKILL),
    );
    let edits: [fn(&mut ReviewedSkillV1); 5] = [
        |record| record.source.provider = "local".into(),
        |record| record.source.source_id.push('a'),
        |record| record.source.commit = Some(NEXT_COMMIT.into()),
        |record| record.source.tree = Some(NEXT_COMMIT.into()),
        |record| record.frontmatter.name = Some("different-name".into()),
    ];
    let mut mismatches = Vec::new();
    for edit in edits {
        let mut forged = saved.record.clone();
        edit(&mut forged);
        mismatches.push(forged);
    }
    let mut swapped = saved.record;
    swapped.source.snapshot = other.record.source.snapshot;
    mismatches.push(swapped);
    for mismatch in mismatches {
        let id = reseal_record(&store, mismatch);
        assert!(matches!(
            authorize(&store, &allow(), AgentId::Claude, approval(&id, true)),
            Err(DeliveryError::InvalidReview(_))
        ));
    }
}

#[test]
fn refuses_manifest_path_escape_and_a_missing_root_skill() {
    let state = tempfile::tempdir().unwrap();
    let store = Store::new(state.path());
    let fixture = fixture(COMMIT, FOLDER, SKILL).file("skills/tidy/notes.txt", "Notes.\n");
    let saved = save(&store, REPO, FOLDER, &fixture);
    let snapshot = store
        .snapshot(&saved.record.source.snapshot)
        .unwrap()
        .unwrap();
    for (index, path) in [
        (1, "skills/tidy/../outside.txt"),
        (0, "skills/tidy/references/SKILL.md"),
    ] {
        let mut manifest = snapshot.manifest.clone();
        manifest["files"][index]["path"] = json!(path);
        let mut record = saved.record.clone();
        record.source.snapshot = reseal_snapshot(&store, &manifest);
        record.source.files[index].path = path.into();
        let id = reseal_record(&store, record);
        assert!(
            matches!(
                authorize(&store, &allow(), AgentId::Claude, approval(&id, true)),
                Err(DeliveryError::InvalidReview(_))
            ),
            "admission should refuse manifest at {path}"
        );
    }
}

#[test]
fn rejects_a_review_whose_confirmed_name_cannot_be_a_visible_folder() {
    let state = tempfile::tempdir().unwrap();
    let store = Store::new(state.path());
    for name in ["../outside", "nul.txt", ".hidden"] {
        let skill = SKILL.replace("name: tidy", &format!("name: {name}"));
        let saved = save(&store, REPO, FOLDER, &fixture(COMMIT, FOLDER, &skill));
        assert_eq!(saved.gate.result, GateResult::Pass);
        assert!(matches!(
            authorize(
                &store,
                &allow(),
                AgentId::Claude,
                approval(&saved.record.id, false)
            ),
            Err(DeliveryError::InvalidReview(_))
        ));
    }
}

#[test]
fn missing_a_selected_blob_returns_the_typed_store_failure() {
    let state = tempfile::tempdir().unwrap();
    let store = Store::new(state.path());
    let fixture =
        fixture(COMMIT, FOLDER, SKILL).file("skills/tidy/reference.txt", "Necessary bytes.");
    let saved = save(&store, REPO, FOLDER, &fixture);
    let sha = saved
        .record
        .source
        .files
        .iter()
        .find(|file| file.path.ends_with("reference.txt"))
        .unwrap()
        .sha256
        .strip_prefix("sha256:")
        .unwrap();
    fs::remove_file(store.snapshots_dir().join("blobs").join(sha)).unwrap();

    let error = authorize(
        &store,
        &allow(),
        AgentId::Claude,
        approval(&saved.record.id, true),
    )
    .err()
    .expect("missing selected data");
    assert!(matches!(
        Error::from(error),
        Error::ReviewStore(StoreError::Corrupt { .. })
    ));
}
