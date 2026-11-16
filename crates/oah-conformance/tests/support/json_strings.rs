// 엄격한 JSON 문법과 추출기의 정규 바이트를 검사하고 문자열 디코딩 실패는 즉시 거부한다.
use std::collections::BTreeMap;

#[derive(Debug)]
pub enum Value {
    Scalar(String),
    String(String),
    Array(Vec<Value>),
    Object(BTreeMap<String, Value>),
}

pub fn document(text: &str) -> Option<Value> {
    parse_document(text, false)
}

pub fn input_document(text: &str) -> Option<Value> {
    parse_document(text, true)
}

fn parse_document(text: &str, invalid_input_marker: bool) -> Option<Value> {
    let mut chars = text.chars();
    let value = value(&mut chars, invalid_input_marker)?;
    whitespace(&mut chars);
    chars.as_str().is_empty().then_some(value)
}

pub fn canonical_document(text: &str) -> Option<Value> {
    let value = document(text)?;
    let mut canonical = render(&value, 0);
    canonical.push('\n');
    (canonical == text).then_some(value)
}

fn whitespace(chars: &mut std::str::Chars<'_>) {
    while chars.clone().next().is_some_and(|c| " \t\r\n".contains(c)) {
        chars.next();
    }
}

fn take(chars: &mut std::str::Chars<'_>, expected: char) -> Option<()> {
    (chars.next()? == expected).then_some(())
}

fn value(chars: &mut std::str::Chars<'_>, invalid_input_marker: bool) -> Option<Value> {
    whitespace(chars);
    match chars.clone().next()? {
        '"' => {
            chars.next();
            Some(Value::String(string(chars)?))
        }
        '{' => {
            chars.next();
            whitespace(chars);
            let mut fields = BTreeMap::new();
            if chars.clone().next() != Some('}') {
                loop {
                    whitespace(chars);
                    take(chars, '"')?;
                    let key = string(chars)?;
                    whitespace(chars);
                    take(chars, ':')?;
                    if fields
                        .insert(key, value(chars, invalid_input_marker)?)
                        .is_some()
                    {
                        return None;
                    }
                    whitespace(chars);
                    if chars.clone().next() != Some(',') {
                        break;
                    }
                    chars.next();
                }
            }
            take(chars, '}')?;
            Some(Value::Object(fields))
        }
        '[' => {
            chars.next();
            whitespace(chars);
            let mut items = Vec::new();
            if chars.clone().next() != Some(']') {
                loop {
                    items.push(value(chars, invalid_input_marker)?);
                    whitespace(chars);
                    if chars.clone().next() != Some(',') {
                        break;
                    }
                    chars.next();
                }
            }
            take(chars, ']')?;
            Some(Value::Array(items))
        }
        't' | 'f' | 'n' => {
            let literal = match chars.clone().next()? {
                't' => "true",
                'f' => "false",
                'n' => "null",
                _ => unreachable!(),
            };
            for c in literal.chars() {
                take(chars, c)?;
            }
            Some(Value::Scalar(literal.to_owned()))
        }
        'N' if invalid_input_marker => {
            for c in "NaN".chars() {
                take(chars, c)?;
            }
            Some(Value::Scalar("NaN".to_owned()))
        }
        '-' | '0'..='9' => {
            let start = chars.as_str();
            while chars
                .clone()
                .next()
                .is_some_and(|c| c.is_ascii_digit() || "-.eE+".contains(c))
            {
                chars.next();
            }
            let number = &start[..start.len() - chars.as_str().len()];
            Some(Value::Scalar(number_value(number)?))
        }
        _ => None,
    }
}

fn number_value(text: &str) -> Option<String> {
    let mut chars = text.chars();
    if chars.clone().next() == Some('-') {
        chars.next();
    }
    match chars.next()? {
        '0' => {}
        '1'..='9' => {
            while chars.clone().next().is_some_and(|c| c.is_ascii_digit()) {
                chars.next();
            }
        }
        _ => return None,
    }
    if chars.clone().next() == Some('.') {
        chars.next();
        let before = chars.as_str().len();
        while chars.clone().next().is_some_and(|c| c.is_ascii_digit()) {
            chars.next();
        }
        if chars.as_str().len() == before {
            return None;
        }
    }
    if chars.clone().next().is_some_and(|c| "eE".contains(c)) {
        chars.next();
        if chars.clone().next().is_some_and(|c| "+-".contains(c)) {
            chars.next();
        }
        let before = chars.as_str().len();
        while chars.clone().next().is_some_and(|c| c.is_ascii_digit()) {
            chars.next();
        }
        if chars.as_str().len() == before {
            return None;
        }
    }
    if !chars.as_str().is_empty() {
        return None;
    }
    if !text.contains(['.', 'e', 'E']) {
        return Some(if text == "-0" { "0" } else { text }.to_owned());
    }
    let number: f64 = text.parse().ok()?;
    if !number.is_finite() {
        return None;
    }
    let repr = format!("{number:?}");
    match repr.split_once('e') {
        Some((mantissa, exponent)) => {
            let exponent: i32 = exponent.parse().ok()?;
            Some(format!("{mantissa}e{exponent:+03}"))
        }
        None => Some(repr),
    }
}

fn render(value: &Value, depth: usize) -> String {
    let indent = "  ".repeat(depth);
    let child = "  ".repeat(depth + 1);
    match value {
        Value::Scalar(text) => text.clone(),
        Value::String(text) => quoted(text),
        Value::Array(items) if items.is_empty() => "[]".to_owned(),
        Value::Array(items) => format!(
            "[\n{}\n{indent}]",
            items
                .iter()
                .map(|item| format!("{child}{}", render(item, depth + 1)))
                .collect::<Vec<_>>()
                .join(",\n")
        ),
        Value::Object(fields) if fields.is_empty() => "{}".to_owned(),
        Value::Object(fields) => format!(
            "{{\n{}\n{indent}}}",
            fields
                .iter()
                .map(|(key, value)| format!("{child}{}: {}", quoted(key), render(value, depth + 1)))
                .collect::<Vec<_>>()
                .join(",\n")
        ),
    }
}

fn quoted(text: &str) -> String {
    let mut out = String::from("\"");
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            ' '..='~' => out.push(c),
            _ => {
                for unit in c.encode_utf16(&mut [0; 2]) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
    // The extractor's sole non-default escape: source ids retain their decoded value.
    let mut escaped = String::new();
    let mut start = 0;
    for (at, _) in out.match_indices("sk-") {
        if out[..at]
            .chars()
            .next_back()
            .is_some_and(|c| c.is_ascii_alphanumeric() || c == '\\')
        {
            continue;
        }
        escaped.push_str(&out[start..at]);
        escaped.push_str("\\u0073");
        start = at + 1;
    }
    escaped.push_str(&out[start..]);
    escaped
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
