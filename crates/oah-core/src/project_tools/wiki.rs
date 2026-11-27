// 프로젝트 위키 로컬 작업: 위키 폴더를 제자리에서 찾고, 빠진 표준 파일만 만드는 계획을 미리 보이고 적용하며, 제한된 텍스트 검색을 한다.
//! Project Wiki, the local part: find the wiki folder in place, preview and apply the creation of its missing
//! standard files, and search its Markdown text. No process is started and no network is used.
//!
//! Every call takes the [`ProjectRoot`] the caller selected and names folders relative to it, written with `/`. A
//! folder is opened by handle, one portable component at a time: a symbolic link, a junction, another reparse
//! point, a hard-linked file or a different volume is reported, never followed. Like [`ProjectRoot`] this works
//! on local APFS and on fixed local NTFS; elsewhere every call is [`WikiError::Unsupported`].
//!
//! * [`discover`] lists the folders that look like a wiki, with bounds, and changes nothing.
//! * [`plan_setup`] previews one folder: which of the five standard files are missing and would be created, and
//!   which exist and are kept. [`apply_setup`] surveys the disk again from the same request and writes only when
//!   the result is the plan the caller confirmed, by id. It only ever creates: an existing file is never opened
//!   for writing, so a real wiki's notes, attachments, Obsidian data, index and log stay byte-identical.
//! * [`search`] returns bounded hits: the file relative to the wiki, the line, the heading above it and the line's
//!   text.
//!
//! The folder is the caller's explicit choice, else the folder `[wiki].dir` names, else the one folder
//! [`discover`] finds, else [`DEFAULT_DIR`]. A configured folder that is missing or unsafe is an error to show: it is
//! not created and no other folder is used in its place. This module never reads or writes `.oah/config.toml`;
//! [`WikiSetupPlan::config_dir`] is the separate intent the config writer acts on.

mod copy;

pub use copy::{
    COPY_BYTES_MAX, COPY_DEPTH_MAX, COPY_FILE_BYTES_MAX, COPY_FILES_MAX, CopyAction, CopyBlock,
    CopyBlocked, CopyFile, LinkNote, WikiCopyPlan, WikiCopyReport, WikiCopyRequest, apply_copy,
    plan_copy,
};

use crate::error::Coded;
use crate::fsx::Fingerprint;
use crate::fsx::skill_publish::{Content, EntryKind, Folder, ProjectRoot, PublishError};
use crate::review::identity::{portable_component_key, portable_paths};
use caseless::default_case_fold_str;
use serde::{Deserialize, Serialize};
use std::io;
use unicode_normalization::UnicodeNormalization;

/// The wiki folder of a project that names none and has none.
pub const DEFAULT_DIR: &str = "docs/wiki";
/// The folders a wiki is looked for in, before folders that hold Obsidian data.
pub const STANDARD_DIRS: [&str; 3] = [DEFAULT_DIR, "wiki", "knowledge/wiki"];
/// The standard files, relative to the wiki folder, in the order a plan lists them.
pub const STANDARD_FILES: [&str; 5] = [
    "index.md",
    "log.md",
    "wiki-guide.md",
    "templates/note.md",
    "templates/decision.md",
];

/// A wiki file larger than this is not searched.
pub const SEARCH_FILE_BYTES_MAX: u64 = 1 << 20;
/// One search reads at most this many files and bytes, and goes this many folders deep.
pub const SEARCH_FILES_MAX: usize = 5_000;
pub const SEARCH_BYTES_MAX: u64 = 64 << 20;
pub const SEARCH_DEPTH_MAX: usize = 12;
/// The hits of one search: the default, and the most a caller can ask for.
pub const SEARCH_HITS_DEFAULT: usize = 50;
pub const SEARCH_HITS_MAX: usize = 200;
/// The longest query, context and heading, in characters.
pub const QUERY_CHARS_MAX: usize = 200;
pub const CONTEXT_CHARS_MAX: usize = 240;
pub const HEADING_CHARS_MAX: usize = 120;

const DIR_CHARS_MAX: usize = 300;
const DIR_DEPTH_MAX: usize = 8;
/// Folders a wiki is never placed in or under.
const MANAGED_DIRS: [&str; 6] = [
    ".git",
    ".oah",
    ".obsidian",
    ".claude",
    ".agents",
    "node_modules",
];
const FOLDER_ENTRIES_MAX: usize = 10_000;
const SKIPPED_MAX: usize = 50;
const SCAN_DEPTH_MAX: usize = 3;
const SCAN_FOLDERS_MAX: usize = 400;
const SCAN_ENTRIES_MAX: usize = 2_000;
/// Folders the search for Obsidian data does not enter: build output and installed dependencies.
const SCAN_SKIPPED: [&str; 7] = [
    "node_modules",
    "target",
    "dist",
    "build",
    "vendor",
    "venv",
    "__pycache__",
];
const OBSIDIAN: &str = ".obsidian";
const TEMPLATES: &str = "templates";
const GUIDE_TITLE: &str = "How this wiki works";
const SETUP_VERSION: &str = "oah-wiki-setup/v1";

// The source CLI's templates, so both write the same wiki.
const INDEX_MD: &str = include_str!("../../../../src/oah/templates/wiki/index.md");
const LOG_MD: &str = include_str!("../../../../src/oah/templates/wiki/log.md");
const GUIDE_MD: &str = include_str!("../../../../src/oah/templates/wiki/guide.md");
const NOTE_MD: &str = include_str!("../../../../src/oah/templates/wiki/note.md");
const DECISION_MD: &str = include_str!("../../../../src/oah/templates/wiki/decision.md");

