// 검토 저장소: 검토 기록과 그 스냅숏(매니페스트·받은 바이트)을 OAH_HOME/state 아래 내용 주소 파일로 한 번만 쓰고, 같은 검토는 unchanged 로 돌려준다.
//! The review store and the review operation over it (CONTRACTS C9, DESIGN-C2 §5.2 checks 2 and 14).
//!
//! Layout under `OAH_HOME/state`, written only by this module:
//!
//! * `reviews/<hex>.json`: one `ReviewedSkillV1` in its stored form, named by its record id (`sha256:<hex>`).
//! * `snapshots/<hex>.json`: the manifest of a snapshot in its stored form, named by the snapshot id, which is the
//!   SHA-256 of exactly these bytes.
//! * `snapshots/blobs/<hex>`: each fetched byte string the snapshot keeps, named by its SHA-256.
//!
//! Every file is content-addressed and created once through `fsx::publish_new`. A file that is already there must
//! hold the same bytes; anything else is `OAH_SKILL_STATE_CORRUPT`, which OAH never repairs or resets. Reading
//! checks every record against its own digest, every manifest and blob against its name.
//!
//! [`review_and_store`] is v0.4.0's `review.review` over a collector, in two steps a caller may separate (to stop
//! between them on a cancellation): [`prepare`] checks the identity and collects the record and its snapshot,
//! writing nothing; [`commit`] answers `unchanged` for a record equal to a stored one apart from `id` and
//! `checked_at`, otherwise writes, then runs the install gate. The stored records are the identity memory: a
//! stored record with the same `source_id` and another spec is a truncated identity collision, refused before
//! anything is fetched and again before anything is written. A review that stored nothing has no identity to protect, so no separate identity
//! table is kept. Writers hold the store's `fsx::FileLock` (`state/reviews.lock`) from that second check to the
//! last write and write blobs, then the manifest, then the record, so a stored record always names a whole
//! snapshot. Readers never lock. A blocked review is stored too: quarantine means nothing reaches a skill store
//! or an agent folder, and the snapshot stays here as data.

use super::gate::{GateDecision, GatePolicy, gate};
use super::identity::{SourceSpec, identity};
use super::json::{record_bytes, sha256_hex};
use super::record::{ReviewRequest, ReviewedSkillV1, digest_record_fields, review};
use super::snapshot::{BLOB_LIMIT, Collector, Observation, Snapshot, is_sha256_hex};
use super::{FailReason, ReviewError};
use crate::config::AgentId;
use crate::error::{Coded, codes};
use crate::fsx::{RegularOpenError, open_regular};
use oah_core::fsx::{FileLock, FsxError, FsxErrorKind, LockOptions, publish_new};
use serde_json::Value;
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fmt;
use std::io::{self, Read};
use std::path::{Path, PathBuf};

/// The largest stored record read back (v0.4.0's state file limit, 1 MiB).
const RECORD_LIMIT: u64 = 1 << 20;
/// The largest stored snapshot manifest read back.
const MANIFEST_LIMIT: u64 = 4 << 20;

/// The result of one review (CONTRACTS C9): the record, the gate's decision for it, and whether an equal record
/// was already stored, in which case `record` is that stored one.
#[derive(Debug, Clone, PartialEq)]
pub struct ReviewOutcome {
    pub record: ReviewedSkillV1,
    pub gate: GateDecision,
    pub unchanged: bool,
}

/// A store file OAH cannot use, or a file operation that failed.
#[derive(Debug)]
pub enum StoreError {
    /// `OAH_SKILL_STATE_CORRUPT`: a store file that does not read as what its name says; never repaired.
    Corrupt { path: PathBuf, detail: String },
    /// Incoming review data that exceeds the store's size limit before publication.
    WriteTooLarge { path: PathBuf, limit: u64 },
    /// Listing, inspecting or reading a store file failed (`OAH_FILE_IO`).
    Io { path: PathBuf, detail: String },
    /// Creating a folder, a file or the lock failed.
    File(FsxError),
}

