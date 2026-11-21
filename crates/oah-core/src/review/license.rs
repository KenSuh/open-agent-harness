// 라이선스 근거와 적격성: 고정한 스냅숏의 라이선스 파일 전체를 엄격히 대조하고, 형태(단일·선택·결합·미확인)와 정책별 적격성을 계산한다.
//! License evidence of one snapshot, and what a policy makes of it (`v0.4:src/oah/design/sources/license.py`).
//!
//! Evidence comes from the collected commit only: for every selected path, the license-like files of the deepest
//! folder at or above it govern, license-like files nested inside a selected folder must read the same, and notice
//! files are recorded. A declared expression is read only from a registered manifest. Nothing here reads a caller's
//! claim, GitHub's license field or a model's answer.
//!
//! [`classify_text`] accepts a file only when its whole text is one supported license (MIT, ISC, BSD-2-Clause,
//! BSD-3-Clause, Apache-2.0) with only a title and copyright lines before it. [`verdict`] gives the form (single,
//! choice, conjunction or unknown); [`eligibility`] what a policy makes of it. Both are pure and recomputed on every
//! read. The v0.4.0 patterns are kept as written and read by [`Pattern`].

use super::json::{number_text, sha256_hex};
use super::license_texts::{
    APACHE_APPENDIX, APACHE_START, APACHE_TERMS_SHA256, BSD_DISCLAIMER, BSD_OPENING, BSD_THIRD,
    ISC_BODY, MIT_BODY, OTHER_TITLES,
};
use super::pattern::Pattern;
use super::pystr::{casefold, is_format, repr, split_whitespace, splitlines, strip};
use super::snapshot::{CollectError, Entry, Tree, is_object_id};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

/// The licenses a text can be classified as.
pub const SUPPORTED: [&str; 5] = ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC"];
/// The largest license or notice file read.
pub const FILE_LIMIT: u64 = 1 << 20;
/// The largest registered manifest read.
pub const MANIFEST_LIMIT: u64 = 256 * 1024;
/// Maximum number of license, notice and manifest files read across one discovery pass.
pub const EVIDENCE_FILE_LIMIT: usize = 64;
/// Maximum aggregate bytes read across license, notice and manifest files in one discovery pass.
pub const EVIDENCE_BYTES_LIMIT: u64 = 4 << 20;
/// A selected folder with more entries is not checked for nested licenses.
pub const TREE_LIMIT: usize = 20_000;
/// The longest declared expression read.
pub const EXPRESSION_LIMIT: usize = 200;

const LIST_MARKER_SOURCE: &str = r"(?:\(?[0-9]{1,2}[.)]|[*\-•])";
const TITLES: [&str; 20] = [
    "mit license",
    "the mit license",
    "mit license (mit)",
    "the mit license (mit)",
    "mit",
    "isc license",
    "the isc license",
    "isc license (isc)",
    "the isc license (isc)",
    "isc",
    "bsd 2-clause license",
    "bsd 3-clause license",
    "bsd-2-clause license",
    "bsd-3-clause license",
    "the bsd license",
    "bsd license",
    "the 2-clause bsd license",
    "the 3-clause bsd license",
    "new bsd license",
    "simplified bsd license",
];
const OTHER_IDS: [(&str, &str); 19] = [
    ("gpl-2.0", "GPL-2.0"),
    ("gpl-2.0-only", "GPL-2.0"),
    ("gpl-2.0-or-later", "GPL-2.0"),
    ("gpl-3.0", "GPL-3.0"),
    ("gpl-3.0-only", "GPL-3.0"),
    ("gpl-3.0-or-later", "GPL-3.0"),
    ("lgpl-2.1", "LGPL-2.1"),
    ("lgpl-2.1-only", "LGPL-2.1"),
    ("lgpl-2.1-or-later", "LGPL-2.1"),
    ("lgpl-3.0", "LGPL-3.0"),
    ("lgpl-3.0-only", "LGPL-3.0"),
    ("lgpl-3.0-or-later", "LGPL-3.0"),
    ("agpl-3.0", "AGPL-3.0"),
    ("agpl-3.0-only", "AGPL-3.0"),
    ("agpl-3.0-or-later", "AGPL-3.0"),
    ("mpl-2.0", "MPL-2.0"),
    ("unlicense", "Unlicense"),
    ("busl-1.1", "BUSL-1.1"),
    ("sspl-1.0", "SSPL-1.0"),
];
const UNKNOWN_TEXT: &str = "not one complete supported license text (MIT, ISC, BSD-2-Clause, BSD-3-Clause or \
                            Apache-2.0) with supported holder fields";
const APACHE_EXAMPLE: &str = "Copyright [yyyy] [name of copyright owner]";

