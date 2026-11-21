// 관리 블록 표시줄 문법: 줄 전체로만 세는 시작·끝 표시줄을 찾고, 깨짐·중복·짝 없음·새 버전을 타입 거절로 돌려준다.
//! The marker grammar of the managed block (v0.4:src/oah/rules.py:19-21,28,74-102). Markers count only as whole
//! lines, so a document that merely mentions a marker is left alone.

use super::error::{RulesError, RulesErrorKind as Kind};
use super::text::{py_rstrip, strip_eol};

pub const VERSION: u64 = 1;
pub const BEGIN: &str = "<!-- OAH:RULES:BEGIN v1 -->";
pub const END: &str = "<!-- OAH:RULES:END -->";
/// Any line starting with this is a marker, well-formed or not.
pub(crate) const RESERVED: &str = "<!-- OAH:RULES:";

/// A begin marker's version digits, leading zeros removed (`""` for zero).
fn begin_version(body: &str) -> Option<&str> {
    let digits = body
        .strip_prefix("<!-- OAH:RULES:BEGIN v")?
        .strip_suffix(" -->")?;
    (!digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit()))
        .then(|| digits.trim_start_matches('0'))
}

/// 0-based (begin, end) lines of the managed block, `None` when absent; refuses broken markers.
pub fn find_block(lines: &[&str]) -> Result<Option<(usize, usize)>, RulesError> {
    let (mut begins, mut ends, mut broken) = (Vec::new(), Vec::new(), Vec::new());
    for (number, line) in lines.iter().enumerate() {
        let body = py_rstrip(strip_eol(line));
        if !body.starts_with(RESERVED) {
            continue;
        }
        match begin_version(body) {
            Some(version) if !version.is_empty() => begins.push((number, version)),
            _ if body == END => ends.push(number),
            _ => broken.push(number),
        }
    }
    let hint = format!(
        "Fix the markers by hand so AGENTS.md has at most one {BEGIN} line followed by one {END} line, then run \
         `oah rules sync` again. Nothing was written."
    );
    if let Some(first) = broken.first() {
        let message = format!(
            "AGENTS.md line {} is a malformed rules marker. {hint}",
            first + 1
        );
        return Err(RulesError::new(Kind::MarkerBroken, message));
    }
    if begins.len() > 1 || ends.len() > 1 {
        let message = format!(
            "AGENTS.md has {} begin and {} end markers. {hint}",
            begins.len(),
            ends.len()
        );
        return Err(RulesError::new(Kind::MarkerDuplicate, message));
    }
    let (Some(&(begin, version)), Some(&end)) = (begins.first(), ends.first()) else {
        if begins.is_empty() && ends.is_empty() {
            return Ok(None);
        }
        let message = format!("AGENTS.md has an unpaired or reversed rules marker. {hint}");
        return Err(RulesError::new(Kind::MarkerBroken, message));
    };
    if end < begin {
        let message = format!("AGENTS.md has an unpaired or reversed rules marker. {hint}");
        return Err(RulesError::new(Kind::MarkerBroken, message));
    }
    if version != "1" {
        let message = format!(
            "AGENTS.md has a v{version} rules block from a newer oah; upgrade oah instead of syncing with this one"
        );
        return Err(RulesError::new(Kind::Newer, message));
    }
    Ok(Some((begin, end)))
}