impl Coded for StoreError {
    fn code(&self) -> &'static str {
        match self {
            StoreError::Corrupt { .. } | StoreError::WriteTooLarge { .. } => {
                codes::SKILL_STATE_CORRUPT
            }
            StoreError::Io { .. } => FsxErrorKind::Io.code(),
            StoreError::File(error) => error.code(),
        }
    }

    fn message(&self) -> String {
        match self {
            StoreError::Corrupt { path, detail } => format!(
                "{}: {detail}; oah never repairs or resets it. Move the file aside to start that part again",
                path.display()
            ),
            StoreError::WriteTooLarge { path, limit } => format!(
                "Review data exceeds the {limit}-byte storage limit for {}. No files were published.",
                path.display()
            ),
            StoreError::Io { path, detail } => format!("{detail} ({})", path.display()),
            StoreError::File(error) => {
                let text = error.to_string();
                text.strip_prefix(error.code())
                    .and_then(|rest| rest.strip_prefix(": "))
                    .unwrap_or(&text)
                    .to_string()
            }
        }
    }
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code(), self.message())
    }
}

impl std::error::Error for StoreError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            StoreError::File(error) => Some(error),
            StoreError::Corrupt { .. }
            | StoreError::WriteTooLarge { .. }
            | StoreError::Io { .. } => None,
        }
    }
}

/// Why [`review_and_store`] has no outcome: the review's own refusal or failure, or the store's.
#[derive(Debug)]
pub enum Failure {
    Review(ReviewError),
    Store(StoreError),
}

impl From<StoreError> for Failure {
    fn from(error: StoreError) -> Failure {
        Failure::Store(error)
    }
}

fn corrupt(path: &Path, detail: impl Into<String>) -> StoreError {
    StoreError::Corrupt {
        path: path.to_path_buf(),
        detail: detail.into(),
    }
}

fn io_error(path: &Path, what: &str, error: &io::Error) -> StoreError {
    StoreError::Io {
        path: path.to_path_buf(),
        detail: format!("{what}: {error}"),
    }
}

/// The review store of one OAH home.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Store {
    reviews: PathBuf,
    snapshots: PathBuf,
}

impl Store {
    /// The store under `state` (`OAH_HOME/state`). Nothing is created until a review is stored.
    pub fn new(state: &Path) -> Store {
        Store {
            reviews: state.join("reviews"),
            snapshots: state.join("snapshots"),
        }
    }

    /// `state/reviews`.
    pub fn reviews_dir(&self) -> &Path {
        &self.reviews
    }

    /// `state/snapshots`.
    pub fn snapshots_dir(&self) -> &Path {
        &self.snapshots
    }

    /// Every stored review, each checked against its own digest, in id order. Other files are not reviews.
    pub fn reviews(&self) -> Result<Vec<ReviewedSkillV1>, StoreError> {
        let listing = match std::fs::read_dir(&self.reviews) {
            Ok(listing) => listing,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(io_error(&self.reviews, "cannot list the reviews", &error)),
        };
        let mut names = Vec::new();
        for entry in listing {
            let entry = entry
                .map_err(|error| io_error(&self.reviews, "cannot list the reviews", &error))?;
            let name = entry.file_name();
            if let Some(hex) = name
                .to_str()
                .and_then(|name| name.strip_suffix(".json"))
                .filter(|hex| is_sha256_hex(hex))
            {
                names.push(hex.to_string());
            }
        }
        names.sort();
        let mut records = Vec::with_capacity(names.len());
        for hex in names {
            records.extend(self.read_review(&hex)?);
        }
        Ok(records)
    }

    /// The stored review with `id` (`sha256:<hex>`), checked against its digest; `None` when there is none.
    pub fn review(&self, id: &str) -> Result<Option<ReviewedSkillV1>, StoreError> {
        match store_id_hex(id) {
            Some(hex) => self.read_review(hex),
            None => Ok(None),
        }
    }

