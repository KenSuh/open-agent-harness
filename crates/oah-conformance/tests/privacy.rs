// conformance/ 아래 모든 파일에 사적 경로·전자우편·비밀 상수가 없는지 확인하고, 각 패턴이 실제로 잡히는지 메모리 표본으로 증명한다.
use std::path::{Path, PathBuf};

fn conformance_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("conformance")
}

/// Every file below `root`, sorted.
fn files_below(root: &Path) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(&folder) else {
            continue;
        };
        for entry in entries.filter_map(Result::ok) {
            let path = entry.path();
            if path.is_dir() {
                pending.push(path);
            } else {
                found.push(path);
            }
        }
    }
    found.sort();
    found
}

/// What in `text` looks like private data: a home folder path, an e-mail address, or a constant named
/// `*_KEY`, `*_TOKEN`, `*_SECRET` or `*_PASSWORD` assigned a literal. Each finding names its kind.
fn privacy_findings(text: &str) -> Vec<String> {
    let mut found = text_findings(text, true);
    let decoded = decoded_json_text(text);
    if decoded != text {
        found.extend(text_findings(&decoded, false));
    }
    found.sort();
    found.dedup();
    found
}

fn decoded_json_text(text: &str) -> String {
    let mut decoded = String::with_capacity(text.len());
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        decoded.push(c);
        if c == '"' {
            let mut candidate = chars.clone();
            if let Some(value) = json_string(&mut candidate) {
                decoded.push_str(&value);
                decoded.push('"');
                chars = candidate;
            }
        }
    }
    decoded
}

fn hex_quad(chars: &mut std::str::Chars<'_>) -> Option<u32> {
    (0..4).try_fold(0, |value, _| Some(value * 16 + chars.next()?.to_digit(16)?))
}

fn json_string(chars: &mut std::str::Chars<'_>) -> Option<String> {
    let mut value = String::new();
    while let Some(c) = chars.next() {
        match c {
            '"' => return Some(value),
            '\\' => value.push(match chars.next()? {
                '"' => '"',
                '\\' => '\\',
                '/' => '/',
                'b' => '\u{08}',
                'f' => '\u{0c}',
                'n' => '\n',
                'r' => '\r',
                't' => '\t',
                'u' => {
                    let mut code = hex_quad(chars)?;
                    if (0xd800..=0xdbff).contains(&code) {
                        if chars.next()? != '\\' || chars.next()? != 'u' {
                            return None;
                        }
                        let low = hex_quad(chars)?;
                        if !(0xdc00..=0xdfff).contains(&low) {
                            return None;
                        }
                        code = 0x10000 + (code - 0xd800) * 0x400 + low - 0xdc00;
                    }
                    char::from_u32(code)?
                }
                _ => return None,
            }),
            '\u{00}'..='\u{1f}' => return None,
            _ => value.push(c),
        }
    }
    None
}

fn text_findings(text: &str, json_escapes: bool) -> Vec<String> {
    let mut found = Vec::new();
    if text.contains("/Users/") {
        found.push("path: /Users/".to_owned());
    }
    if home_path(text) {
        found.push("path: /home/<name>".to_owned());
    }
    let lower = text.to_ascii_lowercase();
    if lower.contains("c:\\users\\") || lower.contains("c:\\\\users\\\\") {
        found.push("path: C:\\Users\\".to_owned());
    }
    if let Some(address) = email(text, json_escapes) {
        found.push(format!("e-mail: {address}"));
    }
    if let Some(name) = secret_constant(text) {
        found.push(format!("constant: {name}"));
    }
    found
}

fn home_path(text: &str) -> bool {
    text.match_indices("/home/").any(|(at, _)| {
        text[at + "/home/".len()..]
            .chars()
            .next()
            .is_some_and(|c| !matches!(c, '/' | '\\' | '"' | '\'' | '\r' | '\n' | '\0'))
    })
}

fn is_local(c: char) -> bool {
    c.is_alphanumeric() || ".!#$%&'*+-/=?^_`{|}~".contains(c)
}

fn is_domain(c: char) -> bool {
    c.is_alphanumeric() || c == '-' || c == '.'
}

fn email(text: &str, json_escapes: bool) -> Option<String> {
    for (at, _) in text.match_indices('@') {
        let mut start = at;
        for (offset, c) in text[..at].char_indices().rev() {
            if !is_local(c) {
                break;
            }
            start = offset;
        }
        if json_escapes && text[..start].ends_with('\\') {
            let local = &text[start..at];
            if local.starts_with(['n', 'r', 't', 'b', 'f']) {
                start += 1;
            } else if local.starts_with('u')
                && local
                    .as_bytes()
                    .get(1..5)
                    .is_some_and(|hex| hex.iter().all(u8::is_ascii_hexdigit))
            {
                start += 5;
            }
        }
        if start == at {
            continue;
        }
        let mut end = at + 1;
        for c in text[end..].chars() {
            if !is_domain(c) {
                break;
            }
            end += c.len_utf8();
        }
        let domain = text[at + 1..end].trim_end_matches('.');
        let Some((head, tld)) = domain.rsplit_once('.') else {
            continue;
        };
        if !head.is_empty() && tld.chars().count() >= 2 && tld.chars().all(char::is_alphabetic) {
            return Some(text[start..at + 1 + domain.len()].to_owned());
        }
    }
    None
}