static LICENSE_NAME: LazyLock<Pattern> = LazyLock::new(|| {
    Pattern::fixed(
        concat!(
            r"(?:un)?licen[cs]e(?:[-._](?:mit|apache(?:-?2(?:\.0)?)?|bsd(?:-[23]-clause)?|isc|gpl[a-z0-9.-]*|lgpl[a-z0-9.-]*",
            r"|mpl[a-z0-9.-]*))?(?:\.(?:md|txt|rst|markdown))?|copying(?:\.(?:lesser|lib))?(?:\.(?:md|txt))?"
        ),
        true,
    )
});
static NOTICE_NAME: LazyLock<Pattern> =
    LazyLock::new(|| Pattern::fixed(r"(?:notice|copyright)(?:\.(?:md|txt|rst))?", true));
static HOLDER_FIELD: LazyLock<Pattern> = LazyLock::new(|| {
    Pattern::fixed(
        concat!(
            r"(?:(?P<notice>(?i:(?:portions )?copyright(?:[ \t]*(?:\(c\)|©))?|\(c\)|©)[ \t]+",
            r"[0-9]{4}(?:[ \t]*[-,][ \t]*[0-9]{4})*[ \t]+))?",
            r"[A-Z][A-Za-z0-9]*(?:['-][A-Z][A-Za-z0-9]*)*",
            r"(?:, (?i:inc|ltd|llc|corp|co))?\.?",
            r"(?: <[\w.+-]+@[\w.-]+>)?",
            r"(?: (?i:all rights reserved)\.?)?"
        ),
        false,
    )
});
static ALL_RIGHTS: LazyLock<Pattern> =
    LazyLock::new(|| Pattern::fixed(r"(?i:all rights reserved)\.?", false));
static LIST_MARKER: LazyLock<Pattern> = LazyLock::new(|| Pattern::fixed(LIST_MARKER_SOURCE, false));
static BSD3: LazyLock<Pattern> = LazyLock::new(|| {
    let text = format!(
        "{} {{MARKER}}{BSD_THIRD} {BSD_DISCLAIMER}",
        BSD_OPENING.replace("redistributions", "{MARKER}redistributions")
    );
    let marker = format!(r"(?:{LIST_MARKER_SOURCE}\s+)?");
    template(
        &text,
        &[
            (
                "HOLDER",
                r"(?:the\s+copyright\s+(?:holder|owner)|(?P<holder>[\s\S]{1,200}?))",
            ),
            ("ITS", r"(?:its|their)"),
            ("MARKER", &marker),
        ],
        true,
    )
});
static ISC: LazyLock<Pattern> = LazyLock::new(|| {
    template(
        ISC_BODY,
        &[
            ("AND", r"(?:and/or|and)"),
            ("DISCLAIM", r"(?:author disclaims|authors disclaim)"),
            ("AUTHOR", r"authors?"),
        ],
        false,
    )
});
static APPENDIX: LazyLock<Pattern> = LazyLock::new(|| {
    template(
        APACHE_APPENDIX,
        &[("COPYRIGHT", r"(?P<holder>[\s\S]{1,200}?)")],
        true,
    )
});
static APACHE_END: LazyLock<Pattern> =
    LazyLock::new(|| Pattern::fixed(r"end\s+of\s+terms\s+and\s+conditions", true));

/// What one license text reads as.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TextVerdict {
    pub spdx: Option<String>,
    /// The whole text is a supported license.
    pub strict: bool,
    pub problem: Option<String>,
}

impl TextVerdict {
    fn new(spdx: Option<&str>, strict: bool, problem: Option<String>) -> TextVerdict {
        TextVerdict {
            spdx: spdx.map(str::to_string),
            strict,
            problem,
        }
    }
}

/// The license form of one snapshot.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum LicenseForm {
    Single,
    Choice,
    Conjunction,
    Unknown,
}

impl LicenseForm {
    pub fn as_str(self) -> &'static str {
        match self {
            LicenseForm::Single => "single",
            LicenseForm::Choice => "choice",
            LicenseForm::Conjunction => "conjunction",
            LicenseForm::Unknown => "unknown",
        }
    }
}

/// What a policy makes of a verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum EligibilityStatus {
    Eligible,
    EligibleByChoice,
    Excluded,
    Unknown,
}

/// One read license file and how it reads.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ShownFile {
    pub path: Option<String>,
    pub spdx: Option<String>,
    pub strict: bool,
    pub problem: Option<String>,
}

/// The license form of one snapshot's evidence.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Verdict {
    pub form: LicenseForm,
    pub expression: Option<String>,
    pub ids: Vec<String>,
    /// Every id is a complete supported text.
    pub strict: bool,
    pub problems: Vec<String>,
    pub files: Vec<ShownFile>,
}

