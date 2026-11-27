// 위키 카드: 설정이 가리키는 폴더나 프로젝트 안 표준 위치·Obsidian 표식을 링크를 따라가지 않고 확인해 상태를 정한다.
//! The Project Wiki card. A set `dir` under `[wiki]` is checked as it is; without one, the standard places and
//! Obsidian markers inside the project are looked for. Folders are inspected one component at a time without
//! following a link or reparse point, and no file's contents are read.

use super::{Card, SETTINGS_FILE, Settings, settings_unreadable, shown};
use crate::dto::{ProjectToolDto, ProjectToolIdDto, ToolPathBaseDto, ToolPathDto};
use crate::fsx::FsxError;
use crate::rules::input::{ExpectedEntry, InspectedEntry, inspect_project_entry};
use std::fs;
use std::io::ErrorKind;
use std::path::Path;

/// Where the source CLI keeps the wiki when `[wiki] dir` is not set (`src/oah/wiki/base.py` `DEFAULTS`).
const DEFAULT_DIR: &str = "docs/wiki";
/// The in-project places a wiki is looked for, the default first.
const STANDARD_DIRS: [&str; 3] = [DEFAULT_DIR, "wiki", "knowledge/wiki"];
/// The folder Obsidian keeps in a vault's root.
const OBSIDIAN_MARKER: &str = ".obsidian";
/// The files and the folder a wiki set up by OAH has (`src/oah/wiki/notes.py`).
const STANDARD_ENTRIES: [(&str, ExpectedEntry); 4] = [
    ("index.md", ExpectedEntry::RegularFile),
    ("log.md", ExpectedEntry::RegularFile),
    ("wiki-guide.md", ExpectedEntry::RegularFile),
    ("templates", ExpectedEntry::Directory),
];
/// How many names of the project root are looked at for Obsidian vaults.
pub(super) const ROOT_SCAN_LIMIT: usize = 256;
/// The longest `dir` value that is checked.
const DIR_LIMIT: usize = 1024;

/// What is at a project-relative folder path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Folder {
    Present,
    Missing,
    /// The folder, or a folder on the way, is a link or reparse point.
    Link,
    /// The path, or a part of it, exists and is not a folder.
    Occupied,
    Denied,
    Unreadable,
}

fn failed(error: &FsxError) -> Folder {
    match error.io_error().map(std::io::Error::kind) {
        Some(ErrorKind::PermissionDenied) => Folder::Denied,
        _ => Folder::Unreadable,
    }
}

/// Inspects `relative` (with `/`, or `.` for the root) under `root` without following anything, then opens the
/// folder for listing so that a folder the user may not read is told apart from one that is there.
fn folder_at(root: &Path, relative: &str) -> Folder {
    let mut path = root.to_path_buf();
    for part in relative.split('/').filter(|part| *part != ".") {
        path.push(part);
        match inspect_project_entry(&path, ExpectedEntry::Directory) {
            Ok(InspectedEntry::Allowed) => {}
            Ok(InspectedEntry::Missing) => return Folder::Missing,
            Ok(InspectedEntry::LinkOrReparse) => return Folder::Link,
            Ok(InspectedEntry::Unsupported) => return Folder::Occupied,
            Err(error) => return failed(&error),
        }
    }
    match fs::read_dir(&path).map(|mut entries| entries.next()) {
        Ok(Some(Err(error))) | Err(error) if error.kind() == ErrorKind::PermissionDenied => {
            Folder::Denied
        }
        Ok(_) => Folder::Present,
        Err(_) => Folder::Unreadable,
    }
}

/// `value` as a project-relative path with `/`: no root, drive, `..` or empty result. `None` when it is not one.
fn relative_dir(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.len() > DIR_LIMIT {
        return None;
    }
    let unified = if cfg!(windows) {
        trimmed.replace('\\', "/")
    } else {
        trimmed.to_string()
    };
    if unified.starts_with('/') {
        return None;
    }
    let mut parts = Vec::new();
    for part in unified.split('/') {
        match part {
            "" | "." => {}
            ".." => return None,
            part if part.contains(':') || part.chars().any(char::is_control) => return None,
            part => parts.push(part),
        }
    }
    Some(if parts.is_empty() {
        String::from(".")
    } else {
        parts.join("/")
    })
}

fn project_path(label: &str, path: &str) -> ToolPathDto {
    ToolPathDto {
        label: label.to_string(),
        path: path.to_string(),
        base: ToolPathBaseDto::Project,
    }
}