    /// The stored snapshot with `id`: its manifest and every blob the manifest names, each checked against its
    /// name; `None` when there is no manifest under that id.
    pub fn snapshot(&self, id: &str) -> Result<Option<Snapshot>, StoreError> {
        let Some(hex) = store_id_hex(id) else {
            return Ok(None);
        };
        let path = self.snapshots.join(format!("{hex}.json"));
        let Some(bytes) = read_file(&path, MANIFEST_LIMIT)? else {
            return Ok(None);
        };
        if sha256_hex(&bytes) != hex {
            return Err(corrupt(
                &path,
                "does not match its own digest (changed after it was stored)",
            ));
        }
        let manifest: Value = serde_json::from_slice(&bytes)
            .map_err(|_| corrupt(&path, "is not a snapshot manifest"))?;
        let mut names = Vec::new();
        blob_names(&manifest, &mut names);
        let mut blobs = BTreeMap::new();
        for name in names {
            if blobs.contains_key(&name) {
                continue;
            }
            let blob = self.snapshots.join("blobs").join(&name);
            let data = read_file(&blob, BLOB_LIMIT)?
                .ok_or_else(|| corrupt(&path, format!("names blob {name}, which is not stored")))?;
            if sha256_hex(&data) != name {
                return Err(corrupt(
                    &blob,
                    "does not match its own digest (changed after it was stored)",
                ));
            }
            blobs.insert(name, data);
        }
        Ok(Some(Snapshot {
            id: id.to_string(),
            manifest,
            blobs,
        }))
    }

    fn read_review(&self, hex: &str) -> Result<Option<ReviewedSkillV1>, StoreError> {
        let path = self.reviews.join(format!("{hex}.json"));
        let Some(bytes) = read_file(&path, RECORD_LIMIT)? else {
            return Ok(None);
        };
        let invalid_record = |_| corrupt(&path, "is not a review record");
        let record: ReviewedSkillV1 = serde_json::from_slice(&bytes).map_err(invalid_record)?;
        let stored: Value = serde_json::from_slice(&bytes).map_err(invalid_record)?;
        let encoded = record.to_json();
        if !matches_record_shape(&stored, &encoded) {
            return Err(corrupt(&path, "contains unsupported review record fields"));
        }
        if store_id_hex(&record.id) != Some(hex) || digest_record_fields(encoded) != record.id {
            return Err(corrupt(
                &path,
                "does not match its own digest (changed after it was stored)",
            ));
        }
        Ok(Some(record))
    }

    /// The writer lock of the store, taken after its folders exist.
    fn lock(&self) -> Result<FileLock, StoreError> {
        create_dir(&self.reviews)?;
        create_dir(&self.snapshots.join("blobs"))?;
        FileLock::acquire(&self.reviews, LockOptions::default()).map_err(StoreError::File)
    }

    fn put_snapshot(&self, snapshot: &Snapshot) -> Result<(), StoreError> {
        let blobs = self.snapshots.join("blobs");
        let mut blob_writes = Vec::with_capacity(snapshot.blobs.len());
        for (name, data) in &snapshot.blobs {
            let path = blobs.join(name);
            if !is_sha256_hex(name) || sha256_hex(data) != *name {
                return Err(corrupt(&path, "the collected blob does not match its name"));
            }
            blob_writes.push(PendingWrite::new(path, data, BLOB_LIMIT)?);
        }
        let Some(hex) = store_id_hex(&snapshot.id) else {
            return Err(corrupt(
                &self.snapshots,
                "the collected manifest does not match its id",
            ));
        };
        let path = self.snapshots.join(format!("{hex}.json"));
        let bytes = record_bytes(&snapshot.manifest);
        if sha256_hex(bytes.as_bytes()) != hex {
            return Err(corrupt(
                &path,
                "the collected manifest does not match its id",
            ));
        }
        let manifest = PendingWrite::new(path, bytes.as_bytes(), MANIFEST_LIMIT)?;
        for blob in blob_writes {
            blob.publish()?;
        }
        manifest.publish()
    }

