// 출처 신원: GitHub owner/repo 와 폴더 경로를 검사하고, 정규화한 명세 필드로 sk- 출처 ID 와 전체 신원 해시를 만든다.
//! Source identity and the commit-tracked spec of one GitHub skill folder.
//!
//! Ports `identity` and `hit_paths` (`v0.4:src/oah/discovery/review.py:65-80,235-239`) with the parts of `build_spec`,
//! `github_locator` and `_check_paths` (`v0.4:src/oah/design/sources/model.py:99-173,223-233,274-337`) and the portable
//! path rule (`v0.4:src/oah/toolkit/fetch.py:198-226`) that a review reaches. A review always builds
//! `https://github.com/<owner/repo>` from a name that passed the owner/repo check, so the URL form checks of
//! `github_locator` (scheme, host, user, port, query, fragment, segment count) always hold and are not repeated.

use super::ReviewError;
use super::json::digest;
use super::pattern::Pattern;
use super::pystr::{is_format, portable_key_component, repr};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::LazyLock;

/// The review policy id; it is part of every identity and of the policy digest.
pub const POLICY_VERSION: &str = "oah-skill-review/v1";
/// The source verification policy every snapshot spec names.
pub const VERIFICATION_POLICY: &str = "source-verification-v1";
/// The longest repository path a spec accepts.
const PATH_MAX: usize = 300;
/// File names Windows reserves for devices, in any case and with any extension.
const WINDOWS_DEVICES: [&str; 26] = [
    "con", "prn", "aux", "nul", "conin$", "conout$", "com0", "com1", "com2", "com3", "com4",
    "com5", "com6", "com7", "com8", "com9", "lpt0", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6",
    "lpt7", "lpt8", "lpt9",
];

