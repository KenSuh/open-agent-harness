// 개인정보 거부 규약과 허용 대조군을 한 표에서 만들고 같은 입력을 여러 JSON 층으로 감싼다.
pub struct Probe {
    pub name: String,
    pub text: String,
    pub forbidden: bool,
}

/// Independent JSON writer for the probe transport, not the scanner's decoder.
pub fn json_string(text: &str) -> String {
    let mut out = String::from("\"");
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{00}'..='\u{1f}' => out.push_str(&format!("\\u{:04x}", u32::from(c))),
            _ => out.push(c),
        }
    }
    out.push('"');
    out
}

pub fn samples() -> Vec<Probe> {
    let users = ["Us", "ers"].concat();
    let home = ["ho", "me"].concat();
    let at = "@";
    let address = format!("person{at}synthetic.invalid");
    let token = ["APP_", "TOKEN"].concat();
    let password = ["APP_", "PASSWORD"].concat();
    let home_path = |name: &str| format!("/{home}/{name}/x");
    let windows = format!(r"C:\{users}\synthetic\x");
    let text_json = |text: &str| format!("{{\"text\":{}}}", json_string(text));
    let embedded =
        |text: &str| format!("{{\"files\":{{\"settings.json\":{}}}}}", json_string(text));

    let rows = [
        ("r1-macos", format!("/{users}/synthetic/x"), true),
        ("r1-linux", home_path("synthetic"), true),
        ("r1-windows", windows.clone(), true),
        (
            "r1-windows-json",
            format!("{{\"path\":{}}}", json_string(&windows)),
            true,
        ),
        ("r1-email", address.clone(), true),
        (
            "r1-quoted-key",
            format!("APP_{} = \"fixture-value\"", "KEY"),
            true,
        ),
        ("r1-quoted-token", format!("{token}: 'fixture-value'"), true),
        (
            "r1-quoted-secret",
            format!("{{\"APP_{}\":\"fixture-value\"}}", "SECRET"),
            true,
        ),
        (
            "r1-quoted-password",
            format!("{password}=\"fixture-value\""),
            true,
        ),
        (
            "r1-json-newline-email",
            text_json(&format!("first line\n{address}")),
            true,
        ),
        (
            "r1-json-tab-email",
            text_json(&format!("first word\t{address}")),
            true,
        ),
        (
            "r1-json-escaped-assignment",
            text_json(&format!("{token} = \"fixture-value\"")),
            true,
        ),
        (
            "r1-unquoted-token",
            format!("{token}=fixture-value-123456789"),
            true,
        ),
        (
            "r1-numeric-password",
            format!("{password} = 123456789"),
            true,
        ),
        ("r1-unicode-home", home_path("사용자"), true),
        (
            "c1-escaped-macos",
            format!(r#"{{"p":"\u002f{users}\u002fsynthetic"}}"#),
            true,
        ),
        (
            "c1-escaped-home",
            format!(r#"{{"p":"\/{home}\/\ud83d\ude00/x"}}"#),
            true,
        ),
        (
            "c1-escaped-email",
            r#"{"p":"person\u0040synthetic.invalid"}"#.to_owned(),
            true,
        ),
        ("c1-escaped-key", r#"{"APP_\u004bEY":123}"#.to_owned(), true),
        (
            "c1-lower-prefix",
            format!("app_{}=fixture-value", "KEY"),
            true,
        ),
        ("c1-punctuation-home", home_path("-fixture"), true),
        ("c1-email-after-backslash", format!(r"\{address}"), true),
        (
            "r2-single-json-email",
            r#"{"author":"person\u0040synthetic.invalid"}"#.to_owned(),
            true,
        ),
        (
            "r2-embedded-email",
            embedded(r#"{"author":"person\u0040synthetic.invalid"}"#),
            true,
        ),
        (
            "r2-embedded-macos",
            embedded(&format!(r#"{{"path":"\u002f{users}\u002fsynthetic/x"}}"#)),
            true,
        ),
        (
            "r2-embedded-home",
            embedded(&format!(r#"{{"path":"\/{home}\/synthetic/x"}}"#)),
            true,
        ),
        (
            "r2-embedded-windows",
            embedded(&format!(r#"{{"path":"C:\u005c{users}\u005csynthetic"}}"#)),
            true,
        ),
        (
            "r2-embedded-key",
            embedded(r#"{"APP_\u004bEY":"fixture-value"}"#),
            true,
        ),
        (
            "r2-quoted-local",
            format!("\"private user\"{at}synthetic.invalid"),
            true,
        ),
        (
            "r2-json-quoted-local",
            format!(
                "{{\"author\":{}}}",
                json_string(&format!("\"private user\"{at}synthetic.invalid"))
            ),
            true,
        ),
        (
            "r2-dotenv-spaced",
            format!("{token} = fixturevalue\n"),
            true,
        ),
        (
            "r2-dotenv-tabbed",
            format!("{token}=\tfixturevalue\n"),
            true,
        ),
        (
            "r2-dotenv-unspaced",
            format!("{token}=fixturevalue\n"),
            true,
        ),
        (
            "r2-dotenv-hyphen",
            format!("{token} = fixture-value\n"),
            true,
        ),
        ("home-dot", home_path(".fixture"), true),
        ("home-space", home_path(" "), true),
        ("home-quote", format!("/{home}/\""), true),
        ("windows-lower", windows.to_lowercase(), true),
        ("windows-escaped", windows.replace('\\', "\\\\"), true),
        (
            "windows-unicode",
            format!(r#"{{"path":"C:\u005c{users}\u005csynthetic"}}"#),
            true,
        ),
        ("unicode-email", format!("사용자{at}예시.한국"), true),
        ("apostrophe-email", format!("o'hara{at}example.org"), true),
        (
            "punctuation-before-at",
            format!("!{at}synthetic.invalid"),
            true,
        ),
        (
            "empty-quoted-local",
            format!("\"\"{at}synthetic.invalid"),
            true,
        ),
        (
            "quote-local-escape",
            format!("\"a\\\"b\"{at}synthetic.invalid"),
            true,
        ),
        ("single-letter-tld", format!("person{at}synthetic.x"), true),
        ("import-with-prefix", format!("person{at}AGENTS.md"), true),
        (
            "import-with-quote-prefix",
            format!("\"person\"{at}AGENTS.md"),
            true,
        ),
        ("secret-lower-word", format!("{token} = other_token"), true),
        (
            "uncertain-assignment",
            format!("{token} += \"fixture-value\""),
            true,
        ),
        (
            "commented-assignment",
            format!("{token} /* comment\ncontinued */ = \"fixture-value\""),
            true,
        ),
        ("secret-boolean", format!("{token} = true"), true),
        ("secret-null", format!("{token}: null"), true),
        ("secret-number", format!("{password} = -1.25e3"), true),
        ("secret-empty", format!("{token}="), true),
        ("secret-empty-string", format!("{token}=\"\""), true),
        (
            "secret-expression",
            format!("{token} = OTHER_TOKEN + \"fixture\""),
            true,
        ),
        ("secret-call", format!("{token} = getenv(\"NAME\")"), true),
        (
            "secret-quoted-uppercase",
            format!("{token} = \"OTHER_TOKEN\""),
            true,
        ),
        (
            "secret-ref-suffix",
            format!("{token} = $OTHER_TOKEN/fixture"),
            true,
        ),
        (
            "secret-braced-ref-suffix",
            format!("{token} = ${{OTHER_TOKEN}}fixture"),
            true,
        ),
        (
            "secret-ref-expression",
            format!("{token} = ${{OTHER_TOKEN}} + \"fixture\""),
            true,
        ),
        (
            "secret-ref-space-literal",
            format!("{token} = OTHER_TOKEN fixture"),
            true,
        ),
        (
            "secret-unicode-prefix",
            format!("앱_{}=fixture-value", "KEY"),
            true,
        ),
        (
            "secret-empty-prefix",
            format!("_{}=fixture-value", "KEY"),
            true,
        ),
        (
            "secret-single-quoted-key",
            format!("{{'APP_{}': 1}}", "KEY"),
            true,
        ),
        (
            "secret-encoded-value",
            format!(r#"{{"APP_{}":"\u0066ixture"}}"#, "KEY"),
            true,
        ),
        (
            "secret-arbitrary-key",
            format!("{{\"service name.APP_{}\":1}}", "KEY"),
            true,
        ),
        (
            "bad-escape-with-path",
            format!(r#""\q /{users}/synthetic""#),
            true,
        ),
        ("clean", "synthetic public text".to_owned(), false),
        // Bare imports have no local part; serialized quotes and escapes are transport syntax.
        ("bare-import", "@AGENTS.md".to_owned(), false),
        (
            "c1-agents-import",
            "\"claude_after\": \"@AGENTS.md\\n\"".to_owned(),
            false,
        ),
        (
            "c1-imports-after-escapes",
            text_json("\n@AGENTS.md\t@AGENTS.md"),
            false,
        ),
        (
            "c1-import-after-unicode",
            format!(r#"{{"p":"\u000a{at}AGENTS.md"}}"#),
            false,
        ),
        ("r2-import", text_json("@AGENTS.md\n"), false),
        ("import-relative-path", "@rules/AGENTS.md".to_owned(), false),
        // No component follows /home/, so these are directory roots, not account paths.
        (
            "c1-empty-home",
            format!("/homepage/ and a bare home folder: /{home}/"),
            false,
        ),
        ("json-empty-home", text_json(&format!("/{home}/")), false),
        // Exact symbolic references contain no literal; whitespace never changes this decision.
        ("c1-variable", format!("{token}=$OTHER_TOKEN"), false),
        (
            "c1-braced-variable",
            format!("{password}=${{OTHER_PASSWORD}}"),
            false,
        ),
        ("c1-identifier", format!("APP_{} = OTHER_KEY", "KEY"), false),
        ("r2-identifier", format!("{token} = OTHER_TOKEN\n"), false),
        ("unspaced-identifier", format!("{token}=OTHER_TOKEN"), false),
        ("tabbed-identifier", format!("{token}=\tOTHER_TOKEN"), false),
        (
            "c1-quoted-variable",
            format!("APP_{}=\"$OTHER_SECRET\"", "SECRET"),
            false,
        ),
        (
            "quoted-braced-variable",
            format!("{password}=\"${{OTHER_PASSWORD}}\""),
            false,
        ),
        (
            "single-quoted-variable",
            format!("{token}='$OTHER_TOKEN'"),
            false,
        ),
        // These have no address-shaped domain, private path, or assigned secret name.
        (
            "c1-relative",
            "\"path\": \"<base>/userhome/.claude/skills\"".to_owned(),
            false,
        ),
        ("c1-plugin", "\"superpowers@m\": []".to_owned(), false),
        ("c1-npm", "npm install -g @openai/codex".to_owned(), false),
        (
            "c1-bare-name",
            "only if you stored your own ARTIFICIAL_ANALYSIS_API_KEY, a key".to_owned(),
            false,
        ),
        (
            "c1-suffix-boundary",
            "AA_KEY_NAME = \"x\"".to_owned(),
            false,
        ),
        ("c1-non-email", "\"email\": \"fixture\"".to_owned(), false),
        ("secret-equality", format!("{token} == \"fixture\""), false),
        ("numeric-tld", format!("person{at}synthetic.123"), false),
    ];
    let mut out = Vec::new();
    for (name, text, forbidden) in rows {
        let json = text_json(&text);
        let nested = embedded(&json);
        for (encoding, text) in [("plain", text), ("json", json), ("embedded", nested)] {
            out.push(Probe {
                name: format!("{name}/{encoding}"),
                text,
                forbidden,
            });
        }
    }
    // More than the old raw-plus-one decode; an explicit worklist must have no depth cutoff.
    let mut deep = r#"{"author":"person\u0040synthetic.invalid"}"#.to_owned();
    for _ in 0..9 {
        deep = embedded(&deep);
    }
    out.push(Probe {
        name: "deep-embedded-email".to_owned(),
        text: deep,
        forbidden: true,
    });
    out
}
