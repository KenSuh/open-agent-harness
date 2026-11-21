// 규칙 텍스트 도구: v0.4.0 의 줄 나누기·줄끝·공백 규칙을 파이썬과 같은 결과로 옮긴 작은 함수들이다.
//! Line and whitespace helpers with v0.4.0's exact semantics (v0.4:src/oah/wiki/markdown.py:12-33 and Python's
//! `str.strip`, `str.rstrip`, `bytes.strip`), so the block algorithm stays byte-identical to the frozen one.

/// A line ending.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Eol {
    Lf,
    Crlf,
}

impl Eol {
    pub fn as_str(self) -> &'static str {
        match self {
            Eol::Lf => "\n",
            Eol::Crlf => "\r\n",
        }
    }
}

/// The file's line ending: that of its first line, else LF.
pub fn eol_of(data: &[u8]) -> Eol {
    match data.iter().position(|byte| *byte == b'\n') {
        Some(first) if first > 0 && data[first - 1] == b'\r' => Eol::Crlf,
        _ => Eol::Lf,
    }
}

/// Splits on LF only; every line keeps its ending, so joining the lines gives the input back.
pub fn split_lines(text: &str) -> Vec<&str> {
    text.split_inclusive('\n').collect()
}

/// The line without its LF and one CR before it.
pub fn strip_eol(line: &str) -> &str {
    let line = line.strip_suffix('\n').unwrap_or(line);
    line.strip_suffix('\r').unwrap_or(line)
}

/// Python's `str.isspace` for one character: Unicode separators plus the ASCII and information separators.
fn is_py_space(c: char) -> bool {
    matches!(c, '\t'..='\r' | '\x1c'..='\x1f' | ' ' | '\u{85}' | '\u{a0}' | '\u{1680}')
        || matches!(
            c,
            '\u{2000}'..='\u{200a}' | '\u{2028}' | '\u{2029}' | '\u{202f}' | '\u{205f}'
        )
        || c == '\u{3000}'
}

/// Python's `str.rstrip()` with no argument.
pub fn py_rstrip(text: &str) -> &str {
    text.trim_end_matches(is_py_space)
}

/// Python's `str.strip()` with no argument.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(is_py_space)
}

/// Whether Python's `bytes.strip()` would leave anything: a byte other than ASCII whitespace.
pub fn has_non_ascii_space(data: &[u8]) -> bool {
    data.iter()
        .any(|byte| !matches!(byte, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_space_includes_information_separators_that_rust_omits() {
        assert_eq!(py_rstrip("a\x1c\x1f \u{3000}"), "a");
        assert_eq!("a\x1c".trim_end(), "a\x1c");
    }
}
