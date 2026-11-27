// 위키 표준 위치 복사: 원본을 그대로 두고 docs/wiki 로 복사하는 계획을 미리 보이고, 확인된 계획만 덮어쓰기 없이 적용한다.
//! Copy an existing wiki to the standard folder, keeping the source.
//!
//! [`plan_copy`] reads every file of the source folder and shows, file by file, what would be copied to
//! [`DEFAULT_DIR`](super::DEFAULT_DIR), what is already there with the same bytes, and everything that blocks the copy:
//! a link, junction or hard-linked file in the source, a name that is not portable or differs only by case from
//! another, a file over the size bounds, a destination file with other bytes, and a relative Markdown link that
//! leaves the wiki folder and would point somewhere else from the new place. [`apply_copy`] surveys again and
//! copies only when that gives the plan the caller confirmed and nothing blocks it. Each file is read again,
//! compared with the planned SHA-256, created under a free name and read back. Nothing is replaced, and the source
//! is never changed or removed. A copy that stopped is finished by a new plan, which lists the files that already
//! landed as `same`.
//!
//! Switching `[wiki].dir` is the config writer's job: [`WikiCopyReport::config_dir`] names the destination only
//! when every file is there and verified.

use super::{
    ApplyItem, ItemStatus, Located, SEARCH_FILE_BYTES_MAX, WikiError, fail, join, locate, path_key,
    portable_name, project_folder, rel_dir,
};
use crate::fsx::Fingerprint;
use crate::fsx::skill_publish::{Content, EntryKind, Folder, ProjectRoot, PublishError};
use crate::review::identity::portable_component_key;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// One copy reads at most this many files and bytes, files up to this size, this many folders deep.
pub const COPY_FILES_MAX: usize = 5_000;
pub const COPY_BYTES_MAX: u64 = 512 << 20;
pub const COPY_FILE_BYTES_MAX: u64 = 64 << 20;
pub const COPY_DEPTH_MAX: usize = 16;

const COPY_VERSION: &str = "oah-wiki-copy/v1";
const FOLDER_ENTRIES_MAX: usize = 10_000;
const LISTED_MAX: usize = 100;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WikiCopyRequest {
    /// The existing wiki folder, relative to the project. The destination is always the standard folder.
    pub source: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum CopyAction {
    Copy,
    /// The destination already holds this file with the same bytes.
    Same,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CopyFile {
    /// Relative to the source folder and to the destination folder.
    pub path: String,
    pub action: CopyAction,
    pub sha256: String,
    pub bytes: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum CopyBlock {
    /// A symbolic link, a junction or another reparse point, a hard-linked file, or another volume.
    Link,
    /// The name cannot be used on every supported system.
    NotPortable,
    /// Two names differ only by case or Unicode form, in the source or between source and destination.
    CaseCollision,
    /// The file, or the whole folder, is over a size bound.
    Oversized,
    TooDeep,
    Unreadable,
    /// The destination holds something else under this name. It is never replaced.
    Conflict,
    /// A relative Markdown link leaves the wiki folder; from the new place it would point somewhere else.
    LinkLeavesWiki,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CopyBlocked {
    /// Relative to the source folder; `.` for the folder as a whole.
    pub path: String,
    pub reason: CopyBlock,
    /// The line and target of the link, for `link-leaves-wiki`.
    pub line: Option<usize>,
    pub target: Option<String>,
}

/// A link that does not resolve inside the source folder today. The copy does not change that.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LinkNote {
    pub file: String,
    pub line: usize,
    pub target: String,
}

/// The preview of a copy. Nothing was written to make it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiCopyPlan {
    /// `sha256:` and 64 hex digits over both folders and everything below. [`apply_copy`] takes it as the
    /// confirmation.
    pub id: String,
    pub source: String,
    pub destination: String,
    /// Folders that would be created, relative to the project, parents first.
    pub folders: Vec<String>,
    pub files: Vec<CopyFile>,
    /// What stops the copy: the first entries, and how many there are. Applying needs none.
    pub blocked: Vec<CopyBlocked>,
    pub blocked_total: usize,
    /// Source files that are not copied: Obsidian's window state (`.obsidian/workspace*`), which the editor rewrites
    /// while it is open and makes again in a new vault.
    pub left: Vec<String>,
    /// Links that already do not resolve inside the source folder: the first entries, and how many there are.
    pub unresolved_links: Vec<LinkNote>,
    pub unresolved_total: usize,
}

impl WikiCopyPlan {
    /// How many files applying would copy.
    pub fn copies(&self) -> usize {
        self.files
            .iter()
            .filter(|file| file.action == CopyAction::Copy)
            .count()
    }
}

/// What a copy did, file by file: `created` for a copied file, `kept` for one that was already the same.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WikiCopyReport {
    pub plan_id: String,
    pub source: String,
    pub destination: String,
    pub folders_created: Vec<String>,
    pub items: Vec<ApplyItem>,
    pub stop: Option<WikiError>,
    /// The destination, for the config writer to set `[wiki].dir` to: given only when the source has files and
    /// every one of them is there and verified.
    pub config_dir: Option<String>,
}

struct CopySurvey {
    plan: WikiCopyPlan,
    source: Located,
    destination: Located,
}

#[derive(Serialize)]
struct CopyBinding<'a> {
    version: &'static str,
    project: String,
    source_folder: String,
    destination_folder: String,
    source: &'a str,
    destination: &'a str,
    folders: &'a [String],
    files: &'a [CopyFile],
    blocked: &'a [CopyBlocked],
    blocked_total: usize,
    left: &'a [String],
}

