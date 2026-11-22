// 검토 기록: 수집한 스냅숏에서 SKILL.md·라이선스·참조·지시문 표시를 읽어 ReviewedSkillV1 을 v0.4.0 필드 그대로 만든다.
//! `ReviewedSkillV1`, field for field as v0.4.0 records it (`v0.4:src/oah/discovery/review.py:131-232`), and the review
//! that builds it from one collection.
//!
//! [`review`] checks the source, collects it once through a [`Collector`], and reads the pinned `SKILL.md` as data:
//! front matter, size, screening, references, compatibility and the license verdict under the allowed licenses. The
//! record's `id` is the digest of everything else. Storing the record, the identity-collision refusal and the
//! `unchanged` answer for a repeated review need the review store, which this module does not hold.

use super::frontmatter::{Frontmatter, frontmatter};
use super::identity::{Identity, POLICY_VERSION, SourceSpec, identity};
use super::json::{digest, sha256_hex};
use super::license::{EligibilityStatus, LicenseForm, eligibility, verdict};
use super::screening::{Reference, Screening, references, screening};
use super::snapshot::{Collector, Observation, Snapshot};
use super::{FailReason, ReviewError};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;
use std::time::{SystemTime, UNIX_EPOCH};

/// The largest `SKILL.md` a review accepts: 192 KiB.
pub const SKILL_LIMIT: usize = 196_608;
/// Tools only Claude Code offers; a skill that names one is offered to Claude Code only.
pub const CLAUDE_ONLY_TOOLS: [&str; 8] = [
    "Task",
    "TodoWrite",
    "TodoRead",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Agent",
    "Skill",
];
/// At most this many files and governing license files are listed in a record.
const FILES_LIMIT: usize = 256;
const GOVERNING_LIMIT: usize = 16;

/// Where the review request came from; kept as the caller gave it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Candidate {
    pub provider: Value,
    pub query_id: Value,
    pub hit: Value,
    pub seed: Value,
}

/// One pinned file of the reviewed folder.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecordFile {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
}

/// What was pinned.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecordSource {
    pub provider: String,
    pub source_id: String,
    pub spec_digest: String,
    pub locator: String,
    pub commit: Option<String>,
    pub tree: Option<String>,
    pub path: String,
    pub snapshot: String,
    pub skill_sha256: String,
    pub skill_lf_sha256: String,
    pub files: Vec<RecordFile>,
    pub references: Vec<Reference>,
}

/// Whether the license files read as a known expression.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum LicenseWord {
    Recognized,
    Unknown,
}

/// A license file that governs the folder.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Governing {
    pub path: String,
    pub sha256: String,
}

/// The license as the record keeps it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecordLicense {
    pub verdict: LicenseWord,
    pub expression: Option<String>,
    pub form: LicenseForm,
    pub eligibility: EligibilityStatus,
    pub governing: Vec<Governing>,
}

/// A host v0.4.0 records compatibility for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Host {
    Claude,
    Codex,
}

/// Which hosts can run the skill, and what it needs.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Compatibility {
    pub hosts: Vec<Host>,
    pub requires_tools: Vec<String>,
    pub scripts_present: bool,
    pub problems: Vec<String>,
}

/// The review's own answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ReviewStatus {
    Reviewed,
    Rejected,
}

/// Why a review rejected the skill.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum RejectReason {
    MalformedFrontmatter,
    TooLarge,
    NameMismatch,
}

/// One review record (`oah-skill-review/v1`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReviewedSkillV1 {
    pub version: u32,
    pub candidate: Candidate,
    pub source: RecordSource,
    pub frontmatter: Frontmatter,
    pub license: RecordLicense,
    pub compatibility: Compatibility,
    pub screening: Screening,
    pub status: ReviewStatus,
    pub reason: Option<RejectReason>,
    pub checked_at: String,
    pub policy_digest: String,
    pub id: String,
}

impl ReviewedSkillV1 {
    /// The record as v0.4.0 stores it.
    pub fn to_json(&self) -> Value {
        serde_json::to_value(self).unwrap_or(Value::Null)
    }

    /// The digest of everything but `id` (v0.4.0 `store.with_id`).
    pub fn content_id(&self) -> String {
        digest_record_fields(self.to_json())
    }
}

pub(super) fn digest_record_fields(mut encoded: Value) -> String {
    if let Some(object) = encoded.as_object_mut() {
        object.remove("id");
    }
    digest(&encoded)
}