/// A refusal or a file-system problem. Every path in it is relative to the project.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum WikiError {
    /// The folder name is not a portable path inside the project.
    DirInvalid {
        dir: String,
        reason: String,
    },
    /// The folder the configuration or a search names does not exist. It is not created.
    Missing {
        dir: String,
    },
    /// Not a real folder or file of this project: a link, a junction, another reparse point, a name that differs
    /// only by case from another one, or an object that was replaced while it was in use.
    Unsafe {
        path: String,
    },
    Denied {
        path: String,
    },
    /// Not local APFS or fixed local NTFS, or the path leaves the project's volume.
    Unsupported {
        path: String,
    },
    /// More than one folder looks like the wiki and none was chosen.
    Ambiguous {
        candidates: Vec<String>,
    },
    DateInvalid,
    QueryInvalid,
    /// The disk no longer gives the plan that was confirmed.
    Stale,
    /// A standard name is taken by something that is not a regular file or folder, or a copy has entries that
    /// block it.
    Blocked {
        paths: Vec<String>,
    },
    /// The folder to copy and the standard folder are the same folder, or one is inside the other.
    Overlap {
        source: String,
        destination: String,
    },
    Io {
        path: String,
        detail: String,
    },
}

impl Coded for WikiError {
    fn code(&self) -> &'static str {
        match self {
            WikiError::DirInvalid { .. } => "OAH_WIKI_DIR_INVALID",
            WikiError::Missing { .. } => "OAH_WIKI_MISSING",
            WikiError::Unsafe { .. } => "OAH_WIKI_PATH_UNSAFE",
            WikiError::Denied { .. } => "OAH_WIKI_ACCESS_DENIED",
            WikiError::Unsupported { .. } => "OAH_WIKI_FS_UNSUPPORTED",
            WikiError::Ambiguous { .. } => "OAH_WIKI_AMBIGUOUS",
            WikiError::DateInvalid | WikiError::QueryInvalid => "OAH_WIKI_INPUT_INVALID",
            WikiError::Stale => crate::error::codes::PLAN_STALE,
            WikiError::Blocked { .. } => "OAH_WIKI_PLAN_BLOCKED",
            WikiError::Overlap { .. } => "OAH_WIKI_COPY_OVERLAP",
            WikiError::Io { .. } => "OAH_WIKI_IO",
        }
    }

    fn message(&self) -> String {
        match self {
            WikiError::DirInvalid { dir, reason } => {
                format!("the wiki folder {dir:?} cannot be used: {reason}")
            }
            WikiError::Missing { dir } => format!(
                "the wiki folder {dir} does not exist; it was not created and no other folder was used"
            ),
            WikiError::Unsafe { path } => format!(
                "{path} is not a real folder or file of this project (a link, a junction, a name that differs only \
                 by case, or something that changed while it was read); nothing was written through it"
            ),
            WikiError::Denied { path } => format!("no permission to use {path}"),
            WikiError::Unsupported { path } => format!(
                "{path} is not on a local APFS or fixed NTFS volume of this project, where OAH writes safely"
            ),
            WikiError::Ambiguous { candidates } => format!(
                "more than one folder looks like the wiki ({}); choose one",
                candidates.join(", ")
            ),
            WikiError::DateInvalid => "the date must be written as YYYY-MM-DD".to_string(),
            WikiError::QueryInvalid => {
                format!("give one to eight words to search for, up to {QUERY_CHARS_MAX} characters")
            }
            WikiError::Stale => {
                "the wiki folder changed after the plan was made; make a new plan".to_string()
            }
            WikiError::Blocked { paths } => format!(
                "{} cannot be used as it is (the plan says why); resolve it, then make a new plan",
                paths.join(", ")
            ),
            WikiError::Overlap {
                source,
                destination,
            } => format!(
                "{source} and {destination} are the same folder or one is inside the other; nothing to copy"
            ),
            WikiError::Io { path, detail } => format!("cannot use {path}: {detail}"),
        }
    }
}

impl std::fmt::Display for WikiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code(), self.message())
    }
}

impl std::error::Error for WikiError {}

/// What `path` (relative to the project) could not do.
fn fail(path: &str, err: PublishError) -> WikiError {
    let path = path.to_string();
    match err {
        PublishError::UnsafePath | PublishError::Changed => WikiError::Unsafe { path },
        PublishError::Unsupported => WikiError::Unsupported { path },
        PublishError::Occupied => WikiError::Stale,
        PublishError::Io(err) => match err.kind() {
            io::ErrorKind::PermissionDenied => WikiError::Denied { path },
            io::ErrorKind::NotFound => WikiError::Stale,
            _ => WikiError::Io {
                path,
                detail: err.to_string(),
            },
        },
        other => WikiError::Io {
            path,
            detail: other.code().to_string(),
        },
    }
}

fn join(prefix: &str, name: &str) -> String {
    if prefix.is_empty() {
        name.to_string()
    } else {
        format!("{prefix}/{name}")
    }
}

/// The components of a folder name relative to the project. `./` in front and one `/` at the end are accepted.
fn rel_dir(text: &str) -> Result<Vec<String>, WikiError> {
    let invalid = |reason: &str| WikiError::DirInvalid {
        dir: text.chars().filter(|c| !c.is_control()).take(120).collect(),
        reason: reason.to_string(),
    };
    let dir = text.strip_prefix("./").unwrap_or(text);
    let dir = dir.strip_suffix('/').unwrap_or(dir);
    if dir.is_empty() || dir.chars().count() > DIR_CHARS_MAX {
        return Err(invalid(
            "give a folder inside the project, such as docs/wiki",
        ));
    }
    if dir.starts_with('/') || dir.contains(['\\', ':']) {
        return Err(invalid(
            "it must be relative to the project and written with /",
        ));
    }
    let parts: Vec<String> = dir.split('/').map(str::to_string).collect();
    if parts.len() > DIR_DEPTH_MAX
        || portable_paths(std::slice::from_ref(&dir.to_string())).is_err()
    {
        return Err(invalid(
            "a name in it is not usable on every supported system, or it is too deep",
        ));
    }
    if parts
        .iter()
        .any(|part| MANAGED_DIRS.contains(&portable_component_key(part).as_str()))
    {
        return Err(invalid("it is inside a folder that a tool manages"));
    }
    Ok(parts)
}

