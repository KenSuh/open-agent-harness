// 스냅숏 수집: 한 커밋의 트리에서 선택 폴더의 파일을 한도 안에서 읽어 고정하고, v0.4.0 스냅숏 매니페스트와 그 ID 를 만든다.
//! One collection of a commit-tracked GitHub folder, as v0.4.0's collector records it
//! (`v0.4:src/oah/design/sources/adapters.py:34-36,109-153,253-282`, `v0.4:src/oah/design/sources/model.py:548-591`).
//!
//! A [`Collector`] resolves the spec's ref to a commit and hands a [`Tree`] over it to [`snapshot`], which validates
//! object identifiers, returned byte lengths, and cumulative resource bounds, gathers license evidence and builds the
//! snapshot manifest. The manifest's v0.4.0 stored bytes name it (`sha256:` of [`record_bytes`]). Every collection
//! here is a first one: there is no earlier verified snapshot to compare with, so an attempt is `changed` (verified),
//! `unverified`, `blocked` or `failed`, as v0.4.0's `transition` names a fresh source's attempt.
//!
//! Open production boundary: verifying remote Git blob SHA-1 object ids (`sha1("blob <len>\0" || data) == entry.oid`)
//! requires a shared SHA-1 dependency (anticipated by M1-S3) and remains explicitly open before wiring an HTTPS
//! [`Tree`] implementation. Local fixture reads through `LocalGit` already address Git's object database by validated
//! object id.

use super::identity::{SourceSpec, portable_paths};
use super::json::{digest, record_bytes, sha256_hex};
use super::license::{EVIDENCE_BYTES_LIMIT, EVIDENCE_FILE_LIMIT, MANIFEST_LIMIT, discover};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

/// The largest file a snapshot reads.
pub const FILE_LIMIT: u64 = 1 << 20;
/// The most bytes one snapshot reads across selected files.
pub const SNAPSHOT_LIMIT: u64 = 8 << 20;
/// The most files one snapshot reads across selected files.
pub const FILE_COUNT_LIMIT: usize = 500;
/// The largest single blob the snapshot store accepts (v0.4.0 `BLOB_LIMIT`).
pub const BLOB_LIMIT: u64 = 8 << 20;
/// Aggregate byte budget across all stored blobs in one snapshot (selected files + license/notice evidence).
pub const SNAPSHOT_BLOBS_LIMIT: u64 = SNAPSHOT_LIMIT + EVIDENCE_BYTES_LIMIT;

/// The closed problem codes an attempt may report (v0.4.0 `PROBLEMS`).
const PROBLEMS: [&str; 14] = [
    "tag-moved",
    "ref-mismatch",
    "path-missing",
    "unsafe-entry",
    "oversize",
    "selection-incomplete",
    "no-matching-release",
    "tag-missing",
    "ref-missing",
    "manifest-missing",
    "release-notes-too-large",
    "changed-during-check",
    "tag-disappeared",
    "release-metadata-invalid",
];
/// The closed failure codes an attempt may report (v0.4.0 `FAILURES`).
const FAILURES: [&str; 19] = [
    "timeout",
    "network",
    "dns",
    "rate-limited",
    "not-found",
    "gone",
    "moved",
    "proxy-configured",
    "private-destination",
    "insecure-url",
    "redirect-off-origin",
    "too-many-redirects",
    "oversize",
    "content-type",
    "content-encoding",
    "bad-response",
    "git-failed",
    "http-error",
    "attempt-failed",
];

/// Whether `text` is a valid lowercase hexadecimal Git object id (SHA-1 40 hex or SHA-256 64 hex).
pub(super) fn is_object_id(text: &str) -> bool {
    matches!(text.len(), 40 | 64)
        && text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Whether `text` is a valid lowercase 64-digit SHA-256 hexadecimal digest.
pub(super) fn is_sha256_hex(text: &str) -> bool {
    text.len() == 64
        && text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// One tree entry: its mode, its object id (empty when `entries` lists something that is not a blob) and its path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub mode: String,
    pub oid: String,
    pub path: String,
}

/// A read of the collected commit failed; the attempt fails as `git-failed`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CollectError {
    pub message: String,
}