/// A policy's answer for a verdict.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Eligibility {
    pub status: EligibilityStatus,
    pub usable_under: Option<String>,
    pub policy: Vec<String>,
    pub reason: String,
}

/// A license expression declared by a registered manifest.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Declared {
    pub path: String,
    pub field: String,
    pub value: String,
}

/// Whether a file name is license-like (`LICENSE`, `LICENSE-MIT.md`, `COPYING`, ...).
pub fn is_license_name(name: &str) -> bool {
    LICENSE_NAME.is_full(name)
}

/// Whether a file name is a notice (`NOTICE`, `COPYRIGHT.txt`, ...).
pub fn is_notice_name(name: &str) -> bool {
    NOTICE_NAME.is_full(name)
}

/// Quotes, dashes and odd spaces made plain, lower case, whitespace runs as one space.
pub(super) fn normalize(text: &str) -> String {
    let plain: String = text
        .chars()
        .map(|c| match c {
            '\u{2018}'..='\u{201b}' => '\'',
            '\u{201c}'..='\u{201f}' => '"',
            '\u{2010}' | '\u{2011}' | '\u{2013}' | '\u{2014}' => '-',
            '\u{a0}' | '\u{c}' => ' ',
            c => c,
        })
        .collect();
    split_whitespace(&plain.to_lowercase()).join(" ")
}

/// Python's `re.escape`.
fn escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len() * 2);
    for c in text.chars() {
        if "()[]{}?*+-|^$\\.&~# \t\n\r\u{b}\u{c}".contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// v0.4.0 `_template`: the escaped text, with raw-text spellings (whitespace runs, curly quotes, dashes, "holder" or
/// "owner", either URL scheme) when `raw`, and each `{NAME}` replaced by its regex. Raw templates ignore case.
fn template(text: &str, slots: &[(&str, &str)], raw: bool) -> Pattern {
    let mut pattern = escape(text);
    if raw {
        pattern = pattern
            .replace(r"\ ", r"\s+")
            .replace('"', "[\"“”„‟]")
            .replace('\'', "['‘’‚‛]");
        pattern = pattern.replace(r"\-", "[-–—‐‑]");
        pattern = pattern.replace(r"copyright\s+holder", r"copyright\s+(?:holder|owner)");
        pattern = pattern.replace(r"http://www\.apache\.org/", r"https?://www\.apache\.org/");
    }
    for (name, regex) in slots {
        pattern = pattern.replace(&escape(&format!("{{{name}}}")), regex);
    }
    Pattern::fixed(&pattern, raw)
}

fn valid_holder(value: &str, notices: bool) -> bool {
    let lines = if notices {
        splitlines(value)
    } else {
        vec![value]
    };
    let mut populated = false;
    for raw in lines {
        let line = strip(raw);
        if notices && (line.is_empty() || ALL_RIGHTS.is_full(line)) {
            continue;
        }
        let chars: Vec<char> = line.chars().collect();
        let Some(found) = HOLDER_FIELD.full(&chars) else {
            return false;
        };
        if notices && found.name("notice").is_none() {
            return false;
        }
        populated = true;
    }
    populated
}

/// The index of the first line after an optional title and copyright block.
fn preamble_end(lines: &[&str]) -> usize {
    for (index, raw) in lines.iter().enumerate() {
        let line = strip(raw);
        if !line.is_empty() && !valid_holder(line, true) {
            let folded = normalize(strip(line.trim_matches(['#', '*', '_', ' '])));
            let folded = folded.trim_end_matches(['.', ':']);
            if !TITLES.contains(&folded) && folded != "all rights reserved" {
                return index;
            }
        }
    }
    lines.len()
}

fn controls(text: &str) -> bool {
    text.chars().filter(|&c| c != '\u{feff}').any(|c| {
        let code = u32::from(c);
        (code < 32 && !matches!(c, '\t' | '\n' | '\r' | '\u{c}')) || code == 127 || is_format(c)
    })
}

/// Which license this whole file is, strictly; or a title-only match that can only exclude.
pub fn classify_text(data: &[u8]) -> TextVerdict {
    let Ok(text) = std::str::from_utf8(data) else {
        return TextVerdict::new(None, false, Some("license text is not UTF-8".to_string()));
    };
    let text = text
        .trim_start_matches('\u{feff}')
        .replace("\r\n", "\n")
        .replace('\r', "\n");
    if controls(&text) {
        let problem = "license text holds control or invisible formatting characters";
        return TextVerdict::new(None, false, Some(problem.to_string()));
    }
    let lines: Vec<&str> = text.split('\n').collect();
    let raw_body = strip(&lines[preamble_end(&lines)..].join("\n")).to_string();
    let unknown = || TextVerdict::new(None, false, Some(UNKNOWN_TEXT.to_string()));
    let body_chars: Vec<char> = raw_body.chars().collect();
    if let Some(found) = BSD3.full(&body_chars) {
        return match found.name("holder") {
            Some(holder) if !valid_holder(&holder, false) => unknown(),
            _ => TextVerdict::new(Some("BSD-3-Clause"), true, None),
        };
    }
    if let Some(end) = APACHE_END.search(&body_chars, 0) {
        let rest: String = body_chars[end.end()..].iter().collect();
        let rest = strip(&rest);
        let rest_chars: Vec<char> = rest.chars().collect();
        let holder = APPENDIX
            .full(&rest_chars)
            .map(|found| found.name("holder").unwrap_or_default());
        let refused = match holder {
            _ if rest.is_empty() => false,
            None => true,
            Some(holder) => holder != APACHE_EXAMPLE && !valid_holder(&holder, true),
        };
        if refused {
            return unknown();
        }
        let terms: String = body_chars[..end.end()].iter().collect();
        let terms = normalize(&terms).replace("https://www.apache.org/", "http://www.apache.org/");
        if terms.starts_with(APACHE_START) && sha256_hex(terms.as_bytes()) == APACHE_TERMS_SHA256 {
            return TextVerdict::new(Some("Apache-2.0"), true, None);
        }
    }
    let body = normalize(&raw_body);
    if body == MIT_BODY {
        return TextVerdict::new(Some("MIT"), true, None);
    }
    if ISC.is_full(&body) {
        return TextVerdict::new(Some("ISC"), true, None);
    }
    let bsd = body
        .split(' ')
        .filter(|token| !LIST_MARKER.is_full(token))
        .collect::<Vec<_>>()
        .join(" ");
    let bsd = bsd.replace("copyright owner", "copyright holder");
    if bsd == format!("{BSD_OPENING} {BSD_DISCLAIMER}") {
        return TextVerdict::new(Some("BSD-2-Clause"), true, None);
    }
    let whole: String = normalize(&text).chars().take(4000).collect();
    for (spdx, phrases) in OTHER_TITLES {
        if phrases.iter().all(|phrase| whole.contains(phrase)) {
            let problem = format!("reads as {spdx}, which this kit never treats as eligible");
            return TextVerdict::new(Some(spdx), false, Some(problem));
        }
    }
    unknown()
}

fn canonical_id(item: &str) -> Option<&'static str> {
    let folded = casefold(item);
    SUPPORTED
        .into_iter()
        .find(|name| casefold(name) == folded)
        .or_else(|| {
            OTHER_IDS
                .iter()
                .find(|(key, _)| *key == folded)
                .map(|(_, name)| *name)
        })
}