/// The preview of copying `request.source` to the standard folder. Reads the source and the destination; writes
/// nothing.
pub fn plan_copy(root: &ProjectRoot, request: &WikiCopyRequest) -> Result<WikiCopyPlan, WikiError> {
    survey_copy(root, request).map(|survey| survey.plan)
}

struct Walk {
    files: Vec<CopyFile>,
    blocked: Vec<CopyBlocked>,
    blocked_total: usize,
    left: Vec<String>,
    /// Destination folders, relative to the destination, that do not exist and hold a file to copy.
    folders: BTreeSet<String>,
    /// Every source file and folder by portable key, for resolving links.
    names: BTreeSet<String>,
    links: Vec<(String, Vec<Link>)>,
    bytes: u64,
}

impl Walk {
    fn block(&mut self, path: &str, reason: CopyBlock) {
        self.block_link(path, reason, None, None);
    }

    fn block_link(
        &mut self,
        path: &str,
        reason: CopyBlock,
        line: Option<usize>,
        target: Option<String>,
    ) {
        self.blocked_total += 1;
        if self.blocked.len() < LISTED_MAX {
            self.blocked.push(CopyBlocked {
                path: path.to_string(),
                reason,
                line,
                target,
            });
        }
    }

    fn block_error(&mut self, path: &str, err: &PublishError) {
        let reason = match err {
            PublishError::UnsafePath | PublishError::Changed | PublishError::Unsupported => {
                CopyBlock::Link
            }
            _ => CopyBlock::Unreadable,
        };
        self.block(path, reason);
    }