impl fmt::Display for CollectError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

/// Read access to one collected commit.
pub trait Tree {
    /// Every entry at or below `path` (`""` is the whole tree), recursively, with an empty object id for
    /// anything that is not a blob.
    fn entries(&self, path: &str) -> Result<Vec<Entry>, CollectError>;
    /// The direct children of `folder` (`""` is the top of the tree), with their object ids.
    fn children(&self, folder: &str) -> Result<Vec<Entry>, CollectError>;
    /// The size of a blob, read before its bytes are.
    fn size(&self, blob: &str) -> Result<u64, CollectError>;
    /// The bytes of a blob.
    fn read(&self, blob: &str) -> Result<Vec<u8>, CollectError>;
}

/// A collected snapshot: its manifest, the manifest's id and the bytes it read, by SHA-256 hex.
#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    pub id: String,
    pub manifest: Value,
    pub blobs: BTreeMap<String, Vec<u8>>,
}

impl Snapshot {
    /// Whether every identity check passed and no problems are recorded.
    pub fn verified(&self) -> bool {
        self.manifest
            .get("verified")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            && self
                .manifest
                .get("problems")
                .and_then(Value::as_array)
                .is_some_and(Vec::is_empty)
    }

    /// The verified bytes of a stored blob: returns `None` if missing, malformed key, or SHA-256 mismatch.
    pub fn blob(&self, sha256: &str) -> Option<&[u8]> {
        if !is_sha256_hex(sha256) {
            return None;
        }
        let data = self.blobs.get(sha256)?;
        if sha256_hex(data) != sha256 {
            return None;
        }
        Some(data.as_slice())
    }