    fn put_new_review(
        &self,
        record: &ReviewedSkillV1,
        snapshot: &Snapshot,
    ) -> Result<(), StoreError> {
        let Some(hex) = store_id_hex(&record.id) else {
            return Err(corrupt(&self.reviews, "the record does not match its id"));
        };
        let path = self.reviews.join(format!("{hex}.json"));
        if record.content_id() != record.id {
            return Err(corrupt(&path, "the record does not match its id"));
        }
        let bytes = record_bytes(&record.to_json());
        let review = PendingWrite::new(path, bytes.as_bytes(), RECORD_LIMIT)?;
        self.put_snapshot(snapshot)?;
        review.publish()
    }
}

fn store_id_hex(id: &str) -> Option<&str> {
    id.strip_prefix("sha256:").filter(|hex| is_sha256_hex(hex))
}

/// Compare the fields present on disk so that omitted optional fields retain their decoded defaults.
fn matches_record_shape(stored: &Value, encoded: &Value) -> bool {
    match (stored, encoded) {
        (Value::Object(stored), Value::Object(encoded)) => stored.iter().all(|(key, value)| {
            encoded
                .get(key)
                .is_some_and(|field| matches_record_shape(value, field))
        }),
        (Value::Array(stored), Value::Array(encoded)) => {
            stored.len() == encoded.len()
                && stored
                    .iter()
                    .zip(encoded)
                    .all(|(value, field)| matches_record_shape(value, field))
        }
        (stored, encoded) => stored == encoded,
    }
}

/// Every SHA-256 the manifest names under a `sha256` key: the files and the license evidence it keeps.
fn blob_names(value: &Value, out: &mut Vec<String>) {
    match value {
        Value::Object(map) => {
            for (key, item) in map {
                match item {
                    Value::String(text) if key == "sha256" && is_sha256_hex(text) => {
                        out.push(text.clone());
                    }
                    _ => blob_names(item, out),
                }
            }
        }
        Value::Array(items) => items.iter().for_each(|item| blob_names(item, out)),
        _ => {}
    }
}

fn create_dir(path: &Path) -> Result<(), StoreError> {
    std::fs::create_dir_all(path)
        .map_err(|error| io_error(path, "cannot create the folder", &error))
}

fn check_size(path: &Path, size: u64, limit: u64) -> Result<(), StoreError> {
    if size > limit {
        return Err(corrupt(path, format!("is larger than {limit} bytes")));
    }
    Ok(())
}

/// A regular file of at most `limit` bytes; `None` when it does not exist. A link or a larger file is corrupt.
fn read_file(path: &Path, limit: u64) -> Result<Option<Vec<u8>>, StoreError> {
    let (file, metadata) = match open_regular(path) {
        Ok(opened) => opened,
        Err(RegularOpenError::Inspect(error)) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(None);
        }
        Err(RegularOpenError::Inspect(error) | RegularOpenError::Open(error))
            if error.kind() == io::ErrorKind::InvalidData =>
        {
            return Err(corrupt(path, "is not a regular file"));
        }
        Err(RegularOpenError::Inspect(error)) => {
            return Err(io_error(path, "cannot inspect the file", &error));
        }
        Err(RegularOpenError::Open(error)) => {
            return Err(io_error(path, "cannot open the file", &error));
        }
    };
    check_size(path, metadata.len(), limit)?;
    let mut bytes = Vec::new();
    file.take(limit.saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|error| io_error(path, "cannot read the file", &error))?;
    check_size(path, u64::try_from(bytes.len()).unwrap_or(u64::MAX), limit)?;
    Ok(Some(bytes))
}

struct PendingWrite<'a> {
    path: PathBuf,
    bytes: &'a [u8],
    limit: u64,
}

impl<'a> PendingWrite<'a> {
    fn new(path: PathBuf, bytes: &'a [u8], limit: u64) -> Result<Self, StoreError> {
        if u64::try_from(bytes.len()).unwrap_or(u64::MAX) > limit {
            return Err(StoreError::WriteTooLarge { path, limit });
        }
        Ok(Self { path, bytes, limit })
    }

