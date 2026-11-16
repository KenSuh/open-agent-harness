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
    if let Some(address) = email(text) {
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
            .is_some_and(|c| c.is_ascii_alphanumeric() || c == '_')
    })
}

fn is_local(c: u8) -> bool {
    c.is_ascii_alphanumeric() || b"._%+-".contains(&c)
}

fn is_domain(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'-' || c == b'.'
}

/// The first e-mail address (local part, `@`, domain with a dot) in `text`. A local part right after a backslash is a JSON escape such as `\n`.
fn email(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    for (at, _) in text.match_indices('@') {
        let mut start = at;
        while start > 0 && is_local(bytes[start - 1]) {
            start -= 1;
        }
        if start == at || (start > 0 && bytes[start - 1] == b'\\') {
            continue;
        }
        let mut end = at + 1;
        while end < bytes.len() && is_domain(bytes[end]) {
            end += 1;
        }
        let domain = text[at + 1..end].trim_end_matches('.');
        let Some((head, tld)) = domain.rsplit_once('.') else {
            continue;
        };
        if !head.is_empty() && tld.len() >= 2 && tld.bytes().all(|c| c.is_ascii_alphabetic()) {
            return Some(text[start..at + 1 + domain.len()].to_owned());
        }
    }
    None
}

/// The first `NAME_KEY = "..."`, `NAME_TOKEN: '...'` or `"NAME_SECRET": "..."` in `text`.
fn secret_constant(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    for suffix in ["_KEY", "_TOKEN", "_SECRET", "_PASSWORD"] {
        for (at, _) in text.match_indices(suffix) {
            let end = at + suffix.len();
            if bytes
                .get(end)
                .is_some_and(|&c| c.is_ascii_alphanumeric() || c == b'_')
            {
                continue;
            }
            let mut start = at;
            while start > 0
                && (bytes[start - 1].is_ascii_uppercase()
                    || bytes[start - 1].is_ascii_digit()
                    || bytes[start - 1] == b'_')
            {
                start -= 1;
            }
            if start == at {
                continue;
            }
            let mut rest = text[end..].trim_start_matches(['"', '\'']).trim_start();
            if let Some(after) = rest.strip_prefix('=').or_else(|| rest.strip_prefix(':')) {
                rest = after.trim_start();
                if rest.starts_with('"') || rest.starts_with('\'') {
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

/// The samples are assembled from fragments, so this file never matches the repository privacy scan itself.
#[test]
fn each_pattern_is_caught() {
    let users = ["Us", "ers"].concat();
    let home = ["ho", "me"].concat();
    let at = "@";
    let samples = [
        (
            "macos home",
            format!("see /{users}/someone/project"),
            format!("path: /{users}/"),
        ),
        (
            "linux home",
            format!("\"path\": \"/{home}/someone/x\""),
            "path: /home/<name>".to_owned(),
        ),
        (
            "windows home",
            format!(r"C:\{users}\someone\x"),
            r"path: C:\Users\".to_owned(),
        ),
        (
            "windows home, lower case",
            format!(r"c:\{}\someone", users.to_lowercase()),
            r"path: C:\Users\".to_owned(),
        ),
        (
            "windows home, JSON escaped",
            format!(r#""C:\\{users}\\someone""#),
            r"path: C:\Users\".to_owned(),
        ),
        (
            "e-mail",
            format!("write to someone{at}example.org today"),
            format!("e-mail: someone{at}example.org"),
        ),
        (
            "e-mail in JSON",
            format!("\"author\": \"a.b+c{at}mail.example.co\""),
            format!("e-mail: a.b+c{at}mail.example.co"),
        ),
        (
            "assigned key",
            format!("OPENAI_API_{} = \"abc\"", "KEY"),
            "constant: OPENAI_API_KEY".to_owned(),
        ),
        (
            "assigned token",
            format!("GH_{}: 'abc'", "TOKEN"),
            "constant: GH_TOKEN".to_owned(),
        ),
        (
            "JSON secret",
            format!("{{\"APP_{}\": \"abc\"}}", "SECRET"),
            "constant: APP_SECRET".to_owned(),
        ),
        (
            "assigned password",
            format!("DB_{}=\"abc\"", "PASSWORD"),
            "constant: DB_PASSWORD".to_owned(),
        ),
    ];
    for (name, text, expected) in samples {
        let found = privacy_findings(&text);
        assert!(
            found.contains(&expected),
            "{name}: {found:?} lacks {expected}"
        );
    }
}

#[test]
fn ordinary_fixture_text_is_not_a_hit() {
    let samples = [
        "\"claude_after\": \"@AGENTS.md\\n\"",
        "\"label\": \"~/.claude/skills\", \"path\": \"<base>/userhome/.claude/skills\"",
        "\"superpowers@m\": []",
        "npm install -g @openai/codex",
        "only if you stored your own ARTIFICIAL_ANALYSIS_API_KEY, a key",
        "AA_KEY_NAME = \"x\"",
        "\"email\": \"fixture\"",
        "/homepage/ and a bare home folder: /home/",
    ];
    for text in samples {
        assert_eq!(privacy_findings(text), Vec::<String>::new(), "{text}");
    }
}