static REPO: LazyLock<Pattern> =
    LazyLock::new(|| Pattern::fixed(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", false));
static LITERAL: LazyLock<Pattern> =
    LazyLock::new(|| Pattern::fixed(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", false));
static OWNER: LazyLock<Pattern> =
    LazyLock::new(|| Pattern::fixed(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", false));
static REPO_NAME: LazyLock<Pattern> =
    LazyLock::new(|| Pattern::fixed(r"[A-Za-z0-9._-]{1,100}", false));
static HIT_NAME: LazyLock<Pattern> = LazyLock::new(|| Pattern::fixed(r"[A-Za-z0-9._-]+", false));

/// The normalized fields an identity digests.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IdentityFields {
    pub provider: String,
    pub locator: String,
    pub track: String,
    #[serde(rename = "ref")]
    pub reference: String,
    pub paths: Vec<String>,
    pub manifest_path: Option<String>,
    pub verification_policy_version: String,
}

/// A skill folder's identity: the E source id (`sk-` and 37 hex digits), the full `sha256:` digest and its fields.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub source_id: String,
    pub full: String,
    pub fields: IdentityFields,
}

/// The commit-tracked spec of one GitHub folder: what decides what a snapshot of it is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceSpec {
    pub locator: String,
    pub paths: Vec<String>,
}

impl SourceSpec {
    /// v0.4.0 `SourceSpec.verification()` for a commit-tracked GitHub source without a manifest.
    pub fn verification(&self) -> Value {
        json!({
            "provider": "github",
            "locator": self.locator,
            "track": "commit",
            "tag_prefix": "",
            "channel": "stable",
            "ref": "HEAD",
            "paths": self.paths,
            "manifest_path": null,
            "release_order": "",
            "selection_policy": null,
            "verification_policy": VERIFICATION_POLICY,
        })
    }

    /// The digest of [`SourceSpec::verification`].
    pub fn spec_digest(&self) -> String {
        digest(&self.verification())
    }
}

/// The identity and spec of `path` in the GitHub repository `repo` (`owner/name`), tracked by commit.
pub fn identity(repo: &str, path: &str) -> Result<(Identity, SourceSpec), ReviewError> {
    let spec = source_spec(repo, path)?;
    let fields = IdentityFields {
        provider: "github".to_string(),
        locator: spec.locator.clone(),
        track: "commit".to_string(),
        reference: "HEAD".to_string(),
        paths: spec.paths.clone(),
        manifest_path: None,
        verification_policy_version: POLICY_VERSION.to_string(),
    };
    let value = serde_json::to_value(&fields).unwrap_or(Value::Null);
    let full = digest(&value);
    let hex = full.trim_start_matches("sha256:");
    let identity = Identity {
        source_id: format!("sk-{}", &hex[..37]),
        full,
        fields,
    };
    Ok((identity, spec))
}

/// The literal folders a search hit may live in: `skills/<name>`, then `<name>`; none for a non-literal name.
pub fn hit_paths(name: &str) -> Vec<String> {
    if !HIT_NAME.is_full(name) || name == "." || name == ".." {
        return Vec::new();
    }
    vec![format!("skills/{name}"), name.to_string()]
}

pub(crate) fn source_spec(repo: &str, path: &str) -> Result<SourceSpec, ReviewError> {
    if !REPO.is_full(repo) {
        return Err(ReviewError::SourceUnsupported(format!(
            "{} is not an owner/repo GitHub name; only GitHub skill folders are reviewed",
            repr(repo)
        )));
    }
    if !LITERAL.is_full(path) || path.split('/').any(|part| part == "..") {
        return Err(ReviewError::SourceUnsupported(format!(
            "{} is not a literal folder path inside the repository",
            repr(path)
        )));
    }
    let (owner, name) = repo.split_once('/').unwrap_or((repo, ""));
    let name = if name.to_lowercase().ends_with(".git") {
        &name[..name.len() - 4]
    } else {
        name
    };
    if !OWNER.is_full(owner) || !REPO_NAME.is_full(name) || name == "." || name == ".." {
        return Err(ReviewError::SourceUnsupported(
            "OAH_SOURCES_LOCATOR_INVALID: the owner or repository name is not a GitHub name"
                .to_string(),
        ));
    }
    let locator = format!(
        "https://github.com/{}/{}",
        owner.to_lowercase(),
        name.to_lowercase()
    );
    let paths =
        check_paths(&[path.to_string()], "--path").map_err(ReviewError::SourceUnsupported)?;
    Ok(SourceSpec { locator, paths })
}

/// v0.4.0 `_check_paths`: repository paths with forward slashes and no wildcards, portable, given once; sorted.
fn check_paths(values: &[String], what: &str) -> Result<Vec<String>, String> {
    for value in values {
        let refused = value.is_empty()
            || value.chars().count() > PATH_MAX
            || value.starts_with(['/', '-'])
            || value.ends_with('/')
            || value.contains(['\\', '[', ']', ':', '*', '?', '"', '<', '>', '|'])
            || value.contains("//")
            || value
                .chars()
                .any(|c| u32::from(c) < 32 || u32::from(c) == 127);
        if refused {
            return Err(format!(
                "OAH_SOURCES_USAGE: {what} {}: give a repository path such as crates/foo or src/lib.rs, with forward \
                 slashes and no wildcards",
                repr(value)
            ));
        }
    }
    portable_paths(values).map_err(|problem| format!("OAH_SOURCES_USAGE: {what}: {problem}"))?;
    let mut paths = values.to_vec();
    paths.sort();
    if paths.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(format!("OAH_SOURCES_USAGE: {what}: a path is given twice"));
    }
    Ok(paths)
}

/// The portable key for one path component (`v0.4.0`: `unicodedata.normalize("NFKC", part).casefold()`
/// on Python 3.11 / Unicode 14.0.0).
pub(crate) fn portable_component_key(part: &str) -> String {
    portable_key_component(part)
}

/// v0.4.0 `check_portable_paths(paths, staging=False)`: names that alias or escape on supported file systems are refused;
/// the error is the refusal text without its code. Keys use [`portable_component_key`].
pub(crate) fn portable_paths(paths: &[String]) -> Result<(), String> {
    let mut seen: BTreeMap<String, (String, bool)> = BTreeMap::new();
    for path in paths {
        let parts: Vec<&str> = path.split('/').collect();
        let mut key = String::new();
        for (index, part) in parts.iter().enumerate() {
            let normalized = portable_component_key(part);
            let device = normalized
                .split('.')
                .next()
                .unwrap_or("")
                .trim_end_matches(' ');
            let unsafe_name = part.is_empty()
                || normalized == "."
                || normalized == ".."
                || normalized == ".git"
                || normalized.ends_with(['.', ' '])
                || normalized.chars().any(|c| {
                    u32::from(c) < 32
                        || u32::from(c) == 127
                        || "<>:\"/\\|?*\u{fffd}".contains(c)
                        || is_format(c)
                })
                || WINDOWS_DEVICES.contains(&device);
            if unsafe_name {
                return Err(format!(
                    "unsafe or reserved upstream path {}; ask upstream to rename it before retrying",
                    repr(path)
                ));
            }
            if index > 0 {
                key.push('/');
            }
            key.push_str(&normalized);
            let spelling = parts[..=index].join("/");
            let is_file = index + 1 == parts.len();
            if let Some((previous, previous_file)) = seen.get(&key)
                && (*previous != spelling || *previous_file || is_file)
            {
                return Err(format!(
                    "portable path collision: {} and {}; ask upstream to rename them before retrying",
                    repr(previous),
                    repr(&spelling)
                ));
            }
            seen.insert(key.clone(), (spelling, is_file));
        }
    }
    Ok(())
}
