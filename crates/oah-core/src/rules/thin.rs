// 얇은 파일 계획: CLAUDE.md·GEMINI.md 의 @AGENTS.md 한 줄과 .cursor/rules/oah.mdc 를 C12 자격표대로 순수하게 계획한다.
//! Thin project files (design §3.4, CONTRACTS C12, D-33). They are project files, never agent-home files, so their
//! eligibility takes plain detection input from the caller and no `InstalledAgent` witness:
//!
//! | File | Planned when |
//! |---|---|
//! | `CLAUDE.md` | always, with the block ([`super::plan`]) |
//! | `GEMINI.md` | Gemini CLI is detected **or** the file already exists in the project root |
//! | `.cursor/rules/oah.mdc` | Cursor is detected; an existing `.mdc` without Cursor is left alone |
//!
//! Every path here is a fixed project-relative path. Nothing removes a thin file.

use super::error::{RulesError, RulesErrorKind as Kind};
use super::input::ProjectFile;
use super::plan::decode;
use super::text::{eol_of, py_strip, split_lines, strip_eol};
use crate::config::AgentId;

/// The import line Claude Code and Gemini CLI read `AGENTS.md` through.
pub const IMPORT: &str = "@AGENTS.md";
pub const GEMINI_MD: &str = "GEMINI.md";
pub const CURSOR_MDC: &str = ".cursor/rules/oah.mdc";
/// The Cursor project rule, with its front matter (design §3.4).
pub const MDC_TEXT: &str = "---\ndescription: OAH team workflow\nalwaysApply: true\n---\nFollow the rules in \
                            AGENTS.md at the repository root. That file is managed by OAH.\n";

/// Why a thin file is planned.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ThinFileTrigger {
    /// The agent that reads the file is detected as installed on this machine (a read by the caller).
    AgentDetected,
    /// The file already exists in the project (the approved project-file exception, `GEMINI.md` only).
    FileExists,
}

/// `GEMINI.md`: Gemini CLI detected, or the file exists.
pub fn gemini_trigger(detected: &[AgentId], file: &ProjectFile) -> Option<ThinFileTrigger> {
    if detected.contains(&AgentId::Gemini) {
        Some(ThinFileTrigger::AgentDetected)
    } else {
        file.exists().then_some(ThinFileTrigger::FileExists)
    }
}

/// `.cursor/rules/oah.mdc`: Cursor detected; an existing file never makes it eligible.
pub fn cursor_trigger(detected: &[AgentId]) -> Option<ThinFileTrigger> {
    detected
        .contains(&AgentId::Cursor)
        .then_some(ThinFileTrigger::AgentDetected)
}

/// What the thin-file planner reads: the agents the caller detected as installed, and the two project files.
#[derive(Debug, Clone, Copy)]
pub struct ThinInput<'a> {
    pub detected: &'a [AgentId],
    pub gemini_md: &'a ProjectFile,
    pub cursor_mdc: &'a ProjectFile,
}

/// One planned thin file. `after == before` means it is already right and needs no write.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ThinFilePlan {
    /// Project-relative, with `/`.
    pub path: &'static str,
    pub trigger: ThinFileTrigger,
    pub before: Option<Vec<u8>>,
    pub after: Vec<u8>,
}

impl ThinFilePlan {
    pub fn needs_write(&self) -> bool {
        self.before.as_deref() != Some(self.after.as_slice())
    }
}

/// Plans `GEMINI.md` and `.cursor/rules/oah.mdc` by the C12 table; an ineligible file is absent from the result.
pub fn plan_thin(input: &ThinInput<'_>) -> Result<Vec<ThinFilePlan>, RulesError> {
    let mut plans = Vec::new();
    if let Some(trigger) = gemini_trigger(input.detected, input.gemini_md) {
        let (before, after) = import_line(input.gemini_md, GEMINI_MD)?;
        plans.push(ThinFilePlan {
            path: GEMINI_MD,
            trigger,
            before,
            after,
        });
    }
    if let Some(trigger) = cursor_trigger(input.detected) {
        let (before, after) = mdc(input.cursor_mdc)?;
        plans.push(ThinFilePlan {
            path: CURSOR_MDC,
            trigger,
            before,
            after,
        });
    }
    Ok(plans)
}

fn refuse_link(file: &ProjectFile, name: &str) -> Result<(), RulesError> {
    if let ProjectFile::Link { target } = file {
        let message = format!(
            "{name} is a link to {}; oah never writes through links, which could reach global files. Replace it \
             with a regular file.",
            target.display()
        );
        return Err(RulesError::new(Kind::Link, message));
    }
    Ok(())
}

/// Exactly one `@AGENTS.md` line: create the file with it, append it when missing, refuse more than one
/// (v0.4:src/oah/rules.py:154-165). Returns (before, after).
pub fn import_line(
    file: &ProjectFile,
    name: &str,
) -> Result<(Option<Vec<u8>>, Vec<u8>), RulesError> {
    refuse_link(file, name)?;
    let Some(bytes) = file.bytes() else {
        return Ok((None, format!("{IMPORT}\n").into_bytes()));
    };
    let lines = split_lines(decode(bytes, name)?);
    let count = lines
        .iter()
        .filter(|line| py_strip(strip_eol(line)) == IMPORT)
        .count();
    if count > 1 {
        let message = format!(
            "{name} has {count} {IMPORT} lines; keep exactly one and run `oah rules sync` again. Nothing was written."
        );
        return Err(RulesError::new(Kind::MarkerDuplicate, message));
    }
    let mut after = bytes.to_vec();
    if count == 0 {
        let eol = eol_of(bytes).as_str().as_bytes();
        if !after.is_empty() && !after.ends_with(b"\n") {
            after.extend_from_slice(eol);
        }
        after.extend_from_slice(IMPORT.as_bytes());
        after.extend_from_slice(eol);
    }
    Ok((Some(bytes.to_vec()), after))
}

/// OAH's own `.mdc`: the fixed text, in the existing file's line ending and keeping its byte order mark.
fn mdc(file: &ProjectFile) -> Result<(Option<Vec<u8>>, Vec<u8>), RulesError> {
    refuse_link(file, CURSOR_MDC)?;
    let before = file.bytes();
    let eol = before.map(eol_of).unwrap_or(super::text::Eol::Lf);
    let mut after = Vec::new();
    if before.is_some_and(|bytes| bytes.starts_with(b"\xef\xbb\xbf")) {
        after.extend_from_slice(b"\xef\xbb\xbf");
    }
    after.extend_from_slice(MDC_TEXT.replace('\n', eol.as_str()).as_bytes());
    Ok((before.map(<[u8]>::to_vec), after))
}
