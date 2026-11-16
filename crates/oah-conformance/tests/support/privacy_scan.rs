// 원문과 모든 JSON 문자열 층에서 개인정보를 거부하고 값이 명백한 참조일 때만 비밀 이름의 대입을 허용한다.
use std::collections::BTreeSet;

#[path = "json_strings.rs"]
mod json;

pub fn privacy_findings(text: &str) -> Vec<String> {
    let mut findings = BTreeSet::new();
    let mut seen = BTreeSet::new();
    let mut pending = vec![text.to_owned()];
    while let Some(layer) = pending.pop() {
        if !seen.insert(layer.clone()) {
            continue;
        }
        let strings = json::strings(&layer);
        let outside = |at| !strings.iter().any(|s| s.start <= at && at < s.end);
        for (at, _) in layer.match_indices("/Users/") {
            if outside(at) {
                findings.insert("path: macOS home");
            }
        }
        for (at, _) in layer.match_indices("/home/") {
            // A bare directory root has no account component; any following component is refused.
            if outside(at)
                && !layer[at + "/home/".len()..]
                    .trim_start_matches('/')
                    .is_empty()
            {
                findings.insert("path: Linux home");
            }
        }
        let lower = layer.to_ascii_lowercase();
        for (at, _) in lower.match_indices("c:") {
            let after = &lower[at + 2..];
            if outside(at)
                && after.starts_with('\\')
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
            if outside(at)
                && layer[..at]
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
                if !outside(at) {
                    continue;
                }
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
                if identifier(&layer[start..end]) && literal_assignment(&layer[end..]) {
                    findings.insert("secret literal assignment");
                }
            }
        }
        for string in strings {
            // Decoded keys need their parent assignment context even though each value is also scanned alone.
            if secret_name(&string.value) && literal_assignment(&layer[string.end..]) {
                findings.insert("secret literal assignment");
            }
            // Each child is shorter than its quoted source: no recursion or fixed depth limit is needed.
            pending.push(string.value);
        }
    }
    findings.into_iter().map(str::to_owned).collect()
}

fn address_domain(text: &str) -> bool {
    let end = text
        .char_indices()
        .find(|(_, c)| !c.is_alphanumeric() && !"-.".contains(*c))
        .map_or(text.len(), |(offset, _)| offset);
    let domain = text[..end].trim_end_matches('.');
    let Some((head, tld)) = domain.rsplit_once('.') else {
        return false;
    };
    !tld.is_empty()
        && tld.chars().all(char::is_alphabetic)
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
    // UPPER_SNAKE is the sole bare reference form; spaces around '=' cannot turn a word into a reference.
    let upper = identifier(value)
        && value.chars().any(|c| c.is_ascii_uppercase())
        && value
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_');
    (upper || shell_reference(value)) && value_boundary(&rest[end..])
}

fn literal_assignment(rest: &str) -> bool {
    let rest = rest.strip_prefix('\'').unwrap_or(rest).trim_start();
    // Equality is a comparison, not an assignment.
    if rest.starts_with("==") {
        return false;
    }
    match rest.strip_prefix('=').or_else(|| rest.strip_prefix(':')) {
        Some(after) => !reference_value(after),
        // Unknown syntax before an assignment marker cannot establish an allowed reference.
        None => rest
            .split([',', ';'])
            .next()
            .is_some_and(|s| s.contains(['=', ':'])),
    }
}