    /// Validates this snapshot's manifest identity, spec binding, observed commit/tree ids, resource bounds,
    /// and the existence and content digest of every recorded file and license blob.
    pub fn validate(&self, spec: &SourceSpec) -> Result<(), String> {
        let Some(object) = self.manifest.as_object() else {
            return Err("snapshot manifest is not an object".to_string());
        };
        if object.get("version").and_then(Value::as_u64) != Some(1)
            || object.get("kind").and_then(Value::as_str) != Some("oah-source-snapshot")
        {
            return Err("snapshot manifest version or kind is invalid".to_string());
        }
        if !self.verified() {
            return Err("snapshot manifest is not verified or carries problems".to_string());
        }
        let expected_id = format!(
            "sha256:{}",
            sha256_hex(record_bytes(&self.manifest).as_bytes())
        );
        if self.id != expected_id {
            return Err("snapshot id does not match manifest bytes".to_string());
        }
        let expected_digest = spec.spec_digest();
        if object.get("spec_digest").and_then(Value::as_str) != Some(expected_digest.as_str())
            || object.get("spec") != Some(&spec.verification())
        {
            return Err("snapshot manifest does not match requested spec".to_string());
        }
        let Some(observed) = object.get("observed").and_then(Value::as_object) else {
            return Err("snapshot manifest is missing observed metadata".to_string());
        };
        let commit = observed.get("commit").and_then(Value::as_str).unwrap_or("");
        let tree_id = observed.get("tree").and_then(Value::as_str).unwrap_or("");
        if !is_object_id(commit)
            || !is_object_id(tree_id)
            || observed.get("ref").and_then(Value::as_str) != Some("HEAD")
        {
            return Err("snapshot observed commit, tree or ref is invalid".to_string());
        }
        let Some(files) = object.get("files").and_then(Value::as_array) else {
            return Err("snapshot manifest files is not an array".to_string());
        };
        if files.len() > FILE_COUNT_LIMIT {
            return Err("snapshot manifest exceeds selected file count limit".to_string());
        }
        let mut selected_bytes: u64 = 0;
        let mut selected_paths = Vec::with_capacity(files.len());
        for item in files {
            let Some(file) = item.as_object() else {
                return Err("snapshot file entry is not an object".to_string());
            };
            let path = file.get("path").and_then(Value::as_str).unwrap_or("");
            let mode = file.get("mode").and_then(Value::as_str).unwrap_or("");
            let oid = file.get("blob").and_then(Value::as_str).unwrap_or("");
            let Some(size) = file.get("size").and_then(Value::as_u64) else {
                return Err("snapshot file entry is missing size".to_string());
            };
            let sha256 = file.get("sha256").and_then(Value::as_str).unwrap_or("");
            if path.is_empty()
                || (!spec.paths.is_empty() && !path_within_spec(path, &spec.paths))
                || !matches!(mode, "100644" | "100755")
                || !is_object_id(oid)
                || size > FILE_LIMIT
            {
                return Err(format!(
                    "snapshot file entry {path:?} fails bounds or identity"
                ));
            }
            selected_bytes = selected_bytes.saturating_add(size);
            if selected_bytes > SNAPSHOT_LIMIT {
                return Err("snapshot selected files exceed SNAPSHOT_LIMIT".to_string());
            }
            let Some(bytes) = self.blob(sha256) else {
                return Err(format!(
                    "snapshot file blob missing or digest mismatch for {path:?}"
                ));
            };
            if u64::try_from(bytes.len()).unwrap_or(u64::MAX) != size {
                return Err(format!("snapshot file size mismatch for {path:?}"));
            }
            selected_paths.push(path.to_string());
        }
        if portable_paths(&selected_paths).is_err() {
            return Err("snapshot selected paths fail portability check".to_string());
        }
        let Some(license) = object.get("license").and_then(Value::as_object) else {
            return Err("snapshot manifest is missing license object".to_string());
        };
        let mut evidence_count: usize = 0;
        let mut evidence_bytes: u64 = 0;
        let mut check_evidence = |item: &Value, max_size: u64| -> Result<(), String> {
            let Some(entry) = item.as_object() else {
                return Err("snapshot license entry is not an object".to_string());
            };
            let path = entry.get("path").and_then(Value::as_str).unwrap_or("");
            let oid = entry.get("blob").and_then(Value::as_str).unwrap_or("");
            let Some(size) = entry.get("size").and_then(Value::as_u64) else {
                return Err("snapshot license entry is missing size".to_string());
            };
            let sha256 = entry.get("sha256").and_then(Value::as_str).unwrap_or("");
            if path.is_empty() || !is_object_id(oid) || size > max_size {
                return Err(format!(
                    "snapshot license entry {path:?} fails bounds or identity"
                ));
            }
            evidence_count += 1;
            evidence_bytes = evidence_bytes.saturating_add(size);
            if evidence_count > EVIDENCE_FILE_LIMIT || evidence_bytes > EVIDENCE_BYTES_LIMIT {
                return Err("snapshot license evidence exceeds aggregate budget".to_string());
            }
            let Some(bytes) = self.blob(sha256) else {
                return Err(format!(
                    "snapshot license blob missing or digest mismatch for {path:?}"
                ));
            };
            if u64::try_from(bytes.len()).unwrap_or(u64::MAX) != size {
                return Err(format!("snapshot license size mismatch for {path:?}"));
            }
            Ok(())
        };
        let Some(license_files) = license.get("files").and_then(Value::as_array) else {
            return Err("snapshot license.files is not an array".to_string());
        };
        for item in license_files {
            check_evidence(item, FILE_LIMIT)?;
        }
        let Some(license_notices) = license.get("notices").and_then(Value::as_array) else {
            return Err("snapshot license.notices is not an array".to_string());
        };
        for item in license_notices {
            check_evidence(item, FILE_LIMIT)?;
        }
        if let Some(manifest_item) = license
            .get("manifest")
            .filter(|manifest| !manifest.is_null())
        {
            check_evidence(manifest_item, MANIFEST_LIMIT)?;
        }
        Ok(())
    }