/// The form and ids of a plain SPDX expression, or `None` for anything else (WITH, nesting, mixed AND/OR, unknown
/// ids). Cargo's legacy `A/B` reads as `A OR B`.
pub fn parse_expression(text: &str) -> Option<(LicenseForm, Vec<String>)> {
    if strip(text).is_empty() || text.chars().count() > EXPRESSION_LIMIT {
        return None;
    }
    let mut value = strip(text).to_string();
    if value.contains('/') && !value.contains(' ') {
        value = value.split('/').collect::<Vec<_>>().join(" OR ");
    }
    while value.starts_with('(') && value.ends_with(')') && value.matches('(').count() == 1 {
        let inner: String = value
            .chars()
            .skip(1)
            .take(value.chars().count().saturating_sub(2))
            .collect();
        value = strip(&inner).to_string();
    }
    let tokens = split_whitespace(&value);
    if tokens.is_empty() || tokens.len().is_multiple_of(2) || value.contains(['(', ')']) {
        return None;
    }
    let operators: BTreeSet<String> = tokens
        .iter()
        .skip(1)
        .step_by(2)
        .map(|token| token.to_uppercase())
        .collect();
    if operators
        .iter()
        .any(|operator| operator != "AND" && operator != "OR")
        || operators.len() > 1
    {
        return None;
    }
    let mut ids = Vec::new();
    for item in tokens.iter().step_by(2) {
        ids.push(canonical_id(item)?.to_string());
    }
    if ids.len() == 1 {
        return Some((LicenseForm::Single, ids));
    }
    if ids.iter().collect::<BTreeSet<_>>().len() != ids.len() {
        return None;
    }
    let form = if operators.contains("OR") {
        LicenseForm::Choice
    } else {
        LicenseForm::Conjunction
    };
    Some((form, ids))
}