    fn publish(self) -> Result<(), StoreError> {
        let Self { path, bytes, limit } = self;
        match publish_new(&path, bytes) {
            Ok(_) => Ok(()),
            Err(error) if error.kind() == FsxErrorKind::Exists => match read_file(&path, limit)? {
                Some(existing) if existing == bytes => Ok(()),
                _ => Err(corrupt(
                    &path,
                    "already holds different bytes under the same name",
                )),
            },
            Err(error) => Err(StoreError::File(error)),
        }
    }
}

/// Passes observations through and keeps the verified snapshot, which [`review`] reads but does not return.
struct Keeping<'a> {
    inner: &'a dyn Collector,
    kept: RefCell<Option<Snapshot>>,
}

impl Collector for Keeping<'_> {
    fn observe(&self, spec: &SourceSpec) -> Observation {
        let observation = self.inner.observe(spec);
        if let Observation::Snapshot(snapshot) = &observation {
            *self.kept.borrow_mut() = Some(snapshot.clone());
        }
        observation
    }
}

/// A stored record with the same `source_id` and another spec: a truncated identity collision (§5.2 check 2).
fn refuse_collision(
    stored: &[ReviewedSkillV1],
    source_id: &str,
    spec_digest: &str,
) -> Result<(), Failure> {
    let collides = stored.iter().any(|record| {
        record.source.source_id == source_id && record.source.spec_digest != spec_digest
    });
    if collides {
        return Err(Failure::Review(ReviewError::Failed {
            reason: FailReason::IdentityCollision,
            detail: format!(
                "{source_id} already names a different skill source (a truncated identity collision); oah \
                 refuses instead of overwriting it"
            ),
        }));
    }
    Ok(())
}

/// Equal apart from `id` and `checked_at` (v0.4.0's `unchanged` comparison).
fn same_review(stored: &ReviewedSkillV1, record: &ReviewedSkillV1) -> bool {
    let blank = |record: &ReviewedSkillV1| ReviewedSkillV1 {
        id: String::new(),
        checked_at: String::new(),
        ..record.clone()
    };
    blank(stored) == blank(record)
}

/// A collected review, not yet stored: the record and the verified snapshot it names.
#[derive(Debug, Clone, PartialEq)]
pub struct Prepared {
    record: ReviewedSkillV1,
    snapshot: Snapshot,
    allow: Vec<String>,
}

impl Prepared {
    /// The record the review produced.
    pub fn record(&self) -> &ReviewedSkillV1 {
        &self.record
    }
}

/// The first step: checks the identity against the stored reviews, then collects the record and its snapshot
/// through `collector`. Nothing is written.
pub fn prepare(
    store: &Store,
    collector: &dyn Collector,
    request: &ReviewRequest<'_>,
) -> Result<Prepared, Failure> {
    let (identity, spec) = identity(request.repo, request.path).map_err(Failure::Review)?;
    refuse_collision(&store.reviews()?, &identity.source_id, &spec.spec_digest())?;
    let keeping = Keeping {
        inner: collector,
        kept: RefCell::new(None),
    };
    let record = review(&keeping, request).map_err(Failure::Review)?;
    let snapshot = keeping
        .kept
        .into_inner()
        .filter(|snapshot| snapshot.id == record.source.snapshot)
        .ok_or_else(|| {
            Failure::Review(ReviewError::Failed {
                reason: FailReason::Collector,
                detail: format!(
                    "the collector could not verify {} (failed: bad-response)",
                    request.repo
                ),
            })
        })?;
    Ok(Prepared {
        record,
        snapshot,
        allow: request.allow.to_vec(),
    })
}

