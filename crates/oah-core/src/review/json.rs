// 파이썬 json.dumps 형식: v0.4.0 이 해시를 매기는 두 직렬화(빈칸 없는 정렬형, indent=1 정렬형)를 같은 바이트로 만든다.
//! The two JSON spellings v0.4.0 hashes, byte for byte.
//!
//! * [`canonical`]: `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` names
//!   records (`id`), specs (`spec_digest`), content (`content_key`), identities and policies.
//! * [`record_bytes`]: `json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"` is a stored
//!   snapshot manifest, whose SHA-256 is the snapshot id.
//!
//! Keys sort by code point, as Python sorts `str`; text keeps non-ASCII characters and escapes only `"`, `\` and
//! control characters; integers are decimal and floats are written as Python's `repr`.

use serde_json::{Number, Value};
use sha2::{Digest, Sha256};

/// The compact sorted form.
pub(super) fn canonical(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, None, 0);
    out
}

/// The stored form of an immutable record: one-space indent, sorted keys, final newline.
pub(super) fn record_bytes(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, Some(1), 0);
    out.push('\n');
    out
}

/// `sha256:` and the SHA-256 of the canonical form (v0.4.0 `store.digest` and `state.digest_of`).
pub(super) fn digest(value: &Value) -> String {
    format!("sha256:{}", sha256_hex(canonical(value).as_bytes()))
}

/// The lower-case hex SHA-256 of bytes.
pub(super) fn sha256_hex(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn write_value(out: &mut String, value: &Value, indent: Option<usize>, level: usize) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => out.push_str(&number_text(number)),
        Value::String(text) => write_text(out, text),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                break_line(out, indent, level + 1);
                write_value(out, item, indent, level + 1);
            }
            break_line(out, indent, level);
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|left, right| left.0.cmp(right.0));
            out.push('{');
            for (index, (key, item)) in entries.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                break_line(out, indent, level + 1);
                write_text(out, key);
                out.push_str(if indent.is_some() { ": " } else { ":" });
                write_value(out, item, indent, level + 1);
            }
            break_line(out, indent, level);
            out.push('}');
        }
    }
}

fn break_line(out: &mut String, indent: Option<usize>, level: usize) {
    if let Some(width) = indent {
        out.push('\n');
        out.extend(std::iter::repeat_n(' ', width * level));
    }
}

/// A JSON string as Python writes it with `ensure_ascii=False`.
fn write_text(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if u32::from(c) < 0x20 => out.push_str(&format!("\\u{:04x}", u32::from(c))),
            c => out.push(c),
        }
    }
    out.push('"');
}

pub(super) fn number_text(number: &Number) -> String {
    if let Some(value) = number.as_u64() {
        return value.to_string();
    }
    if let Some(value) = number.as_i64() {
        return value.to_string();
    }
    number
        .as_f64()
        .map_or_else(|| number.to_string(), python_float)
}

/// Python's `repr(float)`: the shortest round-trip digits (Rust's `{:e}` gives the same digits), fixed notation
/// when the decimal point falls within 16 digits and after the fourth place past zero, else `d.ddde+XX`.
fn python_float(value: f64) -> String {
    let text = format!("{value:e}");
    let (mantissa, exponent) = text.split_once('e').unwrap_or((text.as_str(), "0"));
    let exponent: i64 = exponent.parse().unwrap_or(0);
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let point = exponent + 1;
    if -4 < point && point <= 16 {
        let zeros = |count: i64| "0".repeat(usize::try_from(count).unwrap_or(0));
        let count = i64::try_from(digits.len()).unwrap_or(i64::MAX);
        if point <= 0 {
            format!("{sign}0.{}{digits}", zeros(-point))
        } else if point >= count {
            format!("{sign}{digits}{}.0", zeros(point - count))
        } else {
            let split = usize::try_from(point).unwrap_or(0);
            format!("{sign}{}.{}", &digits[..split], &digits[split..])
        }
    } else {
        let (first, rest) = digits.split_at(1);
        let mantissa = if rest.is_empty() {
            first.to_string()
        } else {
            format!("{first}.{rest}")
        };
        let mark = if exponent < 0 { '-' } else { '+' };
        format!("{sign}{mantissa}e{mark}{:02}", exponent.abs())
    }
}

#[cfg(test)]
mod tests {
    use super::{canonical, python_float, record_bytes};
    use serde_json::json;

    #[test]
    fn canonical_sorts_keys_and_keeps_text() {
        let value = json!({"b": [1, -2, 1.5], "a": {"é": "\"x\"\n\u{1}\u{7f}"}, "c": null});
        assert_eq!(
            canonical(&value),
            "{\"a\":{\"é\":\"\\\"x\\\"\\n\\u0001\u{7f}\"},\"b\":[1,-2,1.5],\"c\":null}"
        );
    }

    #[test]
    fn record_bytes_indent_one_space() {
        let value = json!({"b": [], "a": {"x": [true]}, "c": {}});
        assert_eq!(
            record_bytes(&value),
            "{\n \"a\": {\n  \"x\": [\n   true\n  ]\n },\n \"b\": [],\n \"c\": {}\n}\n"
        );
    }

    #[test]
    fn floats_read_as_python_repr() {
        // Each pair is (value, repr(value)) as python3.11 prints it.
        for (value, text) in [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.5, "1.5"),
            (100.0, "100.0"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (123456789.125, "123456789.125"),
            (1.5e-7, "1.5e-07"),
            (2.5e300, "2.5e+300"),
            (0.1, "0.1"),
        ] {
            assert_eq!(python_float(value), text, "{value}");
        }
    }
}