fn identifier(text: &str) -> bool {
    let mut chars = text.chars();
    chars.next().is_some_and(|c| c.is_alphabetic() || c == '_')
        && chars.all(|c| c.is_alphanumeric() || c == '_')
}

fn variable_reference(value: &str, allow_identifier: bool) -> bool {
    if let Some(name) = value.strip_prefix("${") {
        return name.split_once('}').is_some_and(|(name, rest)| {
            identifier(name) && rest.chars().all(|c| ",;}]".contains(c))
        });
    }
    let value = value.trim_end_matches([',', ';', '}', ']']);
    if let Some(name) = value.strip_prefix('$') {
        return identifier(name);
    }
    allow_identifier
        && identifier(value)
        && !matches!(value, "true" | "false" | "null" | "True" | "False" | "None")
}

fn literal_value(rest: &str, allow_identifier: bool) -> bool {
    let decoded = if rest.starts_with('\\') {
        json_string(&mut rest.chars())
    } else {
        None
    };
    let rest = decoded.as_deref().unwrap_or(rest);
    if let Some(quoted) = rest.strip_prefix('"') {
        let mut chars = quoted.chars();
        return !json_string(&mut chars).is_some_and(|value| variable_reference(&value, false));
    }
    if rest.starts_with('\'') {
        return true;
    }
    let value = rest.split_whitespace().next().unwrap_or("");
    !value.is_empty()
        && !value.starts_with([',', ';', '}', ']'])
        && !variable_reference(value, allow_identifier)
}

fn secret_constant(text: &str) -> Option<String> {
    for suffix in ["_KEY", "_TOKEN", "_SECRET", "_PASSWORD"] {
        for (at, _) in text.match_indices(suffix) {
            let end = at + suffix.len();
            if text[end..]
                .chars()
                .next()
                .is_some_and(|c| c.is_alphanumeric() || c == '_')
            {
                continue;
            }
            let mut start = at;
            for (offset, c) in text[..at].char_indices().rev() {
                if !c.is_alphanumeric() && c != '_' {
                    break;
                }
                start = offset;
            }
            if !identifier(&text[start..end]) {
                continue;
            }
            let mut rest = text[end..].trim_start_matches(['"', '\'']).trim_start();
            if let Some(after) = rest.strip_prefix('=').or_else(|| rest.strip_prefix(':')) {
                if after.starts_with('=') && rest.starts_with('=') {
                    continue;
                }
                let allow_identifier =
                    rest.starts_with('=') && after.starts_with(char::is_whitespace);
                rest = after.trim_start();
                if literal_value(rest, allow_identifier) {
                    return Some(text[start..end].to_owned());
                }
            }
        }
    }
    None
}

#[test]
fn no_file_under_conformance_holds_private_data() {
    let root = conformance_root();
    let files = files_below(&root);
    assert!(!files.is_empty(), "no files under {}", root.display());
    let mut hits = Vec::new();
    for path in files {
        let bytes = std::fs::read(&path).expect("read fixture");
        let text = String::from_utf8_lossy(&bytes);
        for finding in privacy_findings(&text) {
            hits.push(format!("{}: {finding}", path.display()));
        }
    }
    assert!(
        hits.is_empty(),
        "private data in fixtures:\n{}",
        hits.join("\n")
    );
}