    /// Reads one source folder beside the destination folder of the same name, when that exists. `missing` holds
    /// the destination folders above this one that do not exist. Whether the walk goes on.
    fn folder(
        &mut self,
        source: &Folder,
        destination: Option<&Folder>,
        prefix: &str,
        missing: &mut Vec<String>,
        depth: usize,
    ) -> bool {
        let listing = match source.list(FOLDER_ENTRIES_MAX) {
            Ok(listing) if !listing.truncated => listing,
            Ok(_) => {
                self.block(
                    if prefix.is_empty() { "." } else { prefix },
                    CopyBlock::Oversized,
                );
                return true;
            }
            Err(err) => {
                self.block_error(if prefix.is_empty() { "." } else { prefix }, &err);
                return true;
            }
        };
        let mut keys: BTreeMap<String, usize> = BTreeMap::new();
        for entry in &listing.entries {
            *keys.entry(portable_component_key(&entry.name)).or_default() += 1;
        }
        for entry in listing.entries {
            let path = join(prefix, &entry.name);
            let key = portable_component_key(&entry.name);
            if entry.kind == EntryKind::Other {
                self.block(&path, CopyBlock::Link);
            } else if !portable_name(&entry.name) {
                self.block(&path, CopyBlock::NotPortable);
            } else if keys[&key] > 1 {
                self.block(&path, CopyBlock::CaseCollision);
            } else if entry.kind == EntryKind::Folder {
                if depth == COPY_DEPTH_MAX {
                    self.block(&path, CopyBlock::TooDeep);
                    continue;
                }
                let child = match source.folder(&entry.name) {
                    Ok(child) => child,
                    Err(err) => {
                        self.block_error(&path, &err);
                        continue;
                    }
                };
                self.names.insert(path_key(&path));
                let beside = match destination.map(|folder| folder.lookup(&entry.name)) {
                    None | Some(Ok(None)) => None,
                    Some(Ok(Some(found))) if found.name != entry.name => {
                        self.block(&path, CopyBlock::CaseCollision);
                        continue;
                    }
                    Some(Ok(Some(found))) if found.kind == EntryKind::Folder => {
                        match destination.map(|folder| folder.folder(&found.name)) {
                            Some(Ok(folder)) => Some(folder),
                            _ => {
                                self.block(&path, CopyBlock::Conflict);
                                continue;
                            }
                        }
                    }
                    Some(_) => {
                        self.block(&path, CopyBlock::Conflict);
                        continue;
                    }
                };
                let pushed = beside.is_none();
                if pushed {
                    missing.push(path.clone());
                }
                let goes_on = self.folder(&child, beside.as_ref(), &path, missing, depth + 1);
                if pushed {
                    missing.pop();
                }
                if !goes_on {
                    return false;
                }
            } else if !self.file(source, destination, prefix, &entry.name, missing) {
                return false;
            }
        }
        true
    }

    /// Whether the walk goes on.
    fn file(
        &mut self,
        source: &Folder,
        destination: Option<&Folder>,
        prefix: &str,
        name: &str,
        missing: &[String],
    ) -> bool {
        let path = join(prefix, name);
        let in_obsidian = prefix
            .rsplit('/')
            .next()
            .is_some_and(|folder| portable_component_key(folder) == ".obsidian");
        if in_obsidian && portable_component_key(name).starts_with("workspace") {
            self.left.push(path);
            return true;
        }
        if self.files.len() == COPY_FILES_MAX {
            self.block(".", CopyBlock::Oversized);
            return false;
        }
        let data = match source.read(name, COPY_FILE_BYTES_MAX) {
            Ok(Content::Bytes(data)) => data,
            Ok(Content::TooLarge) => {
                self.block(&path, CopyBlock::Oversized);
                return true;
            }
            Err(err) => {
                self.block_error(&path, &err);
                return true;
            }
        };
        self.bytes += data.len() as u64;
        if self.bytes > COPY_BYTES_MAX {
            self.block(".", CopyBlock::Oversized);
            return false;
        }
        self.names.insert(path_key(&path));
        let action = match destination.map(|folder| folder.lookup(name)) {
            None | Some(Ok(None)) => CopyAction::Copy,
            Some(Ok(Some(found))) if found.name != name => {
                self.block(&path, CopyBlock::CaseCollision);
                return true;
            }
            Some(Ok(Some(found))) if found.kind == EntryKind::File => {
                match destination.map(|folder| folder.read(name, data.len() as u64)) {
                    Some(Ok(Content::Bytes(there))) if there == data => CopyAction::Same,
                    _ => {
                        self.block(&path, CopyBlock::Conflict);
                        return true;
                    }
                }
            }
            Some(_) => {
                self.block(&path, CopyBlock::Conflict);
                return true;
            }
        };
        if action == CopyAction::Copy {
            self.folders.extend(missing.iter().cloned());
        }
        if portable_component_key(name).ends_with(".md")
            && data.len() as u64 <= SEARCH_FILE_BYTES_MAX
            && let Ok(text) = std::str::from_utf8(&data)
        {
            let links = links_of(text);
            if !links.is_empty() {
                self.links.push((path.clone(), links));
            }
        }
        self.files.push(CopyFile {
            path,
            action,
            sha256: Fingerprint::of(&data).to_hex(),
            bytes: data.len() as u64,
        });
        true
    }
}

