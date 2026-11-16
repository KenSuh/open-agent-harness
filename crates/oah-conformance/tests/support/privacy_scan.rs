// 정규 픽스처 문법을 먼저 검사하고 명시된 문자열·바이트 층 모두에 같은 개인정보 거부 규칙을 적용한다.
use std::collections::BTreeSet;
use std::path::Path;

#[path = "json_strings.rs"]
mod json;

pub fn privacy_findings(text: &str) -> Vec<String> {
    let Some(value) = json::canonical_document(text) else {
        return vec!["fixture grammar: non-canonical JSON".to_owned()];
    };
    scan_layers(value)
}

pub fn file_privacy_findings(path: &Path, bytes: &[u8]) -> Vec<String> {
    let Ok(text) = std::str::from_utf8(bytes) else {
        return vec!["fixture grammar: invalid UTF-8".to_owned()];
    };
    let plain = match path.to_str() {
        Some(".gitattributes") => text == "* -text\n",
        Some("FORMAT.md") => format_grammar(text),
        _ => return privacy_findings(text),
    };
    if !plain {
        return vec!["fixture grammar: invalid plain-text document".to_owned()];
    }
    let mut findings = BTreeSet::new();
    recognize(text, &mut findings);
    findings.into_iter().map(str::to_owned).collect()
}

fn format_grammar(text: &str) -> bool {
    let headings = [
        "# Conformance fixtures",
        "## Layout",
        "## MANIFEST.json",
        "## Re-running the extractor",
        "## Privacy scan",
    ];
    let actual: Vec<_> = text.lines().filter(|line| line.starts_with('#')).collect();
    let mut fence = false;
    for line in text.lines() {
        if line.starts_with("```") {
            if !matches!(line, "```" | "```sh") || (fence && line != "```") {
                return false;
            }
            fence = !fence;
        }
    }
    text.starts_with("# Conformance fixtures\n\n")
        && actual == headings
        && !fence
        && text.ends_with('\n')
        && !text.ends_with("\n\n")
        && text
            .bytes()
            .all(|b| b == b'\n' || (b' '..=b'~').contains(&b))
}

fn scan_layers(value: json::Value) -> Vec<String> {
    let mut findings = BTreeSet::new();
    let mut seen = BTreeSet::new();
    let mut pending = Vec::new();
    document_layers(value, &mut pending, &mut findings);
    while let Some(layer) = pending.pop() {
        if !seen.insert(layer.clone()) {
            continue;
        }
        let text = layer.trim_start_matches('\u{feff}').trim_start();
        if embedded_document(text) {
            match json::input_document(text) {
                Some(value) => document_layers(value, &mut pending, &mut findings),
                None => {
                    findings.insert("fixture grammar: invalid embedded JSON");
                }
            }
        } else if plain_body(&layer) {
            recognize(&layer, &mut findings);
        } else {
            findings.insert("fixture grammar: unsupported body escape");
        }
    }
    findings.into_iter().map(str::to_owned).collect()
}

fn embedded_document(text: &str) -> bool {
    if text == "{" {
        return false;
    }
    if text.starts_with(['{', '"']) {
        return true;
    }
    let Some(array) = text.strip_prefix('[') else {
        return false;
    };
    let first = array.trim_start();
    first.starts_with(['{', '[', '"', ']', '-'])
        || first.starts_with(|c: char| c.is_ascii_digit())
        || ["true", "false", "null"]
            .iter()
            .any(|word| first.starts_with(word))
}

fn plain_body(text: &str) -> bool {
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' {
            if chars.next() != Some('[') {
                return false;
            }
            loop {
                match chars.next() {
                    Some('0'..='9' | ';') => {}
                    Some('m') => break,
                    _ => return false,
                }
            }
            continue;
        }
        if c.is_control() && !['\r', '\n', '\t'].contains(&c) {
            return false;
        }
        if c == '\\'
            && !chars
                .next()
                .is_some_and(|next| ['\\', '"', '.'].contains(&next))
        {
            return false;
        }
    }
    true
}