/// The standard entries a present wiki folder does not have, or `Err` when the folder cannot be looked into.
fn missing_entries(root: &Path, dir: &str) -> Result<Vec<&'static str>, Folder> {
    let folder = dir
        .split('/')
        .filter(|part| *part != ".")
        .fold(root.to_path_buf(), |path, part| path.join(part));
    let mut missing = Vec::new();
    for (name, expected) in STANDARD_ENTRIES {
        match inspect_project_entry(&folder.join(name), expected) {
            Ok(InspectedEntry::Missing) => missing.push(name),
            Ok(_) => {}
            Err(error) => return Err(failed(&error)),
        }
    }
    Ok(missing)
}

/// The card of a problem with the folder at `dir`.
fn problem(dir: &str, found: Folder, configured: bool) -> Card {
    let correct = if configured {
        format!("correct dir under [wiki] in {SETTINGS_FILE}")
    } else {
        format!("set dir under [wiki] in {SETTINGS_FILE} to the real folder")
    };
    let card = match found {
        Folder::Link => Card::new(
            "link",
            format!("{dir} is a link. OAH reads only real folders inside the project."),
            format!("Replace the link with the folder itself, or {correct}. Then check again."),
        ),
        Folder::Occupied => Card::new(
            "not-folder",
            format!("{dir} exists but is not a folder."),
            format!("Move it aside, or {correct}. Then check again."),
        ),
        Folder::Denied => Card::new(
            "permission-denied",
            format!("OAH is not allowed to read {dir}."),
            "Allow your user account to read the folder, then check again.",
        ),
        _ => Card::new(
            "unreadable",
            format!("{dir} could not be read."),
            "Check that the folder is available, then check again.",
        ),
    };
    card.path(project_path("Wiki folder", dir))
}

/// The card of the one wiki folder in use, with the standard entries it lacks as a note.
fn found(root: &Path, dir: &str, configured: bool) -> Card {
    let missing = match missing_entries(root, dir) {
        Ok(missing) => missing,
        Err(folder) => return problem(dir, folder, configured),
    };
    let status = if configured {
        format!("The project settings point to the wiki folder {dir}.")
    } else {
        format!("Wiki found at {dir}, the default location.")
    };
    let card = Card::new(
        "found",
        status,
        "No action needed. The OAH app can search its text; indexing from the app is not available yet.",
    )
    .path(project_path("Wiki folder", dir));
    if missing.is_empty() {
        card
    } else {
        card.note(format!(
            "Not in this folder: {}. A wiki works without them; the OAH app can add them and leaves what is there.",
            missing.join(", ")
        ))
    }
}

/// The card for a set `dir` value.
fn configured(root: &Path, value: &str) -> Card {
    let Some(dir) = relative_dir(value) else {
        return Card::new(
            "invalid",
            "The wiki folder setting cannot be used.",
            format!(
                "Set dir under [wiki] in {SETTINGS_FILE} to a folder inside the project, such as \
                 {DEFAULT_DIR}. Then check again."
            ),
        )
        .reason(format!(
            "dir is set to \"{}\", which is not a path inside the project.",
            shown(value)
        ));
    };
    match folder_at(root, &dir) {
        Folder::Present => found(root, &dir, true),
        Folder::Missing => Card::new(
            "configured-missing",
            format!("The project settings point to {dir}, but that folder does not exist."),
            format!(
                "Create the folder, or correct dir under [wiki] in {SETTINGS_FILE}. Then check again. \
                 OAH does not create it for you."
            ),
        )
        .path(project_path("Wiki folder", &dir)),
        other => problem(&dir, other, true),
    }
}

/// The top-level folders of `root` that hold an Obsidian marker, sorted, with `.` for the root itself, and whether
/// the root had more names than were looked at. Hidden folders and links are not looked into.
fn obsidian_vaults(root: &Path) -> (Vec<String>, bool) {
    let marked = |folder: &Path| {
        matches!(
            inspect_project_entry(&folder.join(OBSIDIAN_MARKER), ExpectedEntry::Directory),
            Ok(InspectedEntry::Allowed)
        )
    };
    let mut vaults = Vec::new();
    if marked(root) {
        vaults.push(String::from("."));
    }
    let mut names = Vec::new();
    let mut truncated = false;
    if let Ok(entries) = fs::read_dir(root) {
        for (seen, entry) in entries.enumerate() {
            if seen >= ROOT_SCAN_LIMIT {
                truncated = true;
                break;
            }
            let Ok(entry) = entry else { continue };
            // `file_type` does not follow a link, so a linked folder is not a folder here.
            if !entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                continue;
            }
            if let Ok(name) = entry.file_name().into_string()
                && !name.starts_with('.')
            {
                names.push(name);
            }
        }
    }
    names.sort();
    vaults.extend(names.into_iter().filter(|name| marked(&root.join(name))));
    (vaults, truncated)
}

/// Whether `inner` is `outer` or lies below it.
fn within(inner: &str, outer: &str) -> bool {
    outer == "." || inner == outer || inner.starts_with(&format!("{outer}/"))
}