fn survey_copy(root: &ProjectRoot, request: &WikiCopyRequest) -> Result<CopySurvey, WikiError> {
    let project = project_folder(root)?;
    let source = locate(&project, &rel_dir(&request.source)?)?;
    if source.missing > 0 {
        return Err(WikiError::Missing { dir: source.dir() });
    }
    let destination = locate(&project, &rel_dir(super::DEFAULT_DIR)?)?;
    let (from, to) = (source.dir(), destination.dir());
    let stamp = |folder: &Folder, path: &str| folder.stamp().map_err(|err| fail(path, err));
    let (source_stamp, destination_stamp) = (
        stamp(&source.deepest, &from)?,
        stamp(&destination.deepest, &to)?,
    );
    let (from_key, to_key) = (path_key(&from), path_key(&to));
    // No link is followed on either side, so two names for one folder can only be the same or nested names.
    if from_key == to_key
        || to_key.starts_with(&format!("{from_key}/"))
        || from_key.starts_with(&format!("{to_key}/"))
        || (destination.missing == 0 && source_stamp == destination_stamp)
    {
        return Err(WikiError::Overlap {
            source: from,
            destination: to,
        });
    }
    let mut walk = Walk {
        files: Vec::new(),
        blocked: Vec::new(),
        blocked_total: 0,
        left: Vec::new(),
        folders: BTreeSet::new(),
        names: BTreeSet::new(),
        links: Vec::new(),
        bytes: 0,
    };
    let exists = destination.missing == 0;
    walk.folder(
        &source.deepest,
        exists.then_some(&destination.deepest),
        "",
        &mut Vec::new(),
        0,
    );
    let mut unresolved = Vec::new();
    let mut unresolved_total = 0;
    for (file, links) in std::mem::take(&mut walk.links) {
        for link in links {
            match resolve(&walk.names, &file, &link) {
                Resolved::Inside => {}
                Resolved::Leaves => walk.block_link(
                    &file,
                    CopyBlock::LinkLeavesWiki,
                    Some(link.line),
                    Some(link.target),
                ),
                Resolved::Nowhere => {
                    unresolved_total += 1;
                    if unresolved.len() < LISTED_MAX {
                        unresolved.push(LinkNote {
                            file: file.clone(),
                            line: link.line,
                            target: link.target,
                        });
                    }
                }
            }
        }
    }
    let copies = walk
        .files
        .iter()
        .any(|file| file.action == CopyAction::Copy);
    let mut folders: Vec<String> = Vec::new();
    if copies {
        folders.extend(
            (destination.spelled.len() - destination.missing..destination.spelled.len())
                .map(|index| destination.spelled[..=index].join("/")),
        );
    }
    folders.extend(walk.folders.iter().map(|folder| join(&to, folder)));
    let binding = CopyBinding {
        version: COPY_VERSION,
        project: stamp(&project, ".")?,
        source_folder: source_stamp,
        destination_folder: destination_stamp,
        source: &from,
        destination: &to,
        folders: &folders,
        files: &walk.files,
        blocked: &walk.blocked,
        blocked_total: walk.blocked_total,
        left: &walk.left,
    };
    let id = format!(
        "sha256:{}",
        Fingerprint::of(&serde_json::to_vec(&binding).map_err(|err| WikiError::Io {
            path: from.clone(),
            detail: err.to_string(),
        })?)
        .to_hex()
    );
    Ok(CopySurvey {
        plan: WikiCopyPlan {
            id,
            source: from,
            destination: to,
            folders,
            files: walk.files,
            blocked: walk.blocked,
            blocked_total: walk.blocked_total,
            left: walk.left,
            unresolved_links: unresolved,
            unresolved_total,
        },
        source,
        destination,
    })
}

