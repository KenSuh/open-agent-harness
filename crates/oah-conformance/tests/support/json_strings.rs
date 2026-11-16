// JSON 문자열의 범위와 값을 함께 읽어 직렬화 구문을 개인정보의 일부로 오인하지 않게 한다.
pub struct JsonString {
    pub start: usize,
    pub end: usize,
    pub value: String,
}

pub fn strings(text: &str) -> Vec<JsonString> {
    let mut result = Vec::new();
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        if c == '"' {
            let start = text.len() - chars.as_str().len() - 1;
            let mut candidate = chars.clone();
            if let Some(value) = string(&mut candidate) {
                result.push(JsonString {
                    start,
                    end: text.len() - candidate.as_str().len(),
                    value,
                });
                chars = candidate;
            }
        }
    }
    result
}

fn hex_quad(chars: &mut std::str::Chars<'_>) -> Option<u32> {
    (0..4).try_fold(0, |value, _| Some(value * 16 + chars.next()?.to_digit(16)?))
}

pub fn string(chars: &mut std::str::Chars<'_>) -> Option<String> {
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
        assert_eq!(string(&mut text.chars()).as_deref(), expected, "{text}");
    }
}
