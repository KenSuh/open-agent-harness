// 파이썬 문자열 규칙: v0.4.0 코드가 기대는 str 의 공백·대소문자·줄 나눔·서식 문자 판정을 같은 결과로 옮긴다.
//! The parts of Python 3.11 `str` behaviour the ported review and license code relies on.
//!
//! Exact for Python 3.11 (Unicode 14.0): `isspace()` (and so `strip()` and `split()`), `splitlines()`, the
//! `Cf` (format) category, and `repr()` for printable text. Close, with the difference stated:
//!
//! * `lower()` uses Rust's Unicode tables: the same mapping, possibly from a newer Unicode version.
//! * `casefold()` is the lower-case mapping plus every full folding whose result starts with an ASCII letter
//!   (`ß` -> `ss`, `ſ` -> `s`, the `ﬀ`..`ﬆ` ligatures, ...). That is exact for comparisons with the ASCII words
//!   this code uses; other foldings to non-ASCII letters may differ.
//! * `portable_key_component()` applies full Unicode 14.0.0 NFKC (`unicode-normalization =0.1.21`) followed by
//!   full default case folding (`caseless =0.2.2`) for Unicode 14.0.0 assigned scalars (`unicode-general-category =0.5.1`),
//!   preserving Unicode 14.0.0 unassigned scalars verbatim to match Python 3.11 `unicodedata.normalize("NFKC", s).casefold()`.
//! * `repr()` escapes C0 and C1 controls, format characters, line and paragraph separators, non-ASCII spaces
//!   and private-use characters; unassigned code points print as themselves.

use caseless::Caseless;
use unicode_general_category::{GeneralCategory, get_general_category};
use unicode_normalization::UnicodeNormalization;

/// `str.isspace()`: Unicode White_Space plus the separators U+001C..U+001F.
pub(super) fn is_space(c: char) -> bool {
    matches!(
        c,
        '\t'..='\r'
            | '\u{1c}'..='\u{20}'
            | '\u{85}'
            | '\u{a0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200a}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202f}'
            | '\u{205f}'
            | '\u{3000}'
    )
}

/// `str.strip()` with no argument.
pub(super) fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// `str.lstrip()` with no argument.
pub(super) fn lstrip(text: &str) -> &str {
    text.trim_start_matches(is_space)
}

/// `str.split()` with no argument: runs of whitespace separate, empty pieces are dropped.
pub(super) fn split_whitespace(text: &str) -> Vec<&str> {
    text.split(is_space)
        .filter(|piece| !piece.is_empty())
        .collect()
}

/// A word character for `\w` and `\b`: `_` or alphanumeric. Python's `str.isalnum()` excludes the combining
/// marks Unicode lists as alphabetic (Other_Alphabetic); Rust's `is_alphanumeric()` includes them.
pub(super) fn is_word(c: char) -> bool {
    c == '_' || c.is_alphanumeric()
}

/// The simple lower-case mapping `sre` compares with (U+0130 maps to `i`).
pub(super) fn lower_char(c: char) -> char {
    c.to_lowercase().next().unwrap_or(c)
}

/// The simple upper-case mapping, or the character itself when it has only a multi-character one.
pub(super) fn upper_char(c: char) -> char {
    let mut upper = c.to_uppercase();
    match (upper.next(), upper.next()) {
        (Some(single), None) => single,
        _ => c,
    }
}

/// The other lower-case letter an ASCII letter matches when case is ignored (Python's extra case pairs).
pub(super) fn extra_case(c: char) -> Option<char> {
    match c {
        'i' => Some('\u{131}'),
        '\u{131}' => Some('i'),
        's' => Some('\u{17f}'),
        '\u{17f}' => Some('s'),
        _ => None,
    }
}

/// The full case foldings whose result starts with an ASCII letter.
fn ascii_folding(c: char) -> Option<&'static str> {
    Some(match c {
        '\u{df}' | '\u{1e9e}' => "ss",
        '\u{17f}' => "s",
        '\u{1f0}' => "j\u{30c}",
        '\u{1e96}' => "h\u{331}",
        '\u{1e97}' => "t\u{308}",
        '\u{1e98}' => "w\u{30a}",
        '\u{1e99}' => "y\u{30a}",
        '\u{1e9a}' => "a\u{2be}",
        '\u{fb00}' => "ff",
        '\u{fb01}' => "fi",
        '\u{fb02}' => "fl",
        '\u{fb03}' => "ffi",
        '\u{fb04}' => "ffl",
        '\u{fb05}' | '\u{fb06}' => "st",
        _ => return None,
    })
}

/// `str.casefold()`, exact for comparisons with ASCII words (see the module notes).
pub(super) fn casefold(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match ascii_folding(c) {
            Some(folded) => out.push_str(folded),
            None => out.extend(c.to_lowercase()),
        }
    }
    out
}

/// `str.splitlines()` without line ends.
pub(super) fn splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, c)) = chars.next() {
        let breaks = matches!(
            c,
            '\n' | '\r'
                | '\u{b}'
                | '\u{c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !breaks {
            continue;
        }
        lines.push(&text[start..index]);
        let mut next = index + c.len_utf8();
        if c == '\r'
            && let Some((_, '\n')) = chars.peek()
        {
            chars.next();
            next += 1;
        }
        start = next;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// Unicode 14.0 general category Cf, as Python 3.11's `unicodedata` reports it.
const FORMAT: [(u32, u32); 21] = [
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x13438),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
];

/// `unicodedata.category(c) == "Cf"`.
pub(super) fn is_format(c: char) -> bool {
    let code = c as u32;
    FORMAT
        .iter()
        .any(|&(low, high)| low <= code && code <= high)
}

/// Python 3.11 (Unicode 14.0.0) `unicodedata.normalize("NFKC", text).casefold()` for one path component.
///
/// Normalizes the full component with Unicode 14.0.0 NFKC first, then applies full default case folding to
/// Unicode 14.0.0 assigned scalars while preserving Unicode 14.0.0 unassigned scalars verbatim.
pub(super) fn portable_key_component(text: &str) -> String {
    let normalized: String = text.nfkc().collect();
    let mut folded = String::with_capacity(normalized.len());
    for c in normalized.chars() {
        if get_general_category(c) != GeneralCategory::Unassigned {
            folded.extend(std::iter::once(c).default_case_fold());
        } else {
            folded.push(c);
        }
    }
    folded
}

/// Whether `repr()` escapes this character (see the module notes).
fn unprintable(c: char) -> bool {
    let code = c as u32;
    code < 0x20
        || (0x7F..=0xA0).contains(&code)
        || is_format(c)
        || (is_space(c) && c != ' ')
        || (0xE000..=0xF8FF).contains(&code)
        || code >= 0xF0000
}

/// `repr(text)` of a Python `str`: single quotes unless the text holds a single quote and no double quote.
pub(super) fn repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if unprintable(c) => {
                let code = c as u32;
                if code < 0x100 {
                    out.push_str(&format!("\\x{code:02x}"));
                } else if code < 0x10000 {
                    out.push_str(&format!("\\u{code:04x}"));
                } else {
                    out.push_str(&format!("\\U{code:08x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}