/// The declared expression of a `Cargo.toml`, `package.json` or `pyproject.toml`, or the problem that keeps it unread.
pub fn declared_from_manifest(path: &str, data: &[u8]) -> (Option<Declared>, Option<String>) {
    if data.len() as u64 > MANIFEST_LIMIT {
        return (
            None,
            Some("the registered manifest is larger than 256 KiB".to_string()),
        );
    }
    let name = path.rsplit('/').next().unwrap_or(path);
    let unreadable = || {
        (
            None,
            Some("the registered manifest could not be read".to_string()),
        )
    };
    let Ok(text) = std::str::from_utf8(data) else {
        return unreadable();
    };
    let (value, field) = if name == "package.json" {
        let Ok(parsed) = serde_json::from_str::<Value>(text) else {
            return unreadable();
        };
        (
            parsed
                .get("license")
                .cloned()
                .filter(|value| !value.is_null()),
            "license",
        )
    } else {
        let Ok(parsed) = text.parse::<toml_edit::DocumentMut>() else {
            return unreadable();
        };
        let (table, field) = if name == "Cargo.toml" {
            ("package", "package.license")
        } else {
            ("project", "project.license")
        };
        let value = parsed
            .get(table)
            .and_then(toml_edit::Item::as_table_like)
            .and_then(|table| table.get("license"));
        (value.and_then(toml_license), field)
    };
    let Some(value) = value else {
        return (
            None,
            Some(format!("the registered manifest has no {field} expression")),
        );
    };
    match value.as_str() {
        Some(text) if !strip(text).is_empty() && text.chars().count() <= EXPRESSION_LIMIT => {
            let declared = Declared {
                path: path.to_string(),
                field: field.to_string(),
                value: strip(text).to_string(),
            };
            (Some(declared), None)
        }
        _ => (
            None,
            Some(format!(
                "{field} in the registered manifest is not a short license expression"
            )),
        ),
    }
}

/// A TOML `license` value as v0.4.0 reads it: a string, or a table holding only `text` (its value); another table
/// counts as absent, and any other value is kept so it is refused as not an expression.
fn toml_license(item: &toml_edit::Item) -> Option<Value> {
    if let Some(table) = item.as_table_like() {
        let keys: Vec<&str> = table.iter().map(|(key, _)| key).collect();
        if keys != ["text"] {
            return None;
        }
        return table
            .get("text")
            .map(|text| text.as_str().map_or(json!(false), |text| json!(text)));
    }
    Some(item.as_str().map_or(json!(false), |text| json!(text)))
}

fn ancestors(path: &str, is_dir: bool) -> Vec<String> {
    let parts: Vec<&str> = if path.is_empty() {
        Vec::new()
    } else {
        path.split('/').collect()
    };
    let upto = if is_dir {
        parts.len()
    } else {
        parts.len().saturating_sub(1)
    };
    let mut folders = vec![String::new()];
    folders.extend((0..upto).map(|index| parts[..=index].join("/")));
    folders
}