/// A folder name walked from the project: the spelling on disk of the part that exists, and what is missing.
struct Located {
    /// Every component: as spelled on disk where it exists, as asked for where it does not.
    spelled: Vec<String>,
    /// The deepest folder that exists: the folder itself when nothing is missing.
    deepest: Folder,
    /// How many trailing components do not exist.
    missing: usize,
}

impl Located {
    fn dir(&self) -> String {
        self.spelled.join("/")
    }
}

fn locate(project: &Folder, parts: &[String]) -> Result<Located, WikiError> {
    let mut at = project.clone();
    let mut spelled: Vec<String> = Vec::new();
    for (index, part) in parts.iter().enumerate() {
        let asked = join(&spelled.join("/"), part);
        match at.lookup(part).map_err(|err| fail(&asked, err))? {
            None => {
                spelled.extend(parts[index..].iter().cloned());
                return Ok(Located {
                    spelled,
                    deepest: at,
                    missing: parts.len() - index,
                });
            }
            Some(entry) => {
                let path = join(&spelled.join("/"), &entry.name);
                if entry.kind != EntryKind::Folder {
                    return Err(WikiError::Unsafe { path });
                }
                at = at.folder(&entry.name).map_err(|err| fail(&path, err))?;
                spelled.push(entry.name);
            }
        }
    }
    Ok(Located {
        spelled,
        deepest: at,
        missing: 0,
    })
}

fn project_folder(root: &ProjectRoot) -> Result<Folder, WikiError> {
    root.folder().map_err(|err| fail(".", err))
}

// ---------------------------------------------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------------------------------------------

/// A folder that looks like a wiki.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiCandidate {
    /// Relative to the project, as spelled on disk.
    pub dir: String,
    /// One of [`STANDARD_DIRS`].
    pub standard: bool,
    /// It holds an `.obsidian` folder.
    pub obsidian: bool,
    pub index: bool,
    pub log: bool,
}

/// A standard wiki path that exists and cannot be used, with the reason.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiProblem {
    pub dir: String,
    pub error: WikiError,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiDiscovery {
    /// The standard folders that exist, in the order of [`STANDARD_DIRS`], then other folders that hold Obsidian
    /// data, by name.
    pub candidates: Vec<WikiCandidate>,
    pub problems: Vec<WikiProblem>,
    /// The search for Obsidian data stopped at a bound on folders or names; a folder later in a large project may
    /// be missing from `candidates`. Folders more than three levels below the project are never looked at.
    pub truncated: bool,
}

/// The folders of the project that look like a wiki. Reads folder listings only, with bounds, and writes nothing.
pub fn discover(root: &ProjectRoot) -> Result<WikiDiscovery, WikiError> {
    discover_in(&project_folder(root)?)
}

fn has(folder: &Folder, name: &str, kind: EntryKind) -> bool {
    matches!(folder.lookup(name), Ok(Some(entry)) if entry.kind == kind)
}

fn candidate(dir: String, folder: &Folder, standard: bool) -> WikiCandidate {
    WikiCandidate {
        dir,
        standard,
        obsidian: has(folder, OBSIDIAN, EntryKind::Folder),
        index: has(folder, STANDARD_FILES[0], EntryKind::File),
        log: has(folder, STANDARD_FILES[1], EntryKind::File),
    }
}

fn path_key(dir: &str) -> String {
    dir.split('/')
        .map(portable_component_key)
        .collect::<Vec<_>>()
        .join("/")
}

fn discover_in(project: &Folder) -> Result<WikiDiscovery, WikiError> {
    let mut found = WikiDiscovery {
        candidates: Vec::new(),
        problems: Vec::new(),
        truncated: false,
    };
    for dir in STANDARD_DIRS {
        match locate(project, &rel_dir(dir)?) {
            Ok(located) if located.missing == 0 => {
                found
                    .candidates
                    .push(candidate(located.dir(), &located.deepest, true));
            }
            Ok(_) => {}
            Err(error) => found.problems.push(WikiProblem {
                dir: dir.to_string(),
                error,
            }),
        }
    }
    let mut vaults: Vec<WikiCandidate> = Vec::new();
    let mut queue = std::collections::VecDeque::from([(project.clone(), String::new(), 0usize)]);
    let mut visited = 0usize;
    while let Some((folder, path, depth)) = queue.pop_front() {
        if visited == SCAN_FOLDERS_MAX {
            found.truncated = true;
            break;
        }
        visited += 1;
        // A folder that cannot be listed or entered is not a candidate; the standard paths report their own problems.
        let Ok(listing) = folder.list(SCAN_ENTRIES_MAX) else {
            continue;
        };
        found.truncated |= listing.truncated;
        for entry in listing.entries {
            if entry.kind != EntryKind::Folder {
                continue;
            }
            let key = portable_component_key(&entry.name);
            if key == OBSIDIAN {
                if depth > 0 {
                    vaults.push(candidate(path.clone(), &folder, false));
                }
            } else if !key.starts_with('.')
                && !SCAN_SKIPPED.contains(&key.as_str())
                && depth < SCAN_DEPTH_MAX
                && let Ok(child) = folder.folder(&entry.name)
            {
                queue.push_back((child, join(&path, &entry.name), depth + 1));
            }
        }
    }
    vaults.sort_by(|a, b| a.dir.cmp(&b.dir));
    for vault in vaults {
        let key = path_key(&vault.dir);
        if !found.candidates.iter().any(|c| path_key(&c.dir) == key) {
            found.candidates.push(vault);
        }
    }
    Ok(found)
}

