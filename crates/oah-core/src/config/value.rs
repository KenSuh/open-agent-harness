// 설정 값 모델: TOML 파서와 분리된 자체 값·표 형식과 v0.4.0 타입 이름, TOML 표기를 둔다.
use std::fmt;

/// A configuration value, independent of the TOML parser that produced it.
#[derive(Debug, Clone)]
pub enum Value {
    String(String),
    Integer(i64),
    Float(f64),
    Boolean(bool),
    Datetime(Datetime),
    Array(Vec<Value>),
    Table(Table),
}

/// Which of TOML's four date and time forms a value is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DatetimeKind {
    OffsetDateTime,
    LocalDateTime,
    LocalDate,
    LocalTime,
}

/// A TOML date or time, kept as its canonical text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Datetime {
    kind: DatetimeKind,
    text: String,
}

impl Datetime {
    pub fn kind(&self) -> DatetimeKind {
        self.kind
    }

    pub fn text(&self) -> &str {
        &self.text
    }
}

/// An ordered table. Equality ignores order, as TOML tables are unordered.
#[derive(Debug, Clone, Default)]
pub struct Table {
    entries: Vec<(String, Value)>,
}

impl Table {
    pub fn new() -> Self {
        Table::default()
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        self.entries
            .iter()
            .find(|(known, _)| known == key)
            .map(|(_, value)| value)
    }

    pub fn get_mut(&mut self, key: &str) -> Option<&mut Value> {
        self.entries
            .iter_mut()
            .find(|(known, _)| known == key)
            .map(|(_, value)| value)
    }

    pub fn contains_key(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    /// Sets `key`, keeping its position when it already exists and appending it otherwise.
    pub fn insert(&mut self, key: impl Into<String>, value: Value) -> Option<Value> {
        let key = key.into();
        match self.get_mut(&key) {
            Some(slot) => Some(std::mem::replace(slot, value)),
            None => {
                self.entries.push((key, value));
                None
            }
        }
    }

    pub fn remove(&mut self, key: &str) -> Option<Value> {
        let index = self.entries.iter().position(|(known, _)| known == key)?;
        Some(self.entries.remove(index).1)
    }

    /// Entries in insertion (file) order.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &Value)> {
        self.entries
            .iter()
            .map(|(key, value)| (key.as_str(), value))
    }

    pub fn keys(&self) -> impl Iterator<Item = &str> {
        self.entries.iter().map(|(key, _)| key.as_str())
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// The value at a path of keys through nested tables.
    pub fn get_path(&self, path: &[&str]) -> Option<&Value> {
        let (first, rest) = path.split_first()?;
        let value = self.get(first)?;
        if rest.is_empty() {
            Some(value)
        } else {
            value.as_table()?.get_path(rest)
        }
    }
}

impl PartialEq for Table {
    fn eq(&self, other: &Self) -> bool {
        self.len() == other.len()
            && self
                .iter()
                .all(|(key, value)| other.get(key) == Some(value))
    }
}

impl PartialEq for Value {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Value::String(a), Value::String(b)) => a == b,
            (Value::Integer(a), Value::Integer(b)) => a == b,
            // Bitwise, so a NaN equals itself and an unchanged file compares equal to itself.
            (Value::Float(a), Value::Float(b)) => a.to_bits() == b.to_bits(),
            (Value::Boolean(a), Value::Boolean(b)) => a == b,
            (Value::Datetime(a), Value::Datetime(b)) => a == b,
            (Value::Array(a), Value::Array(b)) => a == b,
            (Value::Table(a), Value::Table(b)) => a == b,
            _ => false,
        }
    }
}

impl Value {
    /// How v0.4.0 named this value's type in messages.
    pub fn type_word(&self) -> &'static str {
        match self {
            Value::Boolean(_) => "true or false",
            Value::Integer(_) => "an integer",
            Value::Float(_) => "a number",
            Value::String(_) => "a string",
            Value::Datetime(datetime) => match datetime.kind {
                DatetimeKind::OffsetDateTime | DatetimeKind::LocalDateTime => "a date-time",
                DatetimeKind::LocalDate => "a date",
                DatetimeKind::LocalTime => "a time",
            },
            Value::Array(_) => "an array",
            Value::Table(_) => "a table",
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(text) => Some(text),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Boolean(flag) => Some(*flag),
            _ => None,
        }
    }

    pub fn as_integer(&self) -> Option<i64> {
        match self {
            Value::Integer(number) => Some(*number),
            _ => None,
        }
    }

    pub fn as_float(&self) -> Option<f64> {
        match self {
            Value::Float(number) => Some(*number),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Value::Array(items) => Some(items),
            _ => None,
        }
    }

    pub fn as_table(&self) -> Option<&Table> {
        match self {
            Value::Table(table) => Some(table),
            _ => None,
        }
    }

    /// The value written as TOML, in the form OAH uses for the values it writes.
    pub fn to_toml(&self) -> String {
        let mut out = String::new();
        encode_value(self, &mut out);
        out
    }
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_toml())
    }
}

