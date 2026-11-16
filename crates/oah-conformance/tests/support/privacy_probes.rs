// 개인정보 거부 규약과 허용 대조군을 한 표에서 만들고 같은 입력을 여러 JSON 층으로 감싼다.
pub struct Probe {
    pub name: String,
    pub text: String,
    pub forbidden: bool,
    pub path: &'static str,
    pub bytes: Option<Vec<u8>>,
}

impl Probe {
    pub fn data(&self) -> &[u8] {
        self.bytes.as_deref().unwrap_or(self.text.as_bytes())
    }
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
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                for unit in c.encode_utf16(&mut [0; 2]) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
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
    let text_json = |text: &str| format!("{{\n  \"text\": {}\n}}\n", json_string(text));
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
            "{\"claude_after\": \"@AGENTS.md\\n\"}".to_owned(),
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
        ("c1-variable", format!("{token}=$OTHER_TOKEN"), false),
        (
            "c1-braced-variable",
            format!("{password}=${{OTHER_PASSWORD}}"),
            false,
        ),
        ("c1-identifier", format!("APP_{} = OTHER_KEY", "KEY"), true),
        ("r2-identifier", format!("{token} = OTHER_TOKEN\n"), true),
        ("unspaced-identifier", format!("{token}=OTHER_TOKEN"), true),
        ("tabbed-identifier", format!("{token}=\tOTHER_TOKEN"), true),
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
            "{\"path\": \"<base>/userhome/.claude/skills\"}".to_owned(),
            false,
        ),
        ("c1-plugin", "{\"superpowers@m\": []}".to_owned(), false),
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
        ("c1-non-email", "{\"email\": \"fixture\"}".to_owned(), false),
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
                text: text_json(&text),
                forbidden,
                path: "correction-probe.json",
                bytes: None,
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
        text: text_json(&deep),
        forbidden: true,
        path: "correction-probe.json",
        bytes: None,
    });
    let hex = |bytes: &[u8]| bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    let bytes_input = |data: &[u8]| format!("{{\"bytes\":{{\"input.txt\":\"{}\"}}}}", hex(data));
    let utf16 = |text: &str, little_endian: bool| {
        let mut bytes = if little_endian {
            vec![0xff, 0xfe]
        } else {
            vec![0xfe, 0xff]
        };
        for unit in text.encode_utf16() {
            bytes.extend_from_slice(&if little_endian {
                unit.to_le_bytes()
            } else {
                unit.to_be_bytes()
            });
        }
        bytes
    };
    let invalid = |text: &str| {
        let mut bytes = text.as_bytes().to_vec();
        bytes.push(0xff);
        bytes
    };
    let r3 = [
        (
            "hex-macos-path",
            bytes_input(&invalid(&format!("/{users}/synthetic/x\n"))),
            true,
        ),
        (
            "hex-email",
            bytes_input(&invalid(&format!("Copyright {address}\n"))),
            true,
        ),
        (
            "hex-utf16-email",
            bytes_input(&utf16(&format!("Copyright (c) 2026 {address}\n"), true)),
            true,
        ),
        (
            "hex-utf16-macos-path",
            bytes_input(&utf16(&format!("/{users}/synthetic/x\n"), true)),
            true,
        ),
        (
            "hex-secret",
            bytes_input(&invalid(&format!("{token}=fixture-value\n"))),
            true,
        ),
        (
            "lone-surrogate-escaped-email",
            r#"{"author":"\ud800 person\u0040synthetic.invalid"}"#.to_owned(),
            true,
        ),
        (
            "lone-low-surrogate-escaped-path",
            format!(r#"{{"p":"\udc00\u002f{users}\u002fsynthetic"}}"#),
            true,
        ),
        (
            "lone-surrogate-escaped-key",
            r#"{"APP_\u004bEY\ud800":"fixture-value"}"#.to_owned(),
            true,
        ),
        (
            "lone-surrogate-misaligns-next-email",
            r#"{"a":"\ud800","b":"person\u0040synthetic.invalid"}"#.to_owned(),
            true,
        ),
        (
            "lone-surrogate-misaligns-next-key",
            r#"{"a":"\udcff","APP_\u004bEY":"fixture-value"}"#.to_owned(),
            true,
        ),
        (
            "punycode-korean-tld-email",
            format!("person{at}synthetic.xn--3e0b707e"),
            true,
        ),
        (
            "toml-u8-escape-email",
            "author = \"person\\U00000040synthetic.invalid\"\n".to_owned(),
            true,
        ),
        (
            "yaml-x-escape-email",
            "author: \"person\\x40synthetic.invalid\"\n".to_owned(),
            true,
        ),
        ("ip-literal-email", format!("person{at}[192.0.2.1]"), true),
        (
            "punycode-tld-email",
            format!("person{at}synthetic.xn--p1ai"),
            true,
        ),
        (
            "dotless-domain-email",
            format!("person{at}localhost"),
            false,
        ),
        (
            "uppercase-email",
            format!("PERSON{at}SYNTHETIC.INVALID"),
            true,
        ),
        (
            "percent-encoded-email",
            "mailto:person%40synthetic.invalid".to_owned(),
            false,
        ),
        (
            "dockerfile-env-space",
            format!("ENV {token} fixture-value\n"),
            true,
        ),
        ("properties-space", format!("{token} fixture-value\n"), true),
        ("fish-set", format!("set -x {token} fixture-value\n"), true),
        ("r-arrow", format!("{token} <- \"fixture-value\"\n"), true),
        (
            "xml-element",
            format!("<{token}>fixture-value</{token}>"),
            true,
        ),
        (
            "lisp-setq",
            format!("(setq {token} \"fixture-value\")"),
            true,
        ),
        (
            "upper-literal-base32",
            format!("TOTP_{}={}{}\n", "SECRET", "JBSWY3DP", "EHPK3PXP"),
            true,
        ),
        (
            "upper-literal-akia",
            format!("AWS_{}_KEY={}{}\n", "SECRET", "AKIAIOSFODNN7", "EXAMPLE"),
            true,
        ),
        (
            "yaml-upper-literal",
            format!("{token}: FIXTUREVALUE\n"),
            true,
        ),
        (
            "json-newline-colon",
            format!("{{\"{token}\"\n:\n\"fixture\"}}"),
            true,
        ),
        (
            "invalid-json-windows",
            format!(r#"{{"p":"C:\{users}\synthetic"}}"#),
            true,
        ),
        ("walrus", format!("{token} := \"fixture\""), true),
        ("annotated", format!("{token}: str = \"fixture\""), true),
        (
            "env-subscript",
            format!("os.environ[\"{token}\"] = \"fixture\""),
            true,
        ),
        (
            "shell-default",
            format!("{token}=\"${{X:-fixture}}\""),
            true,
        ),
        ("kwarg", format!("call({token}=\"fixture\")"), true),
        ("clean-hex", bytes_input(b"caf\xe9\n"), false),
        ("bare-import", "@AGENTS.md".to_owned(), false),
    ];
    for (name, text, forbidden) in r3 {
        for (encoding, payload) in [("json", text.clone()), ("embedded", embedded(&text))] {
            out.push(Probe {
                name: format!("r3-{name}/{encoding}"),
                text: text_json(&payload),
                forbidden,
                path: "correction-probe.json",
                bytes: None,
            });
        }
    }
    for (name, payload) in [
        ("macos", format!("/{users}/synthetic/x")),
        ("linux", home_path("synthetic")),
        ("windows", windows),
        ("email", address),
        ("key", format!("APP_{}=fixture", "KEY")),
        ("token", format!("{token}=fixture")),
        ("secret", format!("APP_{}=fixture", "SECRET")),
        ("password", format!("{password}=fixture")),
    ] {
        for (encoding, bytes) in [
            ("utf8", invalid(&payload)),
            ("utf16le", utf16(&payload, true)),
            ("utf16be", utf16(&payload, false)),
        ] {
            out.push(Probe {
                name: format!("bytes-{name}/{encoding}"),
                text: text_json(&bytes_input(&bytes)),
                forbidden: true,
                path: "correction-probe.json",
                bytes: None,
            });
        }
    }
    for (name, text) in [
        ("not-json", "synthetic text"),
        ("empty", ""),
        ("compact", "{\"a\":1}\n"),
        ("indent", "{\n \"a\": 1\n}\n"),
        ("tab", "{\n\t\"a\": 1\n}\n"),
        ("unsorted", "{\n  \"b\": 1,\n  \"a\": 2\n}\n"),
        ("duplicate", "{\n  \"a\": 1,\n  \"a\": 2\n}\n"),
        ("missing-newline", "{}"),
        ("extra-newline", "{}\n\n"),
        ("leading-newline", "\n{}\n"),
        ("crlf", "{}\r\n"),
        ("trailing", "{}\n{}\n"),
        ("comma", "{\n  \"a\": 1,\n}\n"),
        ("non-json-escape", "\"\\q\"\n"),
        ("high-surrogate", "\"\\ud800\"\n"),
        ("low-surrogate", "\"\\udcff\"\n"),
        ("raw-control", "\"raw\ncontrol\"\n"),
        ("unterminated-string", "\"unfinished\n"),
        ("unnecessary-escape", "\"\\u0041\"\n"),
        ("slash-escape", "\"\\/\"\n"),
        ("non-ascii", "\"é\"\n"),
        ("uppercase-hex", "\"\\u00E9\"\n"),
        ("leading-zero", "01\n"),
        ("negative-zero-integer", "-0\n"),
        ("nan", "NaN\n"),
        ("infinity", "Infinity\n"),
        (
            "bad-bytes-hex",
            "{\n  \"bytes\": {\n    \"x\": \"ffx0\"\n  }\n}\n",
        ),
        (
            "odd-bytes-hex",
            "{\n  \"bytes\": {\n    \"x\": \"fff\"\n  }\n}\n",
        ),
        (
            "upper-bytes-hex",
            "{\n  \"bytes\": {\n    \"x\": \"FF\"\n  }\n}\n",
        ),
        (
            "non-string-bytes",
            "{\n  \"bytes\": {\n    \"x\": 12\n  }\n}\n",
        ),
    ] {
        out.push(Probe {
            name: format!("grammar-{name}"),
            text: text.to_owned(),
            forbidden: true,
            path: "correction-probe.json",
            bytes: None,
        });
    }
    for (name, text, forbidden) in [
        ("invalid-json-marker", "{", false),
        ("nan-input-marker", "{\"score\": NaN}", false),
        ("malformed-embedded-object", "{\"a\":}", true),
        ("malformed-embedded-array", "[1,]", true),
        ("toml-invalid-section", "[package\n", false),
        (
            "csv-multiline",
            "model,task\n\"model\nname\",sample\n",
            false,
        ),
        ("json-pair", "\"\\ud83d\\ude00\"", false),
        ("unsupported-body-control", "a\u{00}b", true),
        (
            "bare-upper-snake",
            concat!("SERVICE_TOKEN=", "OTHER_VALUE"),
            true,
        ),
        (
            "ordinary-hex",
            "706572736f6e4073796e7468657469632e696e76616c6964",
            false,
        ),
    ] {
        out.push(Probe {
            name: format!("grammar-{name}"),
            text: text_json(text),
            forbidden,
            path: "correction-probe.json",
            bytes: None,
        });
    }
    for (name, text, forbidden) in [
        ("scalar-string", "\"synthetic\"\n", false),
        ("float", "1.0\n", false),
        ("exponent-small", "1e-07\n", false),
        ("exponent-large", "1e+20\n", false),
        ("exponent-short", "1e-7\n", true),
        ("exponent-upper", "1E-07\n", true),
        ("source-id-escape", "\"\\u0073k-fixture\"\n", false),
        ("source-id-raw", "\"sk-fixture\"\n", true),
        ("unicode-pair", "\"\\ud83d\\ude00\"\n", false),
    ] {
        out.push(Probe {
            name: format!("grammar-{name}"),
            text: text.to_owned(),
            forbidden,
            path: "correction-probe.json",
            bytes: None,
        });
    }
    let format = include_str!("../../../../conformance/FORMAT.md");
    for (name, payload) in [
        ("macos", format!("/{users}/synthetic/x")),
        ("linux", home_path("synthetic")),
        ("windows", format!(r"C:\{users}\synthetic")),
        ("email", format!("person{at}synthetic.invalid")),
        ("punycode", format!("person{at}synthetic.xn--p1ai")),
        ("ip", format!("person{at}[192.0.2.1]")),
        ("key", format!("APP_{}=fixture", "KEY")),
        ("token", format!("{token}=fixture")),
        ("secret", format!("APP_{}=fixture", "SECRET")),
        ("password", format!("{password}=fixture")),
    ] {
        out.push(Probe {
            name: format!("plain-format-{name}"),
            text: format!("{format}\n{payload}\n"),
            forbidden: true,
            path: "FORMAT.md",
            bytes: None,
        });
    }
    for (name, text) in [
        ("format-heading", format.replace("## Layout", "## Other")),
        ("format-fence", format!("{format}\n```\n")),
        (
            "format-no-title",
            format.replacen("# Conformance fixtures", "Other", 1),
        ),
    ] {
        out.push(Probe {
            name: format!("grammar-{name}"),
            text,
            forbidden: true,
            path: "FORMAT.md",
            bytes: None,
        });
    }
    for (name, path, text, forbidden) in [
        ("attributes", ".gitattributes", "* -text\n", false),
        ("attributes-crlf", ".gitattributes", "* -text\r\n", true),
        (
            "attributes-extra",
            ".gitattributes",
            "* -text\nextra\n",
            true,
        ),
        (
            "format",
            "FORMAT.md",
            include_str!("../../../../conformance/FORMAT.md"),
            false,
        ),
        ("format-json", "FORMAT.md", "{}\n", true),
        (
            "nested-format",
            "nested/FORMAT.md",
            "# Conformance fixtures\n",
            true,
        ),
        (
            "nested-attributes",
            "nested/.gitattributes",
            "* -text\n",
            true,
        ),
    ] {
        out.push(Probe {
            name: format!("grammar-{name}"),
            text: text.to_owned(),
            forbidden,
            path,
            bytes: None,
        });
    }
    out.push(Probe {
        name: "grammar-invalid-utf8".to_owned(),
        text: "{}\n".to_owned(),
        forbidden: true,
        path: "correction-probe.json",
        bytes: Some(vec![0xff]),
    });
    out
}