fn document_layers(
    value: json::Value,
    pending: &mut Vec<String>,
    findings: &mut BTreeSet<&'static str>,
) {
    let mut values = vec![value];
    while let Some(value) = values.pop() {
        match value {
            json::Value::Object(fields) => {
                if let Some(json::Value::Object(files)) = fields.get("bytes") {
                    for value in files.values() {
                        let json::Value::String(hex) = value else {
                            findings.insert("fixture grammar: invalid bytes field");
                            continue;
                        };
                        let Some(bytes) = hex_bytes(hex) else {
                            findings.insert("fixture grammar: invalid bytes field");
                            continue;
                        };
                        if bytes.starts_with(&[0xff, 0xfe]) || bytes.starts_with(&[0xfe, 0xff]) {
                            if !bytes.len().is_multiple_of(2) {
                                findings.insert("fixture grammar: truncated UTF-16");
                                continue;
                            }
                            let little_endian = bytes.starts_with(&[0xff, 0xfe]);
                            let units: Vec<_> = bytes[2..]
                                .chunks_exact(2)
                                .map(|pair| {
                                    if little_endian {
                                        u16::from_le_bytes([pair[0], pair[1]])
                                    } else {
                                        u16::from_be_bytes([pair[0], pair[1]])
                                    }
                                })
                                .collect();
                            match String::from_utf16(&units) {
                                Ok(text) => pending.push(text),
                                Err(_) => {
                                    findings.insert("fixture grammar: invalid UTF-16");
                                }
                            }
                        } else {
                            pending.push(String::from_utf8_lossy(&bytes).into_owned());
                        }
                    }
                }
                for (key, value) in fields {
                    if secret_name(&key)
                        && !matches!(&value, json::Value::String(text) if shell_reference(text))
                    {
                        findings.insert("secret literal assignment");
                    }
                    pending.push(key);
                    values.push(value);
                }
            }
            json::Value::Array(items) => values.extend(items),
            json::Value::String(text) => pending.push(text),
            json::Value::Scalar(_) => {}
        }
    }
}

fn hex_bytes(text: &str) -> Option<Vec<u8>> {
    if !text.len().is_multiple_of(2)
        || !text
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return None;
    }
    text.as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let high = char::from(pair[0]).to_digit(16)?;
            let low = char::from(pair[1]).to_digit(16)?;
            u8::try_from(high * 16 + low).ok()
        })
        .collect()
}

fn recognize(layer: &str, findings: &mut BTreeSet<&'static str>) {
    if layer.contains("/Users/") {
        findings.insert("path: macOS home");
    }
    for (at, _) in layer.match_indices("/home/") {
        // A bare directory root has no account component; any following component is refused.
        if !layer[at + "/home/".len()..]
            .trim_start_matches('/')
            .is_empty()
        {
            findings.insert("path: Linux home");
        }
    }
    let lower = layer.to_ascii_lowercase();
    for (at, _) in lower.match_indices("c:") {
        let after = &lower[at + 2..];
        if after.starts_with('\\')
            && after
                .trim_start_matches('\\')
                .strip_prefix("users")
                .is_some_and(|s| s.starts_with('\\'))
        {
            findings.insert("path: Windows home");
        }
    }
    for (at, _) in layer.match_indices('@') {
        // Bare @path imports have no local part. Never guess the shape of a nonempty local part.
        if layer[..at]
            .chars()
            .next_back()
            .is_some_and(|c| !c.is_whitespace())
            && address_domain(&layer[at + 1..])
        {
            findings.insert("e-mail address");
        }
    }
    for suffix in ["_KEY", "_TOKEN", "_SECRET", "_PASSWORD"] {
        for (at, _) in layer.match_indices(suffix) {
            let end = at + suffix.len();
            if layer[end..].chars().next().is_some_and(name_char) {
                continue;
            }
            let start = layer[..at]
                .char_indices()
                .rev()
                .take_while(|(_, c)| name_char(*c))
                .last()
                .map_or(at, |(offset, _)| offset);
            let before = layer[..start].rsplit('\n').next().unwrap_or("").trim();
            let whitespace_assignment = before.is_empty()
                || before == "ENV"
                || before.starts_with("set ")
                || before == "(setq";
            if identifier(&layer[start..end])
                && literal_assignment(&layer[end..], whitespace_assignment)
            {
                findings.insert("secret literal assignment");
            }
        }
    }
}