impl From<&str> for Value {
    fn from(text: &str) -> Self {
        Value::String(text.to_string())
    }
}

impl From<String> for Value {
    fn from(text: String) -> Self {
        Value::String(text)
    }
}

impl From<i64> for Value {
    fn from(number: i64) -> Self {
        Value::Integer(number)
    }
}

impl From<f64> for Value {
    fn from(number: f64) -> Self {
        Value::Float(number)
    }
}

impl From<bool> for Value {
    fn from(flag: bool) -> Self {
        Value::Boolean(flag)
    }
}

impl From<Vec<Value>> for Value {
    fn from(items: Vec<Value>) -> Self {
        Value::Array(items)
    }
}

impl From<Table> for Value {
    fn from(table: Table) -> Self {
        Value::Table(table)
    }
}

/// v0.4.0's rule for whether `value` may stand where `default` is: booleans only with booleans, a float default
/// takes integers and floats, an array default takes any array, otherwise the same type.
pub(crate) fn same_type(default: &Value, value: &Value) -> bool {
    match (default, value) {
        (Value::Boolean(_), Value::Boolean(_)) => true,
        (Value::Boolean(_), _) | (_, Value::Boolean(_)) => false,
        (Value::Float(_), Value::Integer(_) | Value::Float(_)) => true,
        (Value::Array(_), Value::Array(_)) => true,
        (Value::Integer(_), Value::Integer(_)) => true,
        (Value::String(_), Value::String(_)) => true,
        (Value::Table(_), Value::Table(_)) => true,
        (Value::Datetime(a), Value::Datetime(b)) => a.kind.python_type() == b.kind.python_type(),
        _ => false,
    }
}

impl DatetimeKind {
    /// Python's `datetime`, `date` and `time` types, which v0.4.0 compared.
    fn python_type(self) -> u8 {
        match self {
            DatetimeKind::OffsetDateTime | DatetimeKind::LocalDateTime => 0,
            DatetimeKind::LocalDate => 1,
            DatetimeKind::LocalTime => 2,
        }
    }
}

pub(crate) fn from_item(item: &toml_edit::Item) -> Option<Value> {
    match item {
        toml_edit::Item::None => None,
        toml_edit::Item::Value(value) => Some(from_value(value)),
        toml_edit::Item::Table(table) => Some(Value::Table(from_table(table))),
        toml_edit::Item::ArrayOfTables(tables) => Some(Value::Array(
            tables
                .iter()
                .map(|table| Value::Table(from_table(table)))
                .collect(),
        )),
    }
}

pub(crate) fn from_table(table: &toml_edit::Table) -> Table {
    let mut out = Table::new();
    for (key, item) in table.iter() {
        if let Some(value) = from_item(item) {
            out.insert(key, value);
        }
    }
    out
}

pub(crate) fn from_value(value: &toml_edit::Value) -> Value {
    match value {
        toml_edit::Value::String(text) => Value::String(text.value().clone()),
        toml_edit::Value::Integer(number) => Value::Integer(*number.value()),
        toml_edit::Value::Float(number) => Value::Float(*number.value()),
        toml_edit::Value::Boolean(flag) => Value::Boolean(*flag.value()),
        toml_edit::Value::Datetime(datetime) => {
            let datetime = datetime.value();
            let kind = match (
                datetime.date.is_some(),
                datetime.time.is_some(),
                datetime.offset.is_some(),
            ) {
                (true, true, true) => DatetimeKind::OffsetDateTime,
                (true, true, false) => DatetimeKind::LocalDateTime,
                (true, false, _) => DatetimeKind::LocalDate,
                _ => DatetimeKind::LocalTime,
            };
            Value::Datetime(Datetime {
                kind,
                text: datetime.to_string(),
            })
        }
        toml_edit::Value::Array(items) => Value::Array(items.iter().map(from_value).collect()),
        toml_edit::Value::InlineTable(table) => {
            let mut out = Table::new();
            for (key, item) in table.iter() {
                out.insert(key, from_value(item));
            }
            Value::Table(out)
        }
    }
}