/// The second step: under the store's lock, checks the identity again, writes the snapshot's blobs and its manifest
/// where they are missing, then answers `unchanged` for a record equal to a stored one or writes the record. The
/// gate then decides with the request's allowed licenses and offers the skill to `agents`.
pub fn commit(
    store: &Store,
    prepared: Prepared,
    agents: &[AgentId],
) -> Result<ReviewOutcome, Failure> {
    let Prepared {
        record,
        snapshot,
        allow,
    } = prepared;
    let lock = store.lock()?;
    let stored = (|| -> Result<(ReviewedSkillV1, bool), Failure> {
        let stored = store.reviews()?;
        refuse_collision(
            &stored,
            &record.source.source_id,
            &record.source.spec_digest,
        )?;
        let existing = stored
            .into_iter()
            .find(|stored| same_review(stored, &record));
        match existing {
            Some(existing) => {
                store.put_snapshot(&snapshot)?;
                Ok((existing, true))
            }
            None => {
                store.put_new_review(&record, &snapshot)?;
                Ok((record, false))
            }
        }
    })();
    let released = lock.release().map_err(StoreError::File);
    let (record, unchanged) = stored?;
    released?;
    let policy = GatePolicy {
        agents: agents.to_vec(),
        license_allow: allow,
    };
    Ok(ReviewOutcome {
        gate: gate(&record, &policy),
        record,
        unchanged,
    })
}

/// [`prepare`], then [`commit`]. Nothing is written when the review refuses or fails. An `unchanged` review returns
/// the stored record and writes no record; it only puts back snapshot files that were moved aside.
pub fn review_and_store(
    store: &Store,
    collector: &dyn Collector,
    request: &ReviewRequest<'_>,
    agents: &[AgentId],
) -> Result<ReviewOutcome, Failure> {
    commit(store, prepare(store, collector, request)?, agents)
}

#[cfg(test)]
mod tests {
    use super::{
        BLOB_LIMIT, MANIFEST_LIMIT, ReviewedSkillV1, Snapshot, Store, StoreError, read_file,
        record_bytes, sha256_hex,
    };
    use crate::error::{Coded, codes};
    use serde_json::{Value, json};
    use std::collections::BTreeMap;
    use std::fs;

