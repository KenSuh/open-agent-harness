// 지시문 표시와 참조: 본문 줄마다 지시문 같은 표현을 데이터로 표시하고, 표시줄 충돌과 상대 참조의 존재를 기록한다.
//! Screening and references (`v0.4:src/oah/discovery/review.py:29-35,105-125`).
//!
//! The body is read as data. [`screening`] flags instruction-like lines, never following them, and notices a line
//! that starts with `<!-- oah` (which makes the skill undeliverable inline). [`references`] lists the relative files
//! the text points at and whether each exists in the reviewed folder.

use super::pattern::Pattern;
use super::pystr::{casefold, lstrip};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

/// At most this many flags and references are kept.
const LIST_LIMIT: usize = 64;

/// The kind of an instruction-like line.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum FlagKind {
    IgnoreInstructions,
    RoleOverride,
    ShellPipe,
    DestructiveShell,
}

impl FlagKind {
    pub fn as_str(self) -> &'static str {
        match self {
            FlagKind::IgnoreInstructions => "ignore-instructions",
            FlagKind::RoleOverride => "role-override",
            FlagKind::ShellPipe => "shell-pipe",
            FlagKind::DestructiveShell => "destructive-shell",
        }
    }
}

/// One flagged line, counted from 1.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScreeningFlag {
    pub line: u32,
    pub kind: FlagKind,
}

/// What screening found.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Screening {
    pub flags: Vec<ScreeningFlag>,
    pub marker_collision: bool,
}

/// A relative file the text points at.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Reference {
    pub path: String,
    pub present: bool,
}

static FLAGS: LazyLock<[(FlagKind, Pattern); 4]> = LazyLock::new(|| {
    [
        (
            FlagKind::IgnoreInstructions,
            Pattern::fixed(
                r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|earlier)\b.{0,20}\b(instructions?|prompts?|rules?)\b",
                true,
            ),
        ),
        (
            FlagKind::RoleOverride,
            Pattern::fixed(r"\byou are now\b|\bnew system prompt\b", true),
        ),
        (
            FlagKind::ShellPipe,
            Pattern::fixed(
                r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b",
                true,
            ),
        ),
        (
            FlagKind::DestructiveShell,
            Pattern::fixed(r"^\s*(\$\s*)?(sudo\s+)?rm\s+-rf\b", true),
        ),
    ]
});

static REFERENCE: LazyLock<Pattern> = LazyLock::new(|| {
    Pattern::fixed(
        concat!(
            r"\]\(([^)\s#]+)\)|`((?:references|scripts|assets|templates|examples)/[^`\s]+)`",
            r"|\b((?:references|scripts|assets|templates|examples)/[A-Za-z0-9._/-]+\.[A-Za-z0-9]+)"
        ),
        false,
    )
});

static SCHEME: LazyLock<Pattern> = LazyLock::new(|| Pattern::fixed(r"[a-z][a-z0-9+.-]*:", false));

/// Instruction-like lines (at most 64 flags, in line order) and the marker collision.
pub fn screening(text: &str) -> Screening {
    screening_with_steps(text).0
}

/// [`screening`] paired with the exact matcher VM step count for deterministic work-bound checks.
pub(crate) fn screening_with_steps(text: &str) -> (Screening, usize) {
    let mut flags = Vec::new();
    let mut steps = 0usize;
    let lines = text.replace("\r\n", "\n");
    'lines: for (index, line) in lines.split('\n').enumerate() {
        let chars: Vec<char> = line.chars().collect();
        for (kind, pattern) in FLAGS.iter() {
            if flags.len() == LIST_LIMIT {
                break 'lines;
            }
            let (hit, used) = pattern.search_steps(&chars, 0);
            steps += used;
            if hit.is_some() {
                let line = u32::try_from(index + 1).unwrap_or(u32::MAX);
                flags.push(ScreeningFlag { line, kind: *kind });
            }
        }
    }
    // Case folding never shortens text, so the first eight characters decide the prefix.
    let marker_collision = text.split('\n').any(|line| {
        let head: String = lstrip(line).chars().take(8).collect();
        casefold(&head).starts_with("<!-- oah")
    });
    (
        Screening {
            flags,
            marker_collision,
        },
        steps,
    )
}

/// The relative references of `text` (at most 64, sorted), each present when `<folder>/<path>` is in `present`.
pub fn references(text: &str, folder: &str, present: &BTreeSet<String>) -> Vec<Reference> {
    references_with_steps(text, folder, present).0
}

/// [`references`] paired with the exact matcher VM step count for deterministic work-bound checks.
pub(crate) fn references_with_steps(
    text: &str,
    folder: &str,
    present: &BTreeSet<String>,
) -> (Vec<Reference>, usize) {
    let chars: Vec<char> = text.chars().collect();
    let mut found = BTreeMap::new();
    let (matches, steps) = REFERENCE.all_steps(&chars);
    for item in matches {
        let Some(reference) = item
            .groups()
            .into_iter()
            .flatten()
            .find(|group| !group.is_empty())
        else {
            continue;
        };
        let reference_chars: Vec<char> = reference.chars().collect();
        if SCHEME.prefix(&reference_chars).is_some()
            || reference.starts_with(['/', '#'])
            || reference.split('/').any(|part| part == "..")
        {
            continue;
        }
        let reference = reference.trim_start_matches(['.', '/']);
        if reference.is_empty() || reference == "SKILL.md" {
            continue;
        }
        let exists = present.contains(&format!("{folder}/{reference}"));
        found.entry(reference.to_string()).or_insert(exists);
    }
    (
        found
            .into_iter()
            .take(LIST_LIMIT)
            .map(|(path, present)| Reference { path, present })
            .collect(),
        steps,
    )
}