fn base_name(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

/// License evidence for a snapshot manifest; `store` keeps every text read and returns its SHA-256 hex. License
/// problems live inside the evidence and make the verdict unknown; they never make the collection unverified.
pub fn discover(
    tree: &dyn Tree,
    paths: &[String],
    manifest_path: Option<&str>,
    store: &mut dyn FnMut(Vec<u8>) -> Result<String, CollectError>,
) -> Result<Value, CollectError> {
    let mut problems: Vec<String> = Vec::new();
    let mut files: BTreeMap<String, Value> = BTreeMap::new();
    let mut notices: BTreeMap<String, Value> = BTreeMap::new();
    let mut scopes = Vec::new();
    let mut evidence_files: usize = 0;
    let mut evidence_bytes: u64 = 0;
    let mut keep = |entry: &Entry,
                    into: &mut BTreeMap<String, Value>,
                    problems: &mut Vec<String>| {
        if into.contains_key(&entry.path) || !matches!(entry.mode.as_str(), "100644" | "100755") {
            return Ok(());
        }
        if !is_object_id(&entry.oid) {
            return Err(CollectError {
                message: format!(
                    "invalid license/notice blob object id {:?} for {}",
                    entry.oid, entry.path
                ),
            });
        }
        let size = tree.size(&entry.oid)?;
        if size > FILE_LIMIT {
            problems.push(format!("{} is larger than 1 MiB", entry.path));
            return Ok(());
        }
        if evidence_files >= EVIDENCE_FILE_LIMIT
            || evidence_bytes.saturating_add(size) > EVIDENCE_BYTES_LIMIT
        {
            return Err(CollectError {
                message: format!(
                    "license and notice evidence exceeded the {EVIDENCE_FILE_LIMIT} file / {EVIDENCE_BYTES_LIMIT} byte budget at {}",
                    entry.path
                ),
            });
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
        evidence_files += 1;
        evidence_bytes = evidence_bytes.saturating_add(actual);
        let sha256 = store(data)?;
        let item = json!({"path": entry.path, "blob": entry.oid, "size": actual, "sha256": sha256});
        into.insert(entry.path.clone(), item);
        Ok::<(), CollectError>(())
    };
    let whole = [String::new()];
    let items = if paths.is_empty() { &whole[..] } else { paths };
    for item in items {
        let entries = tree.entries(item)?;
        let is_dir = item.is_empty() || entries.iter().any(|entry| entry.path != *item);
        let (mut level, mut governing): (Option<String>, Vec<Entry>) = (None, Vec::new());
        for folder in ancestors(item, is_dir) {
            let children = tree.children(&folder)?;
            if children.iter().any(|entry| {
                entry.mode == "040000" && casefold(base_name(&entry.path)) == "licenses"
            }) {
                let place = if folder.is_empty() {
                    "the top folder"
                } else {
                    folder.as_str()
                };
                problems.push(format!(
                    "{place} holds a LICENSES folder (per-file licensing), which oah does not review"
                ));
            }
            let found: Vec<Entry> = children
                .iter()
                .filter(|entry| is_license_name(base_name(&entry.path)))
                .cloned()
                .collect();
            for entry in children
                .iter()
                .filter(|entry| is_notice_name(base_name(&entry.path)))
            {
                keep(entry, &mut notices, &mut problems)?;
            }
            if !found.is_empty() {
                (level, governing) = (Some(folder), found);
            }
        }
        for entry in &governing {
            keep(entry, &mut files, &mut problems)?;
        }
        let mut nested = Vec::new();
        if is_dir {
            if entries.len() > TREE_LIMIT {
                let place = if item.is_empty() {
                    "the repository"
                } else {
                    item.as_str()
                };
                problems.push(format!(
                    "{place} has more than {TREE_LIMIT} files, too many to check for nested licenses"
                ));
            } else {
                let prefix = if item.is_empty() {
                    String::new()
                } else {
                    format!("{}/", item.trim_end_matches('/'))
                };
                for entry in &entries {
                    let deeper = entry
                        .path
                        .strip_prefix(prefix.as_str())
                        .is_some_and(|rest| rest.contains('/'));
                    if !deeper {
                        continue;
                    }
                    if is_license_name(base_name(&entry.path)) {
                        nested.push(entry.path.clone());
                        keep(entry, &mut files, &mut problems)?;
                    } else if is_notice_name(base_name(&entry.path)) {
                        keep(entry, &mut notices, &mut problems)?;
                    }
                }
            }
        }
        let mut governing_paths: Vec<String> =
            governing.iter().map(|entry| entry.path.clone()).collect();
        governing_paths.sort();
        nested.sort();
        scopes.push(
            json!({"path": item, "level": level, "files": governing_paths, "nested": nested}),
        );
    }
    let mut declared = Value::Null;
    let mut manifest = None;
    if let Some(manifest_path) = manifest_path.filter(|path| !path.is_empty()) {
        let found: Vec<Entry> = tree
            .entries(manifest_path)?
            .into_iter()
            .filter(|entry| entry.path == manifest_path)
            .collect();
        match found.first() {
            Some(entry) if matches!(entry.mode.as_str(), "100644" | "100755") => {
                if !is_object_id(&entry.oid) {
                    return Err(CollectError {
                        message: format!(
                            "invalid manifest blob object id {:?} for {manifest_path}",
                            entry.oid
                        ),
                    });
                }
                let size = tree.size(&entry.oid)?;
                if size > MANIFEST_LIMIT {
                    problems.push("the registered manifest is larger than 256 KiB".to_string());
                } else {
                    if evidence_files >= EVIDENCE_FILE_LIMIT
                        || evidence_bytes.saturating_add(size) > EVIDENCE_BYTES_LIMIT
                    {
                        return Err(CollectError {
                            message: format!(
                                "license and notice evidence exceeded the {EVIDENCE_FILE_LIMIT} file / {EVIDENCE_BYTES_LIMIT} byte budget at {manifest_path}"
                            ),
                        });
                    }
                    let data = tree.read(&entry.oid)?;
                    let actual = u64::try_from(data.len()).unwrap_or(u64::MAX);
                    if actual != size || actual > MANIFEST_LIMIT {
                        return Err(CollectError {
                            message: format!(
                                "blob {} for {manifest_path} returned {actual} bytes, expected {size}",
                                entry.oid
                            ),
                        });
                    }
                    let (found, problem) = declared_from_manifest(manifest_path, &data);
                    manifest = Some(
                        json!({"path": manifest_path, "blob": entry.oid, "size": actual, "sha256": store(data)?}),
                    );
                    declared = found.map_or(Value::Null, |found| json!(found));
                    problems.extend(problem);
                }
            }
            _ => problems.push("the registered manifest is missing at this commit".to_string()),
        }
    }
    problems.sort();
    problems.dedup();
    let mut result = json!({
        "scopes": scopes,
        "files": files.into_values().collect::<Vec<_>>(),
        "notices": notices.into_values().collect::<Vec<_>>(),
        "declared": declared,
        "problems": problems,
    });
    if let (Some(manifest), Some(object)) = (manifest, result.as_object_mut()) {
        object.insert("manifest".to_string(), manifest);
    }
    Ok(result)
}

fn unknown(problems: Vec<String>, files: Vec<ShownFile>) -> Verdict {
    Verdict {
        form: LicenseForm::Unknown,
        expression: None,
        ids: Vec::new(),
        strict: false,
        problems,
        files,
    }
}

/// Python's `repr` of a JSON value as `json.loads` returns it (objects in key order).
fn python_repr(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Bool(false)) => "False".to_string(),
        Some(Value::Number(number)) => number_text(number),
        Some(Value::String(text)) => repr(text),
        Some(Value::Array(items)) => {
            format!(
                "[{}]",
                items
                    .iter()
                    .map(|item| python_repr(Some(item)))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
        Some(Value::Object(map)) => format!(
            "{{{}}}",
            map.iter()
                .map(|(key, item)| format!("{}: {}", repr(key), python_repr(Some(item))))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn strings(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map_or_else(Vec::new, |items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_string))
                .collect()
        })
}

/// The license form of one snapshot's evidence; `read(sha256 hex)` returns stored bytes, or `None` when they are gone.
pub fn verdict(evidence: Option<&Value>, read: &dyn Fn(&str) -> Option<Vec<u8>>) -> Verdict {
    let Some(evidence) = evidence.and_then(Value::as_object) else {
        return unknown(
            vec!["no license evidence (documents are not adopted code)".to_string()],
            Vec::new(),
        );
    };
    let text_of = |item: &Map<String, Value>, key: &str| {
        item.get(key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    let mut problems = strings(evidence.get("problems"));
    if let Some(manifest) = evidence
        .get("manifest")
        .filter(|manifest| !manifest.is_null())
    {
        let empty = Map::new();
        let manifest = manifest.as_object().unwrap_or(&empty);
        match read(&text_of(manifest, "sha256")) {
            None => problems.push("the stored package manifest is missing".to_string()),
            Some(data) => {
                let (declared, problem) = declared_from_manifest(&text_of(manifest, "path"), &data);
                let declared = declared.map_or(Value::Null, |declared| json!(declared));
                if declared != *evidence.get("declared").unwrap_or(&Value::Null) {
                    problems.push(
                        "the declared license differs from the original package manifest"
                            .to_string(),
                    );
                }
                problems.extend(problem);
            }
        }
    }
    let mut classified: Vec<(Option<String>, TextVerdict)> = Vec::new();
    let mut shown = Vec::new();
    for item in evidence
        .get("files")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let empty = Map::new();
        let item = item.as_object().unwrap_or(&empty);
        let path = item.get("path").and_then(Value::as_str).map(str::to_string);
        let result = match read(&text_of(item, "sha256")) {
            None => TextVerdict::new(
                None,
                false,
                Some("the stored license text is missing".to_string()),
            ),
            Some(data) => classify_text(&data),
        };
        shown.push(ShownFile {
            path: path.clone(),
            spdx: result.spdx.clone(),
            strict: result.strict,
            problem: result.problem.clone(),
        });
        match classified.iter_mut().find(|(known, _)| *known == path) {
            Some(slot) => slot.1 = result,
            None => classified.push((path, result)),
        }
    }
    if !problems.is_empty() {
        return unknown(problems, shown);
    }
    let lookup = |path: &str| {
        classified
            .iter()
            .find(|(known, _)| known.as_deref() == Some(path))
            .map(|(_, result)| result)
    };
    let mut sets: Vec<BTreeSet<String>> = Vec::new();
    for scope in evidence
        .get("scopes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let governing = strings(scope.get("files"));
        if governing.is_empty() {
            let place = scope
                .get("path")
                .and_then(Value::as_str)
                .filter(|path| !path.is_empty());
            let problem = format!(
                "no license file at or above {}",
                place.unwrap_or("the repository top")
            );
            return unknown(vec![problem], shown);
        }
        let mut ids = BTreeSet::new();
        for path in &governing {
            match lookup(path) {
                Some(TextVerdict {
                    spdx: Some(spdx), ..
                }) => {
                    ids.insert(spdx.clone());
                }
                Some(result) => {
                    let problem = result.problem.clone().unwrap_or_else(|| "None".to_string());
                    return unknown(vec![format!("{path}: {problem}")], shown);
                }
                None => return unknown(vec![format!("{path}: not read")], shown),
            }
        }
        for path in strings(scope.get("nested")) {
            let agrees = lookup(&path)
                .and_then(|result| result.spdx.as_ref())
                .is_some_and(|spdx| ids.contains(spdx));
            if !agrees {
                return unknown(
                    vec![format!(
                        "nested license file {path} does not read as the folder's license"
                    )],
                    shown,
                );
            }
        }
        sets.push(ids);
    }
    let Some(ids) = sets.first().cloned() else {
        return unknown(vec!["no selected scope".to_string()], shown);
    };
    if sets.iter().any(|set| *set != ids) {
        return unknown(
            vec!["the selected paths are under different licenses".to_string()],
            shown,
        );
    }
    let strict = classified
        .iter()
        .filter(|(_, result)| result.spdx.as_ref().is_some_and(|spdx| ids.contains(spdx)))
        .all(|(_, result)| result.strict);
    let declared = evidence
        .get("declared")
        .filter(|declared| !declared.is_null());
    let Some(declared) = declared else {
        if ids.len() != 1 {
            let problem = "several license texts without a declared expression; register the package manifest with \
                           --manifest";
            return unknown(vec![problem.to_string()], shown);
        }
        let only: Vec<String> = ids.into_iter().collect();
        return Verdict {
            form: LicenseForm::Single,
            expression: Some(only[0].clone()),
            ids: only,
            strict,
            problems: Vec::new(),
            files: shown,
        };
    };
    let value = declared.get("value");
    let parsed = value.and_then(Value::as_str).and_then(parse_expression);
    let Some((form, declared_ids)) = parsed else {
        let problem = format!(
            "the declared expression {} is not a plain expression of known licenses",
            python_repr(value)
        );
        return unknown(vec![problem], shown);
    };
    if declared_ids.iter().cloned().collect::<BTreeSet<_>>() != ids {
        let problem = format!(
            "the manifest declares {} but the license files read as {}",
            value.and_then(Value::as_str).unwrap_or(""),
            ids.iter().cloned().collect::<Vec<_>>().join(", ")
        );
        return unknown(vec![problem], shown);
    }
    let joiner = if form == LicenseForm::Choice {
        " OR "
    } else {
        " AND "
    };
    Verdict {
        form,
        expression: Some(declared_ids.join(joiner)),
        ids: declared_ids,
        strict,
        problems: Vec::new(),
        files: shown,
    }
}

/// What the allowed licenses make of a verdict. Pure: the same verdict and policy always give the same answer.
pub fn eligibility(result: &Verdict, allow: &[String]) -> Eligibility {
    let policy: Vec<String> = allow
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let answer = |status, usable_under: Option<&str>, reason: String| Eligibility {
        status,
        usable_under: usable_under.map(str::to_string),
        policy: policy.clone(),
        reason,
    };
    let expression = result
        .expression
        .clone()
        .unwrap_or_else(|| "None".to_string());
    if result.form == LicenseForm::Unknown {
        let reason = if result.problems.is_empty() {
            "license evidence is incomplete".to_string()
        } else {
            result.problems.join("; ")
        };
        return answer(EligibilityStatus::Unknown, None, reason);
    }
    if !result.strict {
        return answer(
            EligibilityStatus::Excluded,
            None,
            format!("{expression} is not a license this policy can allow"),
        );
    }
    let allowed = |id: &String| policy.contains(id);
    match result.form {
        LicenseForm::Single => match result.ids.as_slice() {
            [only] if allowed(only) => answer(
                EligibilityStatus::Eligible,
                Some(only),
                format!("{only} is allowed"),
            ),
            [only] => answer(
                EligibilityStatus::Excluded,
                None,
                format!("{only} is not in the allowed licenses"),
            ),
            _ => answer(
                EligibilityStatus::Unknown,
                None,
                "license evidence is incomplete".to_string(),
            ),
        },
        LicenseForm::Choice => {
            if result.ids.is_empty() {
                answer(
                    EligibilityStatus::Unknown,
                    None,
                    "license evidence is incomplete".to_string(),
                )
            } else {
                match result.ids.iter().find(|id| allowed(id)) {
                    Some(first) => answer(
                        EligibilityStatus::EligibleByChoice,
                        Some(first),
                        format!("{expression} is a choice; usable under {first}"),
                    ),
                    None => answer(
                        EligibilityStatus::Excluded,
                        None,
                        format!("no alternative of {expression} is allowed"),
                    ),
                }
            }
        }
        LicenseForm::Conjunction => {
            if result.ids.is_empty() {
                answer(
                    EligibilityStatus::Unknown,
                    None,
                    "license evidence is incomplete".to_string(),
                )
            } else if result.ids.iter().all(allowed) {
                answer(
                    EligibilityStatus::Eligible,
                    Some(&expression),
                    format!("every part of {expression} is allowed"),
                )
            } else {
                answer(
                    EligibilityStatus::Excluded,
                    None,
                    format!("{expression} needs a license that is not allowed"),
                )
            }
        }
        LicenseForm::Unknown => unreachable!("handled above"),
    }
}