/// A key as TOML: bare when it can be, otherwise a basic string.
pub(crate) fn encode_key(key: &str) -> String {
    if !key.is_empty()
        && key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    {
        key.to_string()
    } else {
        let mut out = String::new();
        encode_string(key, &mut out);
        out
    }
}

fn encode_value(value: &Value, out: &mut String) {
    match value {
        Value::String(text) => encode_string(text, out),
        Value::Integer(number) => out.push_str(&number.to_string()),
        Value::Float(number) => {
            if number.is_nan() {
                out.push_str("nan");
            } else if number.is_infinite() {
                out.push_str(if *number > 0.0 { "inf" } else { "-inf" });
            } else {
                // Debug always shows a fraction or an exponent, which TOML needs for a float.
                out.push_str(&format!("{number:?}"));
            }
        }
        Value::Boolean(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Datetime(datetime) => out.push_str(&datetime.text),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                encode_value(item, out);
            }
            out.push(']');
        }
        Value::Table(table) => {
            if table.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push_str("{ ");
            for (index, (key, item)) in table.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                out.push_str(&encode_key(key));
                out.push_str(" = ");
                encode_value(item, out);
            }
            out.push_str(" }");
        }
    }
}

fn encode_string(text: &str, out: &mut String) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{8}' => out.push_str("\\b"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\u{c}' => out.push_str("\\f"),
            '\r' => out.push_str("\\r"),
            ch if (ch as u32) < 0x20 || ch as u32 == 0x7f => {
                out.push_str(&format!("\\u{:04X}", ch as u32));
            }
            ch => out.push(ch),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(text: &str) -> Table {
        from_table(toml_edit::Document::parse(text).unwrap().as_table())
    }

    #[test]
    fn encoded_values_parse_back_to_the_same_value() {
        let mut inner = Table::new();
        inner.insert("model", Value::from(""));
        inner.insert("needs quote", Value::from("a \"b\" \\ c\n\t\u{1}"));
        let values = vec![
            Value::from("plain"),
            Value::from(-7i64),
            Value::from(24.0),
            Value::from(1e-7),
            Value::from(true),
            Value::Array(vec![Value::from("a"), Value::from(2i64)]),
            Value::Table(inner),
            Value::Array(Vec::new()),
            Value::Table(Table::new()),
        ];
        for value in values {
            let text = format!("k = {}\n", value.to_toml());
            let parsed = parse(&text);
            assert_eq!(parsed.get("k"), Some(&value), "{text}");
        }
    }

    #[test]
    fn keys_are_bare_only_when_toml_allows_it() {
        assert_eq!(encode_key("model-a_1"), "model-a_1");
        assert_eq!(encode_key("a.b"), "\"a.b\"");
        assert_eq!(encode_key(""), "\"\"");
        assert_eq!(encode_key("모델"), "\"모델\"");
    }

    #[test]
    fn table_equality_ignores_order_but_array_equality_does_not() {
        assert_eq!(parse("a = 1\nb = 2\n"), parse("b = 2\na = 1\n"));
        assert_ne!(parse("a = [1, 2]\n"), parse("a = [2, 1]\n"));
    }

    #[test]
    fn type_words_follow_v040() {
        let table = parse(
            "a = 2024-01-02T03:04:05Z\nb = 2024-01-02T03:04:05\nc = 2024-01-02\nd = 03:04:05\ne = 1.5\n",
        );
        let words: Vec<&str> = ["a", "b", "c", "d", "e"]
            .iter()
            .map(|key| table.get(key).unwrap().type_word())
            .collect();
        assert_eq!(
            words,
            ["a date-time", "a date-time", "a date", "a time", "a number"]
        );
    }

    #[test]
    fn a_float_default_takes_an_integer_but_a_bool_never_stands_for_a_number() {
        assert!(same_type(&Value::from(1.0), &Value::from(2i64)));
        assert!(!same_type(&Value::from(1i64), &Value::from(2.0)));
        assert!(!same_type(&Value::from(1i64), &Value::from(true)));
        assert!(!same_type(&Value::from(true), &Value::from(1i64)));
    }
}