/// Copies what the confirmed plan lists. The folders are surveyed again from the same request; when that does not
/// give the plan `confirmed_id` names, nothing is written ([`WikiError::Stale`]), and a plan with anything blocked
/// is refused ([`WikiError::Blocked`]). The source is only read.
///
/// An error before the first write is `Err`. After that the report says what was copied and why it stopped.
pub fn apply_copy(
    root: &ProjectRoot,
    request: &WikiCopyRequest,
    confirmed_id: &str,
) -> Result<WikiCopyReport, WikiError> {
    let survey = survey_copy(root, request)?;
    let plan = &survey.plan;
    if plan.id != confirmed_id {
        return Err(WikiError::Stale);
    }
    if plan.blocked_total > 0 {
        return Err(WikiError::Blocked {
            paths: plan
                .blocked
                .iter()
                .map(|blocked| join(&plan.source, &blocked.path))
                .collect(),
        });
    }
    let mut report = WikiCopyReport {
        plan_id: plan.id.clone(),
        source: plan.source.clone(),
        destination: plan.destination.clone(),
        folders_created: Vec::new(),
        items: plan
            .files
            .iter()
            .map(|file| ApplyItem {
                path: file.path.clone(),
                status: match file.action {
                    CopyAction::Same => ItemStatus::Kept,
                    CopyAction::Copy => ItemStatus::Pending,
                },
            })
            .collect(),
        stop: None,
        config_dir: None,
    };
    match write_copy(&survey, &mut report) {
        // A source without a file to copy leaves no wiki at the destination to switch to.
        Ok(()) => report.config_dir = (!plan.files.is_empty()).then(|| plan.destination.clone()),
        Err(err) => report.stop = Some(err),
    }
    Ok(report)
}

fn write_copy(survey: &CopySurvey, report: &mut WikiCopyReport) -> Result<(), WikiError> {
    let plan = &survey.plan;
    if plan.copies() == 0 {
        return Ok(());
    }
    let created: BTreeSet<&str> = plan.folders.iter().map(String::as_str).collect();
    let located = &survey.destination;
    let mut root = located.deepest.clone();
    for index in located.spelled.len() - located.missing..located.spelled.len() {
        let path = located.spelled[..=index].join("/");
        root = root
            .create_folder(&located.spelled[index])
            .map_err(|err| fail(&path, err))?;
        report.folders_created.push(path);
    }
    // The folders opened so far on both sides, by path relative to the wiki.
    let mut sources: BTreeMap<String, Folder> =
        BTreeMap::from([(String::new(), survey.source.deepest.clone())]);
    let mut destinations: BTreeMap<String, Folder> =
        BTreeMap::from([(String::new(), root.clone())]);
    for (index, file) in plan.files.iter().enumerate() {
        if file.action != CopyAction::Copy {
            continue;
        }
        let (folder, name) = file.path.rsplit_once('/').unwrap_or(("", &file.path));
        let from = join(&plan.source, &file.path);
        let to = join(&plan.destination, &file.path);
        let copied = (|| -> Result<(), WikiError> {
            let mut walked = String::new();
            for part in folder.split('/').filter(|part| !part.is_empty()) {
                let parent = walked.clone();
                walked = join(&walked, part);
                if !sources.contains_key(&walked) {
                    let opened = sources[&parent]
                        .folder(part)
                        .map_err(|err| fail(&join(&plan.source, &walked), err))?;
                    sources.insert(walked.clone(), opened);
                }
                if !destinations.contains_key(&walked) {
                    let path = join(&plan.destination, &walked);
                    let opened = if created.contains(path.as_str()) {
                        let made = destinations[&parent].create_folder(part);
                        if made.is_ok() {
                            report.folders_created.push(path.clone());
                        }
                        made
                    } else {
                        destinations[&parent].folder(part)
                    }
                    .map_err(|err| fail(&path, err))?;
                    destinations.insert(walked.clone(), opened);
                }
            }
            let data = match sources[folder].read(name, file.bytes) {
                Ok(Content::Bytes(data)) if Fingerprint::of(&data).to_hex() == file.sha256 => data,
                Ok(_) => return Err(WikiError::Stale),
                Err(err) => return Err(fail(&from, err)),
            };
            let target = &destinations[folder];
            target
                .publish_new(name, &data)
                .map_err(|err| fail(&to, err))?;
            match target.read(name, file.bytes) {
                Ok(Content::Bytes(read)) if read == data => Ok(()),
                Ok(_) => Err(WikiError::Unsafe { path: to.clone() }),
                Err(err) => Err(fail(&to, err)),
            }
        })();
        match copied {
            Ok(()) => report.items[index].status = ItemStatus::Created,
            Err(err) => {
                report.items[index].status = ItemStatus::Failed;
                return Err(err);
            }
        }
    }
    root.check().map_err(|err| fail(&plan.destination, err))
}