// ---------------------------------------------------------------------------------------------------------------
// Standard files
// ---------------------------------------------------------------------------------------------------------------

/// A packaged template as text with LF line endings, whatever the checkout did to it.
fn packaged(text: &str) -> String {
    text.replace("\r\n", "\n")
}

fn valid_date(today: &str) -> bool {
    let bytes = today.as_bytes();
    let number = |range: std::ops::Range<usize>| -> Option<u32> {
        let digits = &today[range];
        if digits.bytes().all(|b| b.is_ascii_digit()) {
            digits.parse().ok()
        } else {
            None
        }
    };
    bytes.len() == 10
        && today.is_ascii()
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && number(0..4).is_some()
        && number(5..7).is_some_and(|month| (1..=12).contains(&month))
        && number(8..10).is_some_and(|day| (1..=31).contains(&day))
}

/// The source CLI's `render_note`: `{{title}}` becomes a quoted string in the frontmatter and the plain title in
/// the body, and `{{date}}` the date.
fn render_note(template: &str, title: &str, today: &str) -> String {
    let lines: Vec<&str> = template.split('\n').collect();
    let delimiter = |line: &str| {
        line.strip_suffix('\r')
            .unwrap_or(line)
            .trim_end()
            .to_string()
    };
    let opens = lines
        .first()
        .is_some_and(|line| delimiter(line.trim_start_matches('\u{feff}')) == "---");
    let end = if opens {
        (1..lines.len()).find(|&index| matches!(delimiter(lines[index]).as_str(), "---" | "..."))
    } else {
        None
    };
    let text = match end {
        Some(end) => {
            let quoted = serde_json::to_string(title).unwrap_or_else(|_| format!("{title:?}"));
            let mut head = lines[..=end].join("\n");
            for form in ["\"{{title}}\"", "'{{title}}'", "{{title}}"] {
                head = head.replace(form, &quoted);
            }
            let body = lines[end + 1..].join("\n").replace("{{title}}", title);
            format!("{head}\n{body}")
        }
        None => template.replace("{{title}}", title),
    };
    text.replace("{{date}}", today)
}

/// The five standard files of a new wiki and their bytes for `today` (`YYYY-MM-DD`), in the order of
/// [`STANDARD_FILES`]. The same bytes as the source CLI's `oah.wiki.notes.standard_files`.
pub fn standard_files(today: &str) -> Result<Vec<(&'static str, Vec<u8>)>, WikiError> {
    if !valid_date(today) {
        return Err(WikiError::DateInvalid);
    }
    let texts = [
        packaged(INDEX_MD),
        packaged(LOG_MD).replace("{{date}}", today),
        render_note(&packaged(GUIDE_MD), GUIDE_TITLE, today),
        packaged(NOTE_MD),
        packaged(DECISION_MD),
    ];
    Ok(STANDARD_FILES
        .into_iter()
        .zip(texts.into_iter().map(String::into_bytes))
        .collect())
}

// ---------------------------------------------------------------------------------------------------------------
// Setup: preview and apply
// ---------------------------------------------------------------------------------------------------------------