    fn problems(&self) -> Vec<String> {
        let listed = self
            .manifest
            .get("problems")
            .and_then(Value::as_array)
            .into_iter()
            .flatten();
        listed
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect()
    }
}

fn path_within_spec(path: &str, spec_paths: &[String]) -> bool {
    spec_paths.iter().any(|base| {
        base.is_empty()
            || path == base
            || path
                .strip_prefix(base.as_str())
                .is_some_and(|rest| rest.starts_with('/'))
    })
}

/// What one collection attempt found.
#[derive(Debug, Clone, PartialEq)]
pub enum Observation {
    Snapshot(Snapshot),
    /// The ref is missing, or another reason kept the attempt from reading the tree.
    Blocked {
        problems: Vec<String>,
    },
    /// The collector failed; `code` is a v0.4.0 failure code such as `git-failed` or `timeout`.
    Failed {
        code: String,
    },
}

impl Observation {
    /// The result word and problems a fresh source's attempt reports (v0.4.0 `transition` with no earlier head).
    pub fn result(&self) -> (&'static str, Vec<String>) {
        let closed = |problems: Vec<String>, fallback: &str| {
            let kept: Vec<String> = problems
                .into_iter()
                .filter(|problem| PROBLEMS.contains(&problem.as_str()))
                .collect();
            if kept.is_empty() {
                vec![fallback.to_string()]
            } else {
                kept
            }
        };
        match self {
            Observation::Snapshot(snapshot) if snapshot.verified() => ("changed", Vec::new()),
            Observation::Snapshot(snapshot) => {
                ("unverified", closed(snapshot.problems(), "ref-mismatch"))
            }
            Observation::Blocked { problems } => {
                ("blocked", closed(problems.clone(), "selection-incomplete"))
            }
            Observation::Failed { code } => {
                let code = if FAILURES.contains(&code.as_str()) {
                    code.as_str()
                } else {
                    "network"
                };
                ("failed", vec![code.to_string()])
            }
        }
    }
}

/// Collects one commit-tracked GitHub folder.
pub trait Collector {
    /// One attempt for `spec`: resolve its ref now and read the selected paths.
    fn observe(&self, spec: &SourceSpec) -> Observation;
}

/// v0.4.0 `read_paths`: the files of the selected paths within the limits, sorted by path, and the problems found.
fn read_paths(
    tree: &dyn Tree,
    paths: &[String],
    store: &mut dyn FnMut(Vec<u8>) -> Result<String, CollectError>,
) -> Result<(Vec<Value>, Vec<String>), CollectError> {
    let mut files: Vec<(String, Value)> = Vec::new();
    let mut problems = BTreeSet::new();
    let mut total: u64 = 0;
    for path in paths {
        let entries = tree.entries(path)?;
        if entries.is_empty() {
            problems.insert("path-missing");
            continue;
        }
        let prefix = format!("{}/", path.trim_end_matches('/'));
        for entry in &entries {
            if !path.is_empty() && entry.path != *path && !entry.path.starts_with(&prefix) {
                return Err(CollectError {
                    message: format!(
                        "tree returned path {:?} outside requested {:?}",
                        entry.path, path
                    ),
                });
            }
        }
        let names: Vec<String> = entries.iter().map(|entry| entry.path.clone()).collect();
        if portable_paths(&names).is_err() {
            problems.insert("unsafe-entry");
            continue;
        }
        for entry in entries {
            if matches!(entry.mode.as_str(), "120000" | "160000") || entry.oid.is_empty() {
                problems.insert("unsafe-entry");
                continue;
            }
            if !is_object_id(&entry.oid) {
                return Err(CollectError {
                    message: format!("invalid blob object id {:?} for {}", entry.oid, entry.path),
                });
            }
            if !matches!(entry.mode.as_str(), "100644" | "100755") {
                problems.insert("unsafe-entry");
                continue;
            }
            let size = tree.size(&entry.oid)?;
            total = total.saturating_add(size);
            if size > FILE_LIMIT || total > SNAPSHOT_LIMIT || files.len() >= FILE_COUNT_LIMIT {
                problems.insert("oversize");
                continue;
            }
            let data = tree.read(&entry.oid)?;
            let actual = u64::try_from(data.len()).unwrap_or(u64::MAX);
            if actual != size || actual > FILE_LIMIT {
                return Err(CollectError {
                    message: format!(
                        "blob {} for {} returned {} bytes, expected {}",
                        entry.oid, entry.path, actual, size
                    ),
                });
            }
            let sha256 = store(data)?;
            let item = json!({"path": entry.path, "mode": entry.mode, "blob": entry.oid, "size": actual, "sha256": sha256});
            files.push((entry.path, item));
        }
    }
    files.sort_by(|left, right| left.0.cmp(&right.0));
    let problems = problems.into_iter().map(str::to_string).collect();
    Ok((files.into_iter().map(|(_, item)| item).collect(), problems))
}