// ---------------------------------------------------------------------------------------------------------------
// Links
// ---------------------------------------------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
struct Link {
    line: usize,
    target: String,
    /// `[[name]]`: found by name anywhere in the vault. Otherwise a Markdown path relative to the note.
    wiki: bool,
}

enum Resolved {
    Inside,
    /// A relative path that climbs out of the wiki folder.
    Leaves,
    Nowhere,
}

/// The links of a note, outside its frontmatter, fenced code blocks and inline code.
fn links_of(text: &str) -> Vec<Link> {
    let mut links = Vec::new();
    let mut fence: Option<(char, usize)> = None;
    let mut frontmatter = false;
    for (index, raw) in text.split('\n').enumerate() {
        let line = raw.strip_suffix('\r').unwrap_or(raw);
        if index == 0 && line.trim_start_matches('\u{feff}').trim_end() == "---" {
            frontmatter = true;
            continue;
        }
        if frontmatter {
            frontmatter = !matches!(line.trim_end(), "---" | "...");
            continue;
        }
        if super::fenced(&mut fence, line) {
            continue;
        }
        // Inline code is text about links, not a link.
        let prose: String = line
            .split('`')
            .enumerate()
            .filter(|(part, _)| part % 2 == 0)
            .map(|(_, text)| text)
            .collect::<Vec<_>>()
            .join(" ");
        let mut rest = prose.as_str();
        while let Some(start) = rest.find("[[") {
            let Some(end) = rest[start + 2..].find("]]") else {
                break;
            };
            let inner = &rest[start + 2..start + 2 + end];
            let target = inner.split(['|', '#']).next().unwrap_or("").trim();
            if !target.is_empty() {
                links.push(Link {
                    line: index + 1,
                    target: target.to_string(),
                    wiki: true,
                });
            }
            rest = &rest[start + 2 + end + 2..];
        }
        let mut rest = prose.as_str();
        while let Some(start) = rest.find("](") {
            rest = &rest[start + 2..];
            if let Some(target) = markdown_target(rest) {
                links.push(Link {
                    line: index + 1,
                    target,
                    wiki: false,
                });
            }
        }
        // A reference definition: \`[label]: target\`.
        let trimmed = prose.trim_start();
        if prose.len() - trimmed.len() <= 3
            && trimmed.starts_with('[')
            && let Some((label, target)) = trimmed.split_once("]:")
            && !label.contains(']')
            && let Some(target) = markdown_target(target.trim_start())
        {
            links.push(Link {
                line: index + 1,
                target,
                wiki: false,
            });
        }
    }
    links
}

/// The file a Markdown link destination names, when it is a path and not an address, a fragment or nothing.
fn markdown_target(text: &str) -> Option<String> {
    let raw = match text.strip_prefix('<') {
        Some(angled) => angled.split('>').next()?,
        None => text.split(|c: char| c == ')' || c.is_whitespace()).next()?,
    };
    let path = raw.split(['#', '?']).next()?;
    let scheme = path.split_once(':').is_some_and(|(scheme, _)| {
        scheme.len() > 1
            && scheme.starts_with(|c: char| c.is_ascii_alphabetic())
            && scheme
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '.' | '-'))
    });
    if path.is_empty() || scheme || path.starts_with("//") {
        return None;
    }
    Some(percent_decoded(path))
}

fn percent_decoded(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let hex = (bytes[index] == b'%')
            .then(|| text.get(index + 1..index + 3))
            .flatten()
            .and_then(|digits| u8::from_str_radix(digits, 16).ok());
        match hex {
            Some(byte) => {
                out.push(byte);
                index += 3;
            }
            None => {
                out.push(bytes[index]);
                index += 1;
            }
        }
    }
    String::from_utf8(out).unwrap_or_else(|_| text.to_string())
}