/// What to set up. The same request is given to [`plan_setup`] and to [`apply_setup`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WikiSetupRequest {
    /// `[wiki].dir` when the configuration file gives it; `None` when it does not.
    pub configured: Option<String>,
    /// The folder the user chose, relative to the project. It is the only way a missing folder is created when
    /// the configuration names one.
    pub selected: Option<String>,
    /// The date new files carry, `YYYY-MM-DD`.
    pub today: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SetupMode {
    /// The folder does not exist; it and the standard files would be created.
    Create,
    /// The folder exists; it is used in place and only missing standard files would be created.
    Adopt,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum FileAction {
    Create,
    /// The file exists and is left exactly as it is.
    Keep,
    /// The name is taken by something that is not a regular file; nothing is applied until it is moved.
    Blocked,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PlannedFile {
    /// Relative to the wiki folder, as spelled on disk when it exists.
    pub path: String,
    pub action: FileAction,
    /// SHA-256 of the bytes that would be written, lowercase hex, for `create`.
    pub sha256: Option<String>,
}

/// The preview of one wiki folder. Nothing was written to make it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiSetupPlan {
    /// `sha256:` and 64 hex digits over the project folder, the wiki folder and everything below. [`apply_setup`]
    /// takes it as the confirmation.
    pub id: String,
    /// The wiki folder relative to the project, as spelled on disk where it exists.
    pub dir: String,
    pub mode: SetupMode,
    /// Folders that would be created, relative to the project, parents first.
    pub folders: Vec<String>,
    /// The five standard files, in the order of [`STANDARD_FILES`].
    pub files: Vec<PlannedFile>,
    /// The value `[wiki].dir` should be set to by the config writer, when the file does not already lead here.
    pub config_dir: Option<String>,
}

impl WikiSetupPlan {
    /// How many folders and files applying would create.
    pub fn changes(&self) -> usize {
        self.folders.len()
            + self
                .files
                .iter()
                .filter(|file| file.action == FileAction::Create)
                .count()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ItemStatus {
    Created,
    Kept,
    Failed,
    /// Not reached, because an earlier item stopped the apply.
    Pending,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ApplyItem {
    pub path: String,
    pub status: ItemStatus,
}

/// What an apply did, item by item. Nothing is rolled back: a later plan shows what is left.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiApplyReport {
    pub plan_id: String,
    pub dir: String,
    /// Folders this apply created, relative to the project.
    pub folders_created: Vec<String>,
    pub items: Vec<ApplyItem>,
    /// Why the apply stopped early; `None` when every item is `created` or `kept`.
    pub stop: Option<WikiError>,
    /// [`WikiSetupPlan::config_dir`], given only when the apply completed.
    pub config_dir: Option<String>,
}

struct SetupSurvey {
    plan: WikiSetupPlan,
    located: Located,
    templates: Option<Folder>,
    bytes: Vec<(&'static str, Vec<u8>)>,
}

#[derive(Serialize)]
struct SetupBinding<'a> {
    version: &'static str,
    project: String,
    folder: String,
    templates: Option<String>,
    dir: &'a str,
    mode: SetupMode,
    folders: &'a [String],
    files: &'a [PlannedFile],
    config_dir: &'a Option<String>,
}

/// The preview: which folder, what would be created and what is kept. Reads folder listings only.
pub fn plan_setup(
    root: &ProjectRoot,
    request: &WikiSetupRequest,
) -> Result<WikiSetupPlan, WikiError> {
    survey_setup(root, request).map(|survey| survey.plan)
}

fn survey_setup(root: &ProjectRoot, request: &WikiSetupRequest) -> Result<SetupSurvey, WikiError> {
    let bytes = standard_files(&request.today)?;
    // A configured name that cannot be used stops the plan only when it is the folder asked for.
    let configured = request.configured.as_deref().map(rel_dir);
    let project = project_folder(root)?;
    let located = match (&request.selected, &configured) {
        (Some(selected), _) => locate(&project, &rel_dir(selected)?)?,
        (None, Some(configured)) => {
            let located = locate(&project, configured.as_ref().map_err(Clone::clone)?)?;
            if located.missing > 0 {
                return Err(WikiError::Missing { dir: located.dir() });
            }
            located
        }
        (None, None) => {
            let found = discover_in(&project)?;
            match found.candidates.as_slice() {
                [] => locate(&project, &rel_dir(DEFAULT_DIR)?)?,
                [one] => locate(&project, &rel_dir(&one.dir)?)?,
                many => {
                    return Err(WikiError::Ambiguous {
                        candidates: many.iter().map(|c| c.dir.clone()).collect(),
                    });
                }
            }
        }
    };
    let dir = located.dir();
    let exists = located.missing == 0;
    let state = |folder: Option<&Folder>,
                 name: &str,
                 shown: &str|
     -> Result<(String, FileAction), WikiError> {
        let Some(folder) = folder else {
            return Ok((name.to_string(), FileAction::Create));
        };
        Ok(
            match folder
                .lookup(name)
                .map_err(|err| fail(&join(&dir, shown), err))?
            {
                None => (name.to_string(), FileAction::Create),
                Some(entry) if entry.kind == EntryKind::File => (entry.name, FileAction::Keep),
                Some(entry) => (entry.name, FileAction::Blocked),
            },
        )
    };
    let wiki = exists.then_some(&located.deepest);
    let templates_entry = match wiki {
        Some(wiki) => wiki
            .lookup(TEMPLATES)
            .map_err(|err| fail(&join(&dir, TEMPLATES), err))?,
        None => None,
    };
    let templates_name = templates_entry
        .as_ref()
        .map_or(TEMPLATES, |entry| entry.name.as_str());
    let templates = match &templates_entry {
        Some(entry) if entry.kind == EntryKind::Folder => Some(
            located
                .deepest
                .folder(&entry.name)
                .map_err(|err| fail(&join(&dir, &entry.name), err))?,
        ),
        _ => None,
    };
    // `templates` taken by anything but a real folder blocks both template files.
    let templates_blocked = templates_entry.is_some() && templates.is_none();
    let mut files = Vec::new();
    for (path, data) in &bytes {
        let (name, action) = match path.split_once('/') {
            None => state(wiki, path, path)?,
            Some((_, leaf)) if templates_blocked => {
                (format!("{templates_name}/{leaf}"), FileAction::Blocked)
            }
            Some((_, leaf)) => {
                let (name, action) = state(templates.as_ref(), leaf, path)?;
                (format!("{templates_name}/{name}"), action)
            }
        };
        files.push(PlannedFile {
            path: name,
            action,
            sha256: (action == FileAction::Create).then(|| Fingerprint::of(data).to_hex()),
        });
    }
    let mut folders: Vec<String> = (located.spelled.len() - located.missing..located.spelled.len())
        .map(|index| located.spelled[..=index].join("/"))
        .collect();
    if templates_entry.is_none() {
        folders.push(join(&dir, TEMPLATES));
    }
    let leads_here = match &configured {
        Some(configured) => configured
            .as_ref()
            .is_ok_and(|parts| parts.join("/") == dir),
        None => dir == DEFAULT_DIR,
    };
    let config_dir = (!leads_here).then(|| dir.clone());
    let mode = if exists {
        SetupMode::Adopt
    } else {
        SetupMode::Create
    };
    let stamp = |folder: &Folder, path: &str| folder.stamp().map_err(|err| fail(path, err));
    let binding = SetupBinding {
        version: SETUP_VERSION,
        project: stamp(&project, ".")?,
        folder: stamp(&located.deepest, &dir)?,
        templates: templates
            .as_ref()
            .map(|folder| stamp(folder, &join(&dir, TEMPLATES)))
            .transpose()?,
        dir: &dir,
        mode,
        folders: &folders,
        files: &files,
        config_dir: &config_dir,
    };
    let id = format!(
        "sha256:{}",
        Fingerprint::of(&serde_json::to_vec(&binding).map_err(|err| WikiError::Io {
            path: dir.clone(),
            detail: err.to_string(),
        })?)
        .to_hex()
    );
    Ok(SetupSurvey {
        plan: WikiSetupPlan {
            id,
            dir,
            mode,
            folders,
            files,
            config_dir,
        },
        located,
        templates,
        bytes,
    })
}

/// Applies the plan the caller confirmed. The disk is surveyed again from the same request; when that does not give
/// the plan `confirmed_id` names, nothing is written and the error is [`WikiError::Stale`]. Each file is created
/// complete under a name that must be free; an existing file is never replaced or opened for writing.
///
/// An error before the first write is `Err`. After that the report says what was created and why it stopped.
pub fn apply_setup(
    root: &ProjectRoot,
    request: &WikiSetupRequest,
    confirmed_id: &str,
) -> Result<WikiApplyReport, WikiError> {
    let survey = survey_setup(root, request)?;
    if survey.plan.id != confirmed_id {
        return Err(WikiError::Stale);
    }
    let blocked: Vec<String> = survey
        .plan
        .files
        .iter()
        .filter(|file| file.action == FileAction::Blocked)
        .map(|file| join(&survey.plan.dir, &file.path))
        .collect();
    if !blocked.is_empty() {
        return Err(WikiError::Blocked { paths: blocked });
    }
    let mut report = WikiApplyReport {
        plan_id: survey.plan.id.clone(),
        dir: survey.plan.dir.clone(),
        folders_created: Vec::new(),
        items: survey
            .plan
            .files
            .iter()
            .map(|file| ApplyItem {
                path: file.path.clone(),
                status: match file.action {
                    FileAction::Keep => ItemStatus::Kept,
                    _ => ItemStatus::Pending,
                },
            })
            .collect(),
        stop: None,
        config_dir: None,
    };
    match write_setup(&survey, &mut report) {
        Ok(()) => report.config_dir = survey.plan.config_dir.clone(),
        Err(err) => report.stop = Some(err),
    }
    Ok(report)
}

fn write_setup(survey: &SetupSurvey, report: &mut WikiApplyReport) -> Result<(), WikiError> {
    let plan = &survey.plan;
    let located = &survey.located;
    let mut wiki = located.deepest.clone();
    for index in located.spelled.len() - located.missing..located.spelled.len() {
        let path = located.spelled[..=index].join("/");
        wiki = wiki
            .create_folder(&located.spelled[index])
            .map_err(|err| fail(&path, err))?;
        report.folders_created.push(path);
    }
    let templates = match &survey.templates {
        Some(folder) => folder.clone(),
        None => {
            let path = join(&plan.dir, TEMPLATES);
            let folder = wiki
                .create_folder(TEMPLATES)
                .map_err(|err| fail(&path, err))?;
            report.folders_created.push(path);
            folder
        }
    };
    for (index, (standard, data)) in survey.bytes.iter().enumerate() {
        if plan.files[index].action != FileAction::Create {
            continue;
        }
        let (folder, name) = match standard.split_once('/') {
            None => (&wiki, *standard),
            Some((_, leaf)) => (&templates, leaf),
        };
        let path = join(&plan.dir, standard);
        let written = folder
            .publish_new(name, data)
            .and_then(|()| folder.read(name, data.len() as u64));
        match written {
            Ok(Content::Bytes(read)) if read == *data => {
                report.items[index].status = ItemStatus::Created;
            }
            Ok(_) => {
                report.items[index].status = ItemStatus::Failed;
                return Err(WikiError::Unsafe { path });
            }
            Err(err) => {
                report.items[index].status = ItemStatus::Failed;
                return Err(fail(&path, err));
            }
        }
    }
    wiki.check().map_err(|err| fail(&plan.dir, err))
}

// ---------------------------------------------------------------------------------------------------------------
// Text search
// ---------------------------------------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WikiSearchRequest {
    /// The wiki folder, relative to the project.
    pub dir: String,
    /// One to eight words; a line is a hit when it holds every word, in any case.
    pub query: String,
    /// [`SEARCH_HITS_DEFAULT`] when `None`; never more than [`SEARCH_HITS_MAX`].
    pub max_hits: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiSearchHit {
    /// Relative to the wiki folder, as spelled on disk.
    pub file: String,
    /// Counted from 1.
    pub line: usize,
    /// The nearest heading above the line, without its `#` marks.
    pub heading: Option<String>,
    /// The line, trimmed, at most [`CONTEXT_CHARS_MAX`] characters around the first word found.
    pub context: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SkipReason {
    /// A symbolic link, a junction or another reparse point, a hard-linked file, or another volume.
    Link,
    /// The name cannot be used on every supported system.
    NotPortable,
    /// Larger than [`SEARCH_FILE_BYTES_MAX`].
    Oversized,
    /// Not UTF-8 text.
    NotText,
    Unreadable,
    /// Deeper than [`SEARCH_DEPTH_MAX`].
    TooDeep,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiSkip {
    pub path: String,
    pub reason: SkipReason,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiSearchResult {
    pub dir: String,
    /// In file order (byte order of the path, folder by folder), then line order.
    pub hits: Vec<WikiSearchHit>,
    /// A bound stopped the search or a folder listing: more hits may exist.
    pub truncated: bool,
    pub files_searched: usize,
    /// The first files and folders that were not read, with the reason; `skipped_total` counts all of them.
    pub skipped: Vec<WikiSkip>,
    pub skipped_total: usize,
}

/// Searches the Markdown files of the wiki folder, folder by folder. Hidden names (`.obsidian` among them), folders
/// named `archive` or `archives`, log archives (`log_archive_NN.md`), links and files over the size limit are
/// not read.
pub fn search(
    root: &ProjectRoot,
    request: &WikiSearchRequest,
) -> Result<WikiSearchResult, WikiError> {
    let terms = query_terms(&request.query)?;
    let parts = rel_dir(&request.dir)?;
    let located = locate(&project_folder(root)?, &parts)?;
    if located.missing > 0 {
        return Err(WikiError::Missing { dir: located.dir() });
    }
    let mut run = SearchRun {
        terms,
        max_hits: request
            .max_hits
            .unwrap_or(SEARCH_HITS_DEFAULT)
            .clamp(1, SEARCH_HITS_MAX),
        result: WikiSearchResult {
            dir: located.dir(),
            hits: Vec::new(),
            truncated: false,
            files_searched: 0,
            skipped: Vec::new(),
            skipped_total: 0,
        },
        bytes: 0,
    };
    run.walk(&located.deepest, "", 0);
    Ok(run.result)
}

fn fold(text: &str) -> String {
    default_case_fold_str(&text.nfc().collect::<String>())
}

fn query_terms(query: &str) -> Result<Vec<String>, WikiError> {
    let terms: Vec<String> = query.split_whitespace().map(fold).collect();
    if terms.is_empty()
        || terms.len() > 8
        || query.chars().count() > QUERY_CHARS_MAX
        || query.chars().any(|c| c.is_control() && !c.is_whitespace())
    {
        return Err(WikiError::QueryInvalid);
    }
    Ok(terms)
}

fn portable_name(name: &str) -> bool {
    !name.contains('/') && portable_paths(std::slice::from_ref(&name.to_string())).is_ok()
}

struct SearchRun {
    terms: Vec<String>,
    max_hits: usize,
    result: WikiSearchResult,
    bytes: u64,
}

impl SearchRun {
    fn skip(&mut self, path: String, reason: SkipReason) {
        self.result.skipped_total += 1;
        if self.result.skipped.len() < SKIPPED_MAX {
            self.result.skipped.push(WikiSkip { path, reason });
        }
    }

    fn skip_error(&mut self, path: String, err: &PublishError) {
        let reason = match err {
            PublishError::UnsafePath | PublishError::Changed | PublishError::Unsupported => {
                SkipReason::Link
            }
            _ => SkipReason::Unreadable,
        };
        self.skip(path, reason);
    }

    /// Whether the search goes on.
    fn walk(&mut self, folder: &Folder, prefix: &str, depth: usize) -> bool {
        let listing = match folder.list(FOLDER_ENTRIES_MAX) {
            Ok(listing) => listing,
            Err(err) => {
                let path = if prefix.is_empty() { "." } else { prefix };
                self.skip_error(path.to_string(), &err);
                return true;
            }
        };
        self.result.truncated |= listing.truncated;
        for entry in listing.entries {
            if entry.name.starts_with('.') {
                continue;
            }
            let path = join(prefix, &entry.name);
            let key = portable_component_key(&entry.name);
            match entry.kind {
                EntryKind::Other => self.skip(path, SkipReason::Link),
                EntryKind::Folder if matches!(key.as_str(), "archive" | "archives") => {}
                EntryKind::File if !key.ends_with(".md") || key.starts_with("log_archive_") => {}
                _ if !portable_name(&entry.name) => self.skip(path, SkipReason::NotPortable),
                EntryKind::Folder if depth == SEARCH_DEPTH_MAX => {
                    self.result.truncated = true;
                    self.skip(path, SkipReason::TooDeep);
                }
                EntryKind::Folder => match folder.folder(&entry.name) {
                    Ok(child) => {
                        if !self.walk(&child, &path, depth + 1) {
                            return false;
                        }
                    }
                    Err(err) => self.skip_error(path, &err),
                },
                EntryKind::File => {
                    if self.result.files_searched == SEARCH_FILES_MAX
                        || self.bytes >= SEARCH_BYTES_MAX
                    {
                        self.result.truncated = true;
                        return false;
                    }
                    match folder.read(&entry.name, SEARCH_FILE_BYTES_MAX) {
                        Ok(Content::TooLarge) => self.skip(path, SkipReason::Oversized),
                        Ok(Content::Bytes(bytes)) => {
                            self.result.files_searched += 1;
                            self.bytes += bytes.len() as u64;
                            match String::from_utf8(bytes) {
                                Ok(text) => {
                                    if !self.scan(&path, &text) {
                                        return false;
                                    }
                                }
                                Err(_) => self.skip(path, SkipReason::NotText),
                            }
                        }
                        Err(err) => self.skip_error(path, &err),
                    }
                }
            }
        }
        true
    }

    /// Adds the hits of one file. Whether the search goes on.
    fn scan(&mut self, path: &str, text: &str) -> bool {
        let folded = fold(text);
        if !self.terms.iter().all(|term| folded.contains(term.as_str())) {
            return true;
        }
        let mut heading: Option<String> = None;
        let mut fence: Option<(char, usize)> = None;
        let mut frontmatter = false;
        for (index, raw) in text.split('\n').enumerate() {
            let line = raw.strip_suffix('\r').unwrap_or(raw);
            let line = if index == 0 {
                line.trim_start_matches('\u{feff}')
            } else {
                line
            };
            if index == 0 && line.trim_end() == "---" {
                frontmatter = true;
            } else if frontmatter {
                frontmatter = !matches!(line.trim_end(), "---" | "...");
            } else if !fenced(&mut fence, line)
                && let Some(text) = atx_heading(line)
            {
                heading = Some(text);
            }
            let folded = fold(line);
            if !self.terms.iter().all(|term| folded.contains(term.as_str())) {
                continue;
            }
            if self.result.hits.len() == self.max_hits {
                self.result.truncated = true;
                return false;
            }
            self.result.hits.push(WikiSearchHit {
                file: path.to_string(),
                line: index + 1,
                heading: heading.clone(),
                context: context(line, &self.terms[0]),
            });
        }
        true
    }
}

/// Follows fenced code blocks line by line: whether `line` is a fence delimiter or inside a fence.
fn fenced(fence: &mut Option<(char, usize)>, line: &str) -> bool {
    match (*fence, fence_marker(line)) {
        (None, None) => false,
        (None, opened) => {
            *fence = opened;
            true
        }
        (Some((mark, length)), Some((closing, closing_length)))
            if mark == closing
                && closing_length >= length
                && line.trim().chars().all(|c| c == mark) =>
        {
            *fence = None;
            true
        }
        (Some(_), _) => true,
    }
}

/// The fence character and run length when the line opens or closes a fenced code block.
fn fence_marker(line: &str) -> Option<(char, usize)> {
    let indent = line.len() - line.trim_start_matches(' ').len();
    let rest = &line[indent..];
    let mark = rest.chars().next().filter(|c| matches!(c, '`' | '~'))?;
    let length = rest.chars().take_while(|c| *c == mark).count();
    (indent <= 3 && length >= 3).then_some((mark, length))
}

/// The text of an ATX heading (`#` to `######`), bounded.
fn atx_heading(line: &str) -> Option<String> {
    let indent = line.len() - line.trim_start_matches(' ').len();
    let rest = &line[indent..];
    let level = rest.bytes().take_while(|b| *b == b'#').count();
    let after = &rest[level..];
    if indent > 3
        || !(1..=6).contains(&level)
        || !(after.is_empty() || after.starts_with([' ', '\t']))
    {
        return None;
    }
    let text = after.trim().trim_end_matches('#').trim_end();
    (!text.is_empty()).then(|| bounded(text, 0, HEADING_CHARS_MAX))
}

/// At most `limit` characters of `text` from character `start`, with `…` where it was cut and control characters as
/// spaces.
fn bounded(text: &str, start: usize, limit: usize) -> String {
    let chars: Vec<char> = text
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect();
    if chars.len() <= limit {
        return chars.into_iter().collect();
    }
    let start = start.min(chars.len() - limit);
    let mut out = String::new();
    if start > 0 {
        out.push('…');
    }
    out.extend(&chars[start..start + limit]);
    if start + limit < chars.len() {
        out.push('…');
    }
    out
}

fn context(line: &str, term: &str) -> String {
    let line = line.trim();
    let folded = fold(line);
    let at = folded
        .find(term)
        .map_or(0, |byte| folded[..byte].chars().count());
    bounded(
        line,
        at.saturating_sub(CONTEXT_CHARS_MAX / 3),
        CONTEXT_CHARS_MAX,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn folder_names_are_relative_portable_and_outside_managed_folders() {
        assert_eq!(rel_dir("docs/wiki").unwrap(), ["docs", "wiki"]);
        assert_eq!(rel_dir("./docs/wiki/").unwrap(), ["docs", "wiki"]);
        assert_eq!(rel_dir("문서/위키").unwrap(), ["문서", "위키"]);
        for bad in [
            "",
            ".",
            "..",
            "../wiki",
            "docs/../wiki",
            "/docs/wiki",
            "C:/wiki",
            "docs\\wiki",
            "docs//wiki",
            "docs/wiki.",
            "docs/con",
            ".git/wiki",
            ".oah/wiki",
            "docs/.obsidian",
            "a/b/c/d/e/f/g/h/i",
        ] {
            assert!(
                matches!(rel_dir(bad), Err(WikiError::DirInvalid { .. })),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn dates_are_checked_and_standard_files_carry_them() {
        for bad in [
            "",
            "2026-1-02",
            "2026-13-01",
            "2026-00-10",
            "2026-01-32",
            "２０２６-01-02",
            "2026/01/02",
        ] {
            assert_eq!(standard_files(bad), Err(WikiError::DateInvalid), "{bad:?}");
        }
        let files = standard_files("2026-01-02").unwrap();
        assert_eq!(
            files.iter().map(|(path, _)| *path).collect::<Vec<_>>(),
            STANDARD_FILES
        );
        let text = |index: usize| String::from_utf8(files[index].1.clone()).unwrap();
        assert!(text(1).contains("## 2026-01-02 Wiki created\n"));
        assert!(text(2).starts_with("---\ntitle: \"How this wiki works\"\ndate: 2026-01-02\n"));
        assert!(text(2).contains("\n# How this wiki works\n"));
        assert!(text(3).contains("title: \"{{title}}\"\ndate: {{date}}\n"));
        assert!(files.iter().all(|(_, data)| !data.contains(&b'\r')));
    }

    #[test]
    fn headings_fences_and_context_are_read_like_markdown() {
        assert_eq!(
            atx_heading("## Retry policy ##").as_deref(),
            Some("Retry policy")
        );
        assert_eq!(atx_heading("   # 결정"), Some("결정".to_string()));
        assert_eq!(atx_heading("#tag"), None);
        assert_eq!(atx_heading("    # code"), None);
        assert_eq!(atx_heading("####### seven"), None);
        assert_eq!(fence_marker("```rust"), Some(('`', 3)));
        assert_eq!(fence_marker("~~~~"), Some(('~', 4)));
        assert_eq!(fence_marker("``"), None);
        let long = format!("{}needle{}", "가".repeat(1_000), "나".repeat(1_000));
        let shown = context(&long, "needle");
        assert!(shown.contains("needle") && shown.starts_with('…') && shown.ends_with('…'));
        assert_eq!(shown.chars().count(), CONTEXT_CHARS_MAX + 2);
        assert_eq!(context("  tab\there  ", "here"), "tab here");
    }

    #[test]
    fn queries_are_bounded_words() {
        assert_eq!(query_terms("  Retry   정책 ").unwrap(), ["retry", "정책"]);
        assert_eq!(
            query_terms("STRASSE").unwrap(),
            query_terms("straße").unwrap()
        );
        for bad in [
            "",
            "   ",
            &"a ".repeat(9),
            &"가".repeat(QUERY_CHARS_MAX + 1),
            "a\u{0}b",
        ] {
            assert_eq!(query_terms(bad), Err(WikiError::QueryInvalid), "{bad:?}");
        }
    }
}