/// The snapshot of `spec` at `commit` (tree `tree_id`), read through `tree`; v0.4.0 `_commit` after the fetch.
pub fn snapshot(
    tree: &dyn Tree,
    commit: &str,
    tree_id: &str,
    spec: &SourceSpec,
) -> Result<Snapshot, CollectError> {
    if !is_object_id(commit) || !is_object_id(tree_id) {
        return Err(CollectError {
            message: format!("invalid commit {commit:?} or tree id {tree_id:?}"),
        });
    }
    let mut blobs = BTreeMap::new();
    let mut stored_bytes: u64 = 0;
    let mut store = |data: Vec<u8>| -> Result<String, CollectError> {
        let len = u64::try_from(data.len()).unwrap_or(u64::MAX);
        if len > BLOB_LIMIT {
            return Err(CollectError {
                message: format!("blob of {len} bytes exceeds BLOB_LIMIT ({BLOB_LIMIT})"),
            });
        }
        let sha256 = sha256_hex(&data);
        if !blobs.contains_key(&sha256) {
            let next = stored_bytes.saturating_add(len);
            if next > SNAPSHOT_BLOBS_LIMIT {
                return Err(CollectError {
                    message: format!(
                        "snapshot blobs exceeded aggregate budget ({SNAPSHOT_BLOBS_LIMIT} bytes)"
                    ),
                });
            }
            stored_bytes = next;
            blobs.insert(sha256.clone(), data);
        }
        Ok(sha256)
    };
    let (files, problems) = read_paths(tree, &spec.paths, &mut store)?;
    let license = discover(tree, &spec.paths, None, &mut store)?;
    let spec_digest = spec.spec_digest();
    let content = json!({"spec": spec_digest, "files": files, "license": license});
    let mut content_key = digest(&content);
    if spec.paths.is_empty() {
        // Without selected paths every commit is new content.
        content_key = digest(
            &json!({"spec": spec_digest, "files": files, "license": license, "commit": commit}),
        );
    }
    let manifest = json!({
        "version": 1,
        "kind": "oah-source-snapshot",
        "spec": spec.verification(),
        "spec_digest": spec_digest,
        "observed": {"commit": commit, "tree": tree_id, "ref": "HEAD", "release": null},
        "files": files,
        "license": license,
        "document": null,
        "verified": problems.is_empty(),
        "problems": problems,
        "notices": [],
        "content_key": content_key,
    });
    let id = format!("sha256:{}", sha256_hex(record_bytes(&manifest).as_bytes()));
    Ok(Snapshot {
        id,
        manifest,
        blobs,
    })
}