    fn review_fixture() -> ReviewedSkillV1 {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../conformance/v0.4.0/review/record-mit-reviewed/expect.json"
        ))
        .expect("frozen review fixture");
        serde_json::from_value(fixture["ok"].clone()).expect("v1 record")
    }

    fn write_review_fixture(store: &Store, id: &str, fixture: &Value) {
        let hex = id.strip_prefix("sha256:").expect("canonical fixture id");
        fs::create_dir_all(store.reviews_dir()).expect("reviews directory");
        let path = store.reviews_dir().join(format!("{hex}.json"));
        fs::write(path, record_bytes(fixture)).expect("review fixture");
    }

    #[test]
    fn stored_v1_rejects_unsupported_fields_in_records_and_nested_entries() {
        let state = tempfile::tempdir().expect("state");
        let store = Store::new(state.path());
        let record = review_fixture();
        write_review_fixture(&store, &record.id, &record.to_json());
        assert_eq!(store.review(&record.id).unwrap().as_ref(), Some(&record));

        for object in ["", "/source", "/source/files/0"] {
            let mut fixture = record.to_json();
            fixture
                .pointer_mut(object)
                .and_then(Value::as_object_mut)
                .expect("fixture object")
                .insert("comment".to_string(), json!("sample note"));
            write_review_fixture(&store, &record.id, &fixture);

            let error = store.review(&record.id).expect_err("unsupported field");
            assert_eq!(error.code(), codes::SKILL_STATE_CORRUPT, "{object}");
            assert!(error.message().contains("unsupported review record fields"));
        }
    }

    #[test]
    fn stored_v1_accepts_omitted_optional_fields_and_nested_candidate_data() {
        let state = tempfile::tempdir().expect("state");
        let store = Store::new(state.path());
        let mut record = review_fixture();
        record.candidate.hit = json!({"details": [{"label": "example", "score": 0.5}]});
        record.id = record.content_id();
        let mut fixture = record.to_json();
        fixture.as_object_mut().unwrap().remove("reason");
        fixture["frontmatter"]
            .as_object_mut()
            .unwrap()
            .remove("allowed_tools");
        write_review_fixture(&store, &record.id, &fixture);

        assert_eq!(store.review(&record.id).unwrap(), Some(record));
    }

    #[test]
    fn bounded_store_reads_distinguish_absence_regular_files_and_corruption() {
        let state = tempfile::tempdir().expect("state");
        let path = state.path().join("data");
        assert_eq!(read_file(&path, 3).unwrap(), None);
        fs::write(&path, b"abc").unwrap();

        assert_eq!(
            read_file(&path, 3).unwrap().as_deref(),
            Some(b"abc".as_slice())
        );
        assert!(matches!(
            read_file(&path, 2),
            Err(StoreError::Corrupt { path: stored, detail })
                if stored == path && detail.contains("larger than 2 bytes")
        ));
        assert!(matches!(
            read_file(state.path(), 3),
            Err(StoreError::Corrupt { detail, .. }) if detail.contains("is not a regular file")
        ));
    }

    #[test]
    fn publishing_requires_one_sha256_prefix_for_each_id() {
        let state = tempfile::tempdir().expect("state");
        let store = Store::new(state.path());
        let _lock = store.lock().expect("store lock");
        let manifest = json!({});
        let manifest_hex = sha256_hex(record_bytes(&manifest).as_bytes());
        let mut snapshot = Snapshot {
            id: format!("sha256:{manifest_hex}"),
            manifest,
            blobs: BTreeMap::new(),
        };
        let canonical_snapshot = snapshot.id.clone();
        let mut record = review_fixture();
        record.source.snapshot = canonical_snapshot.clone();
        let canonical_record = record.content_id();
        let record_hex = canonical_record.strip_prefix("sha256:").unwrap();

        for prefix in ["", "sha256:sha256:"] {
            snapshot.id = format!("{prefix}{manifest_hex}");
            assert!(matches!(
                store.put_snapshot(&snapshot),
                Err(StoreError::Corrupt { .. })
            ));
            snapshot.id.clone_from(&canonical_snapshot);
            record.id = format!("{prefix}{record_hex}");
            assert!(matches!(
                store.put_new_review(&record, &snapshot),
                Err(StoreError::Corrupt { .. })
            ));
        }
        assert!(store.reviews().unwrap().is_empty());
        assert!(store.snapshot(&canonical_snapshot).unwrap().is_none());

        record.id = canonical_record;
        store.put_new_review(&record, &snapshot).unwrap();
        assert_eq!(store.review(&record.id).unwrap().as_ref(), Some(&record));
        assert_eq!(store.snapshot(&snapshot.id).unwrap(), Some(snapshot));
    }

    #[test]
    fn an_oversized_snapshot_file_leaves_no_files() {
        let blob = b"x".to_vec();
        let oversized = vec![0; usize::try_from(BLOB_LIMIT).expect("blob limit") + 1];
        let (blob_name, oversized_name) = (sha256_hex(&blob), sha256_hex(&oversized));
        assert!(blob_name < oversized_name, "the bounded blob sorts first");
        let blobs = BTreeMap::from([(blob_name, blob)]);
        let mut oversized_blobs = blobs.clone();
        oversized_blobs.insert(oversized_name, oversized);
        let cases = [
            (
                json!({"padding": " ".repeat(usize::try_from(MANIFEST_LIMIT).expect("manifest limit"))}),
                blobs,
            ),
            (json!({}), oversized_blobs),
        ];
        for (manifest, blobs) in cases {
            let state = tempfile::tempdir().expect("state");
            let store = Store::new(state.path());
            let _lock = store.lock().expect("store lock");
            let id = format!("sha256:{}", sha256_hex(record_bytes(&manifest).as_bytes()));
            let snapshot = Snapshot {
                id,
                manifest,
                blobs,
            };

            let error = store.put_snapshot(&snapshot).expect_err("oversized file");
            assert_eq!(error.code(), codes::SKILL_STATE_CORRUPT);
            assert!(matches!(error, StoreError::WriteTooLarge { .. }));
            assert!(store.snapshot(&snapshot.id).expect("readable").is_none());
            let blobs_dir = store.snapshots_dir().join("blobs");
            assert!(blobs_dir.read_dir().expect("blobs").next().is_none());
        }
    }
}
