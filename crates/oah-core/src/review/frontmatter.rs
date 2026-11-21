// 앞부분 읽기: SKILL.md 의 --- 블록에서 최상위 문자열 필드를 읽고, 검토 기록이 남기는 다섯 필드로 정리한다.
//! Front matter, as v0.4.0 reads it (`v0.4:src/oah/skillsel.py:111-167`, `v0.4:src/oah/discovery/review.py:83-102`).
//!
//! [`parse_frontmatter`] reads the top-level string fields of a `---` block: plain, quoted and block (`|`, `>`) scalars,
//! with nested tables read as `""`. [`frontmatter`] turns them into the five fields a review records. The text is
//! data: nothing in it is followed.

use super::pystr::{is_space, strip};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// At most this many `allowed-tools` entries are kept.
const TOOLS_LIMIT: usize = 32;

/// The front matter fields a review records.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Frontmatter {
    pub name: Option<String>,
    pub description: Option<String>,
    /// `None` when `disable-model-invocation` is present but neither true nor false.
    pub model_invocable: Option<bool>,
    pub user_invocable: bool,
    /// `None` when `allowed-tools` is absent or empty.
    pub allowed_tools: Option<Vec<String>>,
}

/// The top-level string fields of a `---` front matter block; empty when there is none.
pub fn parse_frontmatter(text: &str) -> BTreeMap<String, String> {
    let text = text
        .trim_start_matches('\u{feff}')
        .replace("\r\n", "\n")
        .replace('\r', "\n");
    let lines: Vec<&str> = text.split('\n').collect();
    let mut result = BTreeMap::new();
    if lines.first().is_none_or(|first| strip(first) != "---") {
        return result;
    }
    let Some(end) = (1..lines.len()).find(|&index| matches!(strip(lines[index]), "---" | "..."))
    else {
        return result;
    };
    let body = &lines[1..end];
    let indented = |line: &str| line.starts_with([' ', '\t']);
    let mut index = 0;
    while index < body.len() {
        let line = body[index];
        index += 1;
        if strip(line).is_empty()
            || strip(line).starts_with('#')
            || indented(line)
            || !line.contains(':')
        {
            continue;
        }
        let (key, value) = line.split_once(':').unwrap_or((line, ""));
        let (key, value) = (strip(key), strip(value));
        let mut continuation = Vec::new();
        while index < body.len() && (strip(body[index]).is_empty() || indented(body[index])) {
            continuation.push(body[index]);
            index += 1;
        }
        while continuation
            .last()
            .is_some_and(|line| strip(line).is_empty())
        {
            continuation.pop();
        }
        let parsed = if value.starts_with(['|', '>']) {
            let stripped: Vec<&str> = continuation.iter().map(|line| strip(line)).collect();
            if value.starts_with('|') {
                stripped.join("\n")
            } else {
                stripped
                    .into_iter()
                    .filter(|line| !line.is_empty())
                    .collect::<Vec<_>>()
                    .join(" ")
            }
        } else if value.is_empty() {
            String::new()
        } else {
            let mut parts = vec![value];
            parts.extend(
                continuation
                    .iter()
                    .map(|line| strip(line))
                    .filter(|line| !line.is_empty()),
            );
            scalar(&parts.join(" "))
        };
        result.insert(key.to_string(), parsed);
    }
    result
}

/// A plain, single-quoted or double-quoted scalar.
fn scalar(text: &str) -> String {
    let text = strip(text);
    let chars: Vec<char> = text.chars().collect();
    if chars.len() >= 2 && chars[0] == '\'' && chars[chars.len() - 1] == '\'' {
        return chars[1..chars.len() - 1]
            .iter()
            .collect::<String>()
            .replace("''", "'");
    }
    if chars.len() >= 2 && chars[0] == '"' && chars[chars.len() - 1] == '"' {
        let inner = &chars[1..chars.len() - 1];
        let mut out = String::new();
        let mut index = 0;
        while index < inner.len() {
            if inner[index] == '\\' && index + 1 < inner.len() {
                out.push(match inner[index + 1] {
                    'n' => '\n',
                    't' => '\t',
                    other => other,
                });
                index += 2;
                continue;
            }
            out.push(inner[index]);
            index += 1;
        }
        return out;
    }
    text.to_string()
}

/// The front matter fields a review records, from the whole `SKILL.md` text.
pub fn frontmatter(text: &str) -> Frontmatter {
    let data = parse_frontmatter(text);
    let field = |key: &str| data.get(key).filter(|value| !value.is_empty()).cloned();
    let allowed_tools = field("allowed-tools").map(|tools| {
        tools
            .split(|c: char| c == ',' || is_space(c))
            .filter(|item| !item.is_empty())
            .take(TOOLS_LIMIT)
            .map(str::to_string)
            .collect()
    });
    let flag = |key: &str| match strip(data.get(key)?).to_lowercase().as_str() {
        "true" => Some(true),
        "false" => Some(false),
        _ => None,
    };
    let disabled = flag("disable-model-invocation");
    let model_invocable = match disabled {
        Some(disabled) => Some(!disabled),
        None if data.contains_key("disable-model-invocation") => None,
        None => Some(true),
    };
    Frontmatter {
        name: field("name"),
        description: field("description"),
        model_invocable,
        user_invocable: flag("user-invocable").unwrap_or(true),
        allowed_tools,
    }
}