/// Samples use fragments so the repository privacy scan does not mistake them for private data.
#[test]
fn every_private_form_is_rejected_and_controls_are_allowed() {
    let users = ["Us", "ers"].concat();
    let home = ["ho", "me"].concat();
    let at = "@";
    let email = format!("person{at}synthetic.invalid");
    let token = ["APP_", "TOKEN"].concat();
    let password = ["APP_", "PASSWORD"].concat();
    let path = |account: &str| format!("/{home}/{account}/x");
    let json_text = |text: &str| format!("{{\"text\": \"{text}\"}}");
    let samples = [
        (format!("see /{users}/someone/project"), true),
        (format!("\"path\": \"{}\"", path("someone")), true),
        (format!(r"C:\{users}\someone\x"), true),
        (format!(r"c:\{}\someone", users.to_lowercase()), true),
        (format!(r#""C:\\{users}\\someone""#), true),
        (format!("write to someone{at}example.org today"), true),
        (format!("\"author\": \"a.b+c{at}mail.example.co\""), true),
        (format!("OPENAI_API_{} = \"abc\"", "KEY"), true),
        (format!("GH_{}: 'abc'", "TOKEN"), true),
        (format!("{{\"APP_{}\": \"abc\"}}", "SECRET"), true),
        (format!("DB_{}=\"abc\"", "PASSWORD"), true),
        (format!("/{users}/synthetic/x"), true),
        (path("synthetic"), true),
        (format!(r"C:\{users}\synthetic\x"), true),
        (format!(r#"{{"path": "C:\\{users}\\synthetic\\x"}}"#), true),
        (email.clone(), true),
        (format!("APP_{} = \"fixture-value\"", "KEY"), true),
        (format!("{token}: 'fixture-value'"), true),
        (format!("{{\"APP_{}\": \"fixture-value\"}}", "SECRET"), true),
        (format!("{password}=\"fixture-value\""), true),
        (json_text(&format!(r"first line\n{email}")), true),
        (json_text(&format!(r"first word\t{email}")), true),
        (json_text(&format!(r#"{token} = \"fixture-value\""#)), true),
        (format!("{token}=fixture-value-123456789"), true),
        (format!("{password} = 123456789"), true),
        (path("사용자"), true),
        (json_text(&format!(r"\/{users}\/synthetic")), true),
        (json_text(&format!(r"\u002f{users}\u002fsynthetic")), true),
        (json_text(r"\/home\/\uc0ac\uc6a9\uc790/x"), true),
        (json_text(r"\/home\/\ud83d\ude00/x"), true),
        (path("-fixture"), true),
        (path(".fixture"), true),
        (path(" fixture"), true),
        (path(" "), true),
        (json_text(&format!(r"C:\u005c{users}\u005csynthetic")), true),
        (json_text(r"person\u0040synthetic.invalid"), true),
        (format!("사용자{at}예시.한국"), true),
        (format!("o'hara{at}example.org"), true),
        (format!(r"\{email}"), true),
        (json_text(&format!(r"\r{email}")), true),
        (json_text(&format!(r"\b{email}")), true),
        (json_text(&format!(r"\f{email}")), true),
        (r#"{"APP_\u004bEY":123}"#.to_owned(), true),
        (format!("app_{}=fixture-value", "KEY"), true),
        (format!("앱_{}=fixture-value", "KEY"), true),
        (format!("_{}=fixture", "KEY"), true),
        (format!("{token}=FIXTURE"), true),
        (format!("{token} = true"), true),
        (format!("{token}: null"), true),
        (format!("{password} = -1.25e3"), true),
        (format!("{token}=\"\""), true),
        (format!("{token}='$OTHER_TOKEN'"), true),
        (format!(r#""\q /{users}/synthetic""#), true),
        ("\"claude_after\": \"@AGENTS.md\\n\"".to_owned(), false),
        (json_text(r"\n@AGENTS.md\t@AGENTS.md"), false),
        (json_text(&format!(r"\u000a{at}AGENTS.md")), false),
        (
            "\"label\": \"~/.claude/skills\", \"path\": \"<base>/userhome/.claude/skills\""
                .to_owned(),
            false,
        ),
        ("\"superpowers@m\": []".to_owned(), false),
        ("npm install -g @openai/codex".to_owned(), false),
        (
            "only if you stored your own ARTIFICIAL_ANALYSIS_API_KEY, a key".to_owned(),
            false,
        ),
        ("AA_KEY_NAME = \"x\"".to_owned(), false),
        ("\"email\": \"fixture\"".to_owned(), false),
        (
            "/homepage/ and a bare home folder: /home/".to_owned(),
            false,
        ),
        (json_text(&format!("/{home}/")), false),
        (format!("{token}=$OTHER_TOKEN"), false),
        (format!("{password}=${{OTHER_PASSWORD}}"), false),
        (format!("{token} = OTHER_TOKEN"), false),
        (format!("{token} = other_token"), false),
        (format!("{token}=\"$OTHER_TOKEN\""), false),
        (json_text(&format!(r#"{token}=\"$OTHER_TOKEN\""#)), false),
        (json_text(&token), false),
        (format!("{token}="), false),
        (format!("{token} == \"fixture\""), false),
    ];
    for (text, forbidden) in samples {
        let found = privacy_findings(&text);
        assert_eq!(!found.is_empty(), forbidden, "{text}: {found:?}");
    }
}

#[test]
fn json_string_decodes_all_escapes_and_rejects_invalid_sequences() {
    let samples = [
        (r#"plain""#, Some("plain")),
        (r#"\"\\\/\b\f\n\r\t""#, Some("\"\\/\u{08}\u{0c}\n\r\t")),
        (r#"\u0041\u00e9\uc0ac""#, Some("Aé사")),
        (r#"\ud83d\ude00""#, Some("😀")),
        (r#"\uD800\uDC00""#, Some("\u{10000}")),
        (r#"\uDBFF\uDFFF""#, Some("\u{10ffff}")),
        (r#"\uD800""#, None),
        (r#"\uDC00""#, None),
        (r#"\uD800\u0041""#, None),
        (r#"\uD800x""#, None),
        (r#"\u12x4""#, None),
        (r#"\u123""#, None),
        (r#"\q""#, None),
        ("raw\ncontrol\"", None),
        ("unterminated", None),
        ("trailing\\", None),
    ];
    for (text, expected) in samples {
        assert_eq!(
            json_string(&mut text.chars()).as_deref(),
            expected,
            "{text}"
        );
    }
}