/// One review: which folder, who asked, which licenses are allowed, and the clock.
#[derive(Debug, Clone)]
pub struct ReviewRequest<'a> {
    /// The GitHub `owner/name`.
    pub repo: &'a str,
    /// The folder inside the repository.
    pub path: &'a str,
    /// `provider`, `query_id`, `hit` and `seed` of the request.
    pub candidate: &'a Map<String, Value>,
    /// The allowed licenses (SPDX ids).
    pub allow: &'a [String],
    pub now: SystemTime,
}

/// The policy digest a record names: the review policy and the sorted allowed licenses.
pub fn policy_digest(allow: &[String]) -> String {
    let mut sorted = allow.to_vec();
    sorted.sort();
    digest(&json!({"policy": POLICY_VERSION, "license_allow": sorted}))
}

/// Pins, reads and classifies one skill folder.
pub fn review(
    collector: &dyn Collector,
    request: &ReviewRequest<'_>,
) -> Result<ReviewedSkillV1, ReviewError> {
    let (identity, spec) = identity(request.repo, request.path)?;
    let observation = collector.observe(&spec);
    let (word, problems) = observation.result();
    if problems.iter().any(|problem| problem == "path-missing") {
        let detail = format!(
            "{} has no folder {} at its current commit",
            request.repo, request.path
        );
        return Err(ReviewError::Failed {
            reason: FailReason::PathUnresolved,
            detail,
        });
    }
    match observation {
        Observation::Snapshot(snapshot) if snapshot.verified() => {
            if snapshot.validate(&spec).is_err() {
                let detail = format!(
                    "the collector could not verify {} (failed: bad-response)",
                    request.repo
                );
                return Err(ReviewError::Failed {
                    reason: FailReason::Collector,
                    detail,
                });
            }
            record(&identity, &spec, &snapshot, request)
        }
        _ => {
            let listed = if problems.is_empty() {
                "no snapshot".to_string()
            } else {
                problems.join(", ")
            };
            let detail = format!(
                "the collector could not verify {} ({word}: {listed})",
                request.repo
            );
            Err(ReviewError::Failed {
                reason: FailReason::Collector,
                detail,
            })
        }
    }
}

/// Python truthiness of a JSON value.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

fn text_of<'a>(item: &'a Value, key: &str) -> &'a str {
    item.get(key).and_then(Value::as_str).unwrap_or("")
}