/// The card when `dir` is not set: the standard places and Obsidian vaults that exist.
fn detected(root: &Path) -> Card {
    let mut candidates: Vec<(String, &'static str)> = Vec::new();
    for dir in STANDARD_DIRS {
        match folder_at(root, dir) {
            Folder::Present => candidates.push((dir.to_string(), "Standard location")),
            Folder::Missing => {}
            Folder::Denied => return problem(dir, Folder::Denied, false),
            // Only the default place is the one the tools would use; a link or a file elsewhere is not a wiki.
            other if dir == DEFAULT_DIR => return problem(dir, other, false),
            _ => {}
        }
    }
    let (vaults, truncated) = obsidian_vaults(root);
    // A vault marker on a standard wiki folder, or on a folder above one, marks that same wiki.
    let standard: Vec<String> = candidates.iter().map(|(dir, _)| dir.clone()).collect();
    candidates.extend(
        vaults
            .into_iter()
            .filter(|vault| !standard.iter().any(|dir| within(dir, vault)))
            .map(|vault| (vault, "Obsidian vault")),
    );
    let card = match candidates.as_slice() {
        [] => Card::new(
            "none",
            "No wiki folder found in this project.",
            format!(
                "To start one, create the folder {DEFAULT_DIR} in the project. To use another folder in \
                 the project, set dir under [wiki] in {SETTINGS_FILE}. Then check again."
            ),
        ),
        [(dir, _)] if dir == DEFAULT_DIR => found(root, dir, false),
        [(dir, label)] => Card::new(
            "detected",
            format!("Possible wiki folder found: {dir}. It is not selected yet."),
            format!(
                "To select it, use Set up wiki in the OAH app, or set dir = \"{dir}\" under [wiki] in \
                 {SETTINGS_FILE}. Then check again."
            ),
        )
        .path(project_path(label, dir)),
        several => {
            let mut card = Card::new(
                "ambiguous",
                format!(
                    "Found {} possible wiki folders. None is selected.",
                    several.len()
                ),
                format!(
                    "Choose one and set dir under [wiki] in {SETTINGS_FILE} to its path. Then check \
                     again. OAH changes nothing until you choose."
                ),
            );
            for (dir, label) in several {
                card = card.path(project_path(label, dir));
            }
            if several.iter().any(|(dir, _)| dir == DEFAULT_DIR) {
                card = card.note(format!(
                    "Without a setting, the source CLI uses {DEFAULT_DIR}."
                ));
            }
            card
        }
    };
    if truncated {
        card.note(format!(
            "Only the first {ROOT_SCAN_LIMIT} entries of the project folder were checked for Obsidian vaults."
        ))
    } else {
        card
    }
}

pub(super) fn card(root: &Path, settings: &Settings) -> ProjectToolDto {
    let card = match settings {
        Err(code) => settings_unreadable(code, "the wiki folder"),
        Ok(config) => match super::setting(config.file().values(), "wiki", "dir") {
            super::Setting::Unset => detected(root),
            super::Setting::Set(value) => match value.as_str() {
                Some(dir) => configured(root, dir),
                None => Card::new(
                    "invalid",
                    "The wiki folder setting cannot be used.",
                    format!(
                        "Set dir under [wiki] in {SETTINGS_FILE} to a folder inside the project, such as \
                         {DEFAULT_DIR}. Then check again."
                    ),
                )
                .reason("dir under [wiki] is not text."),
            },
            super::Setting::NotTable => Card::new(
                "invalid",
                "The wiki folder setting cannot be used.",
                format!("Make wiki a table in {SETTINGS_FILE}: a [wiki] line with dir below it. Then check again."),
            )
            .reason("wiki is not a table."),
        },
    };
    card.into_dto(ProjectToolIdDto::Wiki)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_dir_value_is_a_path_inside_the_project_or_refused() {
        assert_eq!(relative_dir("docs/wiki").as_deref(), Some("docs/wiki"));
        assert_eq!(
            relative_dir(" ./docs//wiki/ ").as_deref(),
            Some("docs/wiki")
        );
        assert_eq!(relative_dir(".").as_deref(), Some("."));
        for refused in ["", "  ", "/abs", "../up", "docs/../../up", "C:/x", "a\nb"] {
            assert_eq!(relative_dir(refused), None, "{refused:?}");
        }
        assert_eq!(relative_dir(&"a".repeat(DIR_LIMIT + 1)), None);
    }

    #[test]
    fn a_vault_above_a_standard_folder_is_the_same_wiki() {
        assert!(within("knowledge/wiki", "."));
        assert!(within("knowledge/wiki", "knowledge"));
        assert!(within("wiki", "wiki"));
        assert!(!within("wiki", "wiki-old"));
        assert!(!within("docs/wiki", "notes"));
    }
}