fn resolve(names: &BTreeSet<String>, file: &str, link: &Link) -> Resolved {
    let folder = file.rsplit_once('/').map_or("", |(folder, _)| folder);
    let known = |path: &str| {
        let key = path_key(path);
        names.contains(&key) || names.contains(&format!("{key}.md"))
    };
    if link.wiki {
        let target = link.target.trim_matches('/');
        let key = path_key(target);
        let by_name = names.iter().any(|name| {
            [key.clone(), format!("{key}.md")]
                .iter()
                .any(|wanted| name == wanted || name.ends_with(&format!("/{wanted}")))
        });
        return if by_name || known(&join(folder, target)) {
            Resolved::Inside
        } else {
            Resolved::Nowhere
        };
    }
    // A leading \`/\` is the vault root in Obsidian; other paths start at the note's folder.
    let (base, path) = match link.target.strip_prefix('/') {
        Some(path) => ("", path),
        None => (folder, link.target.as_str()),
    };
    let mut parts: Vec<&str> = base.split('/').filter(|part| !part.is_empty()).collect();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.pop().is_none() {
                    return Resolved::Leaves;
                }
            }
            part => parts.push(part),
        }
    }
    if parts.is_empty() || known(&parts.join("/")) {
        Resolved::Inside
    } else {
        Resolved::Nowhere
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn targets(text: &str) -> Vec<(usize, &'static str, String)> {
        links_of(text)
            .into_iter()
            .map(|link| {
                (
                    link.line,
                    if link.wiki { "wiki" } else { "path" },
                    link.target,
                )
            })
            .collect()
    }

    #[test]
    fn links_are_read_outside_frontmatter_fences_and_inline_code() {
        let text = [
            "---",
            "sources: [\"[[not-a-link]]\"]",
            "---",
            "See [[retry-policy|the policy]] and [[decisions/2026/cache#Context]] and [[#here]].",
            "![[diagram.png]] with [a file](../src/app%20one.py#L3) and <https://example.com>.",
            "[site](https://example.com/a) [mail](mailto:a@example.com) [frag](#top) [abs](/raw/doc.pdf)",
            "Write `[[like-this]]` or `[x](y.md)` for a link.",
            "```",
            "[[fenced]] [x](fenced.md)",
            "```",
            "[angled](<attachments/my file.png> \"title\")",
            "[ref]: ../../outside.md",
        ]
        .join("\n");
        assert_eq!(
            targets(&text),
            [
                (4, "wiki", "retry-policy".to_string()),
                (4, "wiki", "decisions/2026/cache".to_string()),
                (5, "wiki", "diagram.png".to_string()),
                (5, "path", "../src/app one.py".to_string()),
                (6, "path", "/raw/doc.pdf".to_string()),
                (11, "path", "attachments/my file.png".to_string()),
                (12, "path", "../../outside.md".to_string()),
            ]
        );
    }

    #[test]
    fn links_resolve_inside_the_wiki_leave_it_or_go_nowhere() {
        let names: BTreeSet<String> = [
            "index.md",
            "decisions",
            "decisions/2026",
            "decisions/2026/retry-policy.md",
            "attachments",
            "attachments/diagram.png",
        ]
        .into_iter()
        .map(String::from)
        .collect();
        let link = |target: &str, wiki: bool| Link {
            line: 1,
            target: target.to_string(),
            wiki,
        };
        let from = "decisions/2026/retry-policy.md";
        let inside = |target: &str, wiki: bool| {
            matches!(resolve(&names, from, &link(target, wiki)), Resolved::Inside)
        };
        let leaves = |target: &str| {
            matches!(
                resolve(&names, from, &link(target, false)),
                Resolved::Leaves
            )
        };
        let nowhere = |target: &str, wiki: bool| {
            matches!(
                resolve(&names, from, &link(target, wiki)),
                Resolved::Nowhere
            )
        };
        assert!(
            inside("Retry-Policy", true) && inside("index", true) && inside("diagram.png", true)
        );
        assert!(inside("2026/retry-policy", true) && nowhere("missing-note", true));
        assert!(inside("../../index.md", false) && inside("../../attachments/diagram.png", false));
        assert!(
            inside("/attachments/diagram.png", false)
                && inside("../..", false)
                && inside("../", false)
        );
        assert!(
            leaves("../../../README.md") && leaves("../../../src/app.py") && leaves("/../x.md")
        );
        assert!(nowhere("../../gone.md", false) && nowhere("sibling.md", false));
    }
}