fn address_domain(text: &str) -> bool {
    if let Some(literal) = text.strip_prefix('[').and_then(|s| s.split_once(']')) {
        return !literal.0.is_empty();
    }
    let end = text
        .char_indices()
        .find(|(_, c)| !c.is_alphanumeric() && !"-.".contains(*c))
        .map_or(text.len(), |(offset, _)| offset);
    let domain = text[..end].trim_end_matches('.');
    let Some((head, tld)) = domain.rsplit_once('.') else {
        return false;
    };
    !tld.is_empty()
        && (tld.chars().all(char::is_alphabetic)
            || tld
                .to_ascii_lowercase()
                .strip_prefix("xn--")
                .is_some_and(|label| {
                    !label.is_empty()
                        && label
                            .bytes()
                            .all(|c| c.is_ascii_alphanumeric() || c == b'-')
                }))
        && head
            .split('.')
            .all(|label| !label.is_empty() && label.chars().any(char::is_alphanumeric))
}

fn name_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn identifier(text: &str) -> bool {
    let mut chars = text.chars();
    chars.next().is_some_and(|c| c.is_alphabetic() || c == '_') && chars.all(name_char)
}

fn secret_name(text: &str) -> bool {
    ["_KEY", "_TOKEN", "_SECRET", "_PASSWORD"]
        .iter()
        .any(|suffix| text.ends_with(suffix))
}

fn shell_reference(value: &str) -> bool {
    let name = value
        .strip_prefix("${")
        .and_then(|s| s.strip_suffix('}'))
        .or_else(|| value.strip_prefix('$'));
    name.is_some_and(|s| identifier(s) && s.is_ascii())
}

fn value_boundary(rest: &str) -> bool {
    let rest = rest.trim_start_matches([' ', '\t']);
    rest.is_empty() || rest.starts_with([',', ';', '}', ']', '\r', '\n', '#'])
}

fn reference_value(rest: &str) -> bool {
    let rest = rest.trim_start();
    let quoted = match rest.chars().next() {
        Some('"') => {
            let mut chars = rest[1..].chars();
            json::string(&mut chars).map(|value| (value, chars.as_str()))
        }
        Some('\'') => rest[1..]
            .split_once('\'')
            .map(|(value, tail)| (value.to_owned(), tail)),
        _ => None,
    };
    if rest.starts_with(['"', '\'']) {
        // Quoted symbolic $NAME/${NAME} references contain no literal credential.
        return quoted.is_some_and(|(value, tail)| shell_reference(&value) && value_boundary(tail));
    }
    let end = if rest.starts_with("${") {
        rest.find('}').map_or(rest.len(), |at| at + 1)
    } else {
        rest.char_indices()
            .find(|(_, c)| !name_char(*c) && *c != '$')
            .map_or(rest.len(), |(at, _)| at)
    };
    let value = &rest[..end];
    shell_reference(value) && value_boundary(&rest[end..])
}

fn literal_assignment(rest: &str, whitespace_assignment: bool) -> bool {
    let rest = rest.strip_prefix(['\'', '"']).unwrap_or(rest).trim_start();
    // Equality is a comparison, not an assignment.
    if rest.starts_with("==") {
        return false;
    }
    match rest
        .strip_prefix('=')
        .or_else(|| rest.strip_prefix(':'))
        .or_else(|| rest.strip_prefix("<-"))
        .or_else(|| rest.strip_prefix('>'))
    {
        Some(after) => !reference_value(after),
        // Unknown syntax before an assignment marker cannot establish an allowed reference.
        None => rest.split([',', ';']).next().is_some_and(|s| {
            s.contains(['=', ':'])
                || (whitespace_assignment
                    && !s.trim().is_empty()
                    && !s.trim_start().starts_with([',', ')', '}', ']', '<']))
        }),
    }
}