fn record(
    identity: &Identity,
    spec: &SourceSpec,
    snapshot: &Snapshot,
    request: &ReviewRequest<'_>,
) -> Result<ReviewedSkillV1, ReviewError> {
    let manifest = &snapshot.manifest;
    let folder = format!("{}/", request.path);
    let listed = manifest
        .get("files")
        .and_then(Value::as_array)
        .into_iter()
        .flatten();
    let files: Vec<&Value> = listed
        .filter(|item| text_of(item, "path").starts_with(&folder))
        .collect();
    let skill_path = format!("{}/SKILL.md", request.path);
    let Some(skill_file) = files
        .iter()
        .find(|item| text_of(item, "path") == skill_path)
    else {
        let detail = format!("{}/{} has no SKILL.md", request.repo, request.path);
        return Err(ReviewError::Failed {
            reason: FailReason::PathUnresolved,
            detail,
        });
    };
    let skill_sha256 = text_of(skill_file, "sha256");
    let Some(raw) = snapshot.blob(skill_sha256) else {
        return Err(ReviewError::Failed {
            reason: FailReason::Collector,
            detail: format!(
                "the collector could not verify {} (failed: bad-response)",
                request.repo
            ),
        });
    };
    let text = String::from_utf8_lossy(raw);
    let lf = text
        .trim_start_matches('\u{feff}')
        .replace("\r\n", "\n")
        .replace('\r', "\n");
    let front = frontmatter(&text);
    let read = |sha256: &str| snapshot.blob(sha256).map(<[u8]>::to_vec);
    let license_verdict = verdict(manifest.get("license"), &read);
    let license_eligibility = eligibility(&license_verdict, request.allow);
    let present: BTreeSet<String> = files
        .iter()
        .map(|item| text_of(item, "path").to_string())
        .collect();
    let refs = references(&text, request.path, &present);
    let screen = screening(&text);
    let tools = front.allowed_tools.clone().unwrap_or_default();
    let claude_only = tools
        .iter()
        .any(|tool| CLAUDE_ONLY_TOOLS.contains(&tool.split('(').next().unwrap_or(tool)));
    let hosts = if claude_only {
        vec![Host::Claude]
    } else {
        vec![Host::Claude, Host::Codex]
    };
    let mut problems: Vec<String> = refs
        .iter()
        .filter(|item| !item.present)
        .map(|item| format!("missing-reference: {}", item.path))
        .collect();
    let hit = request.candidate.get("hit").cloned().unwrap_or(Value::Null);
    let hit_name = hit
        .as_object()
        .filter(|hit| !hit.is_empty())
        .and_then(|hit| hit.get("name"))
        .filter(|name| truthy(name));
    let front_name = front.name.clone().map_or(Value::Null, Value::String);
    let mismatch = hit_name.is_some_and(|name| *name != front_name);
    let (status, reason) = if front.name.is_none() || front.description.is_none() {
        (
            ReviewStatus::Rejected,
            Some(RejectReason::MalformedFrontmatter),
        )
    } else if raw.len() > SKILL_LIMIT {
        (ReviewStatus::Rejected, Some(RejectReason::TooLarge))
    } else if mismatch {
        (ReviewStatus::Rejected, Some(RejectReason::NameMismatch))
    } else {
        (ReviewStatus::Reviewed, None)
    };
    if mismatch {
        problems.push("name-mismatch".to_string());
    }
    if screen.marker_collision {
        problems.push("marker-collision: not deliverable inline".to_string());
    }
    let observed = manifest.get("observed");
    let observed_text = |key: &str| {
        observed
            .and_then(|observed| observed.get(key))
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    let governing = manifest
        .get("license")
        .and_then(|license| license.get("files"))
        .and_then(Value::as_array);
    let candidate = |key: &str| request.candidate.get(key).cloned().unwrap_or(Value::Null);
    let mut record = ReviewedSkillV1 {
        version: 1,
        candidate: Candidate {
            provider: request
                .candidate
                .get("provider")
                .cloned()
                .unwrap_or_else(|| json!("github")),
            query_id: candidate("query_id"),
            hit,
            seed: candidate("seed"),
        },
        source: RecordSource {
            provider: "github".to_string(),
            source_id: identity.source_id.clone(),
            spec_digest: spec.spec_digest(),
            locator: spec.locator.clone(),
            commit: observed_text("commit"),
            tree: observed_text("tree"),
            path: request.path.to_string(),
            snapshot: snapshot.id.clone(),
            skill_sha256: format!("sha256:{skill_sha256}"),
            skill_lf_sha256: format!("sha256:{}", sha256_hex(lf.as_bytes())),
            files: files
                .iter()
                .take(FILES_LIMIT)
                .map(|item| RecordFile {
                    path: text_of(item, "path").to_string(),
                    bytes: item.get("size").and_then(Value::as_u64).unwrap_or(0),
                    sha256: format!("sha256:{}", text_of(item, "sha256")),
                })
                .collect(),
            references: refs,
        },
        frontmatter: front,
        license: RecordLicense {
            verdict: if license_verdict
                .expression
                .as_deref()
                .is_some_and(|text| !text.is_empty())
            {
                LicenseWord::Recognized
            } else {
                LicenseWord::Unknown
            },
            expression: license_verdict.expression.clone(),
            form: license_verdict.form,
            eligibility: license_eligibility.status,
            governing: governing
                .into_iter()
                .flatten()
                .take(GOVERNING_LIMIT)
                .map(|item| Governing {
                    path: text_of(item, "path").to_string(),
                    sha256: format!("sha256:{}", text_of(item, "sha256")),
                })
                .collect(),
        },
        compatibility: Compatibility {
            hosts,
            requires_tools: tools,
            scripts_present: files
                .iter()
                .any(|item| text_of(item, "path").starts_with(&format!("{folder}scripts/"))),
            problems,
        },
        screening: screen,
        status,
        reason,
        checked_at: utc_text(request.now),
        policy_digest: policy_digest(request.allow),
        id: String::new(),
    };
    record.id = record.content_id();
    Ok(record)
}

/// `now.astimezone(timezone.utc).isoformat(timespec="seconds")`.
pub fn utc_text(now: SystemTime) -> String {
    let seconds = match now.duration_since(UNIX_EPOCH) {
        Ok(after) => i64::try_from(after.as_secs()).unwrap_or(i64::MAX),
        Err(before) => {
            let before = before.duration();
            let whole = i64::try_from(before.as_secs()).unwrap_or(i64::MAX);
            -(whole + i64::from(before.subsec_nanos() > 0))
        }
    };
    let (days, rest) = (seconds.div_euclid(86_400), seconds.rem_euclid(86_400));
    // Howard Hinnant's days-to-civil conversion.
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}+00:00",
        rest / 3600,
        rest % 3600 / 60,
        rest % 60
    )
}
