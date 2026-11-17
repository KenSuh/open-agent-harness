// 설정 파일 원문 층: 바이트·BOM·디코딩된 텍스트·파싱된 값을 나눠 들고, v0.4.0 규칙으로 섹션을 기본값과 병합한다.
use super::error::{ConfigError, Notice, notice_codes};
use super::value::{self, Table, Value, same_type};
use crate::fsx;
use std::path::{Path, PathBuf};

const BOM: &[u8] = b"\xEF\xBB\xBF";

/// One configuration file as read: its exact bytes, its text and its parsed values, kept apart.
///
/// Reading never creates or changes anything; a missing file reads as an empty one.
#[derive(Debug, Clone)]
pub struct ConfigFile {
    path: PathBuf,
    where_: String,
    bytes: Option<Vec<u8>>,
    bom: bool,
    text: String,
    values: Table,
}

impl ConfigFile {
    /// Reads `path`. Messages name the file by its path.
    pub fn read(path: &Path) -> Result<ConfigFile, ConfigError> {
        let where_ = path.display().to_string();
        let bytes = fsx::read_optional(path).map_err(|error| {
            let reason = error
                .io_error()
                .map(|io| io.to_string())
                .unwrap_or_else(|| error.to_string());
            ConfigError::invalid(format!(
                "{where_} cannot be read ({reason}); make it a readable file or remove it to use the defaults"
            ))
        })?;
        ConfigFile::from_bytes(path, bytes)
    }

    /// Interprets `bytes` as the content of `path` (`None` for a missing file) without touching the disk.
    pub fn from_bytes(path: &Path, bytes: Option<Vec<u8>>) -> Result<ConfigFile, ConfigError> {
        let where_ = path.display().to_string();
        let Some(raw) = bytes else {
            return Ok(ConfigFile {
                path: path.to_path_buf(),
                where_,
                bytes: None,
                bom: false,
                text: String::new(),
                values: Table::new(),
            });
        };
        let (bom, body) = match raw.strip_prefix(BOM) {
            Some(rest) => (true, rest),
            None => (false, raw.as_slice()),
        };
        let text = match std::str::from_utf8(body) {
            Ok(text) => text.to_string(),
            Err(error) => {
                let offset = error.valid_up_to() + if bom { BOM.len() } else { 0 };
                return Err(ConfigError::invalid(format!(
                    "{where_} is not UTF-8 (byte {offset}); save it as UTF-8"
                )));
            }
        };
        let values = parse_values(&text, &where_)?;
        Ok(ConfigFile {
            path: path.to_path_buf(),
            where_,
            bytes: Some(raw),
            bom,
            text,
            values,
        })
    }

    /// The path the file was read from.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// How messages name the file.
    pub fn where_(&self) -> &str {
        &self.where_
    }

    /// Whether the file existed.
    pub fn exists(&self) -> bool {
        self.bytes.is_some()
    }

    /// The exact bytes read, or `None` for a missing file.
    pub fn source_bytes(&self) -> Option<&[u8]> {
        self.bytes.as_deref()
    }

    /// Whether the file starts with a UTF-8 byte order mark.
    pub fn has_bom(&self) -> bool {
        self.bom
    }

    /// The decoded text without the byte order mark.
    pub fn text(&self) -> &str {
        &self.text
    }

    /// Every parsed value, in file order.
    pub fn values(&self) -> &Table {
        &self.values
    }

    /// The file's `[name]` table merged over `defaults` by v0.4.0's rules, with 1.0's one change: an unknown key
    /// is a notice and is left out of the result instead of failing the read.
    ///
    /// A missing table is the defaults. A value of the wrong type, or a non-table where a table belongs, is
    /// `OAH_CONFIG_INVALID` with v0.4.0's wording.
    pub fn section(&self, name: &str, defaults: &Table) -> Result<Merged, ConfigError> {
        let mut merger = Merger::new(&self.where_);
        let table = match self.values.get(name) {
            None => defaults.clone(),
            Some(Value::Table(given)) => merger.merge(defaults, given, name)?.0,
            Some(other) => return Err(not_a_table(&self.where_, name, other)),
        };
        Ok(Merged {
            table,
            notices: merger.notices,
        })
    }
}

/// A merged section and the notices found while merging it.
#[derive(Debug, Clone, PartialEq)]
pub struct Merged {
    pub table: Table,
    pub notices: Vec<Notice>,
}

pub(crate) fn parse_values(text: &str, where_: &str) -> Result<Table, ConfigError> {
    let document =
        toml_edit::ImDocument::parse(text).map_err(|error| invalid_toml(text, where_, &error))?;
    Ok(value::from_table(document.as_table()))
}

pub(crate) fn invalid_toml(text: &str, where_: &str, error: &toml_edit::TomlError) -> ConfigError {
    let location = match error.span() {
        Some(span) => {
            let before = &text[..span.start.min(text.len())];
            let line = before.matches('\n').count() + 1;
            let column = before
                .rsplit('\n')
                .next()
                .map_or(0, |tail| tail.chars().count())
                + 1;
            format!("line {line}, column {column}: ")
        }
        None => String::new(),
    };
    ConfigError::invalid(format!(
        "{where_} is not valid TOML ({location}{}); fix the syntax or remove the file to use the defaults",
        error.message().trim()
    ))
}

pub(crate) fn not_a_table(where_: &str, name: &str, value: &Value) -> ConfigError {
    ConfigError::invalid(format!(
        "{where_}: {name} must be a table, not {}; write it as a [{name}] section",
        value.type_word()
    ))
}

pub(crate) fn type_error(where_: &str, name: &str, default: &Value, value: &Value) -> ConfigError {
    let expected = default.type_word();
    ConfigError::invalid(format!(
        "{where_}: {name} must be {expected}, not {}; change it to {expected} or remove it to use the default",
        value.type_word()
    ))
}

/// Decides, from a key path relative to the merged table, whether the key is kept in the file unused.
pub(crate) type KeptUnused<'a> = &'a dyn Fn(&[String]) -> bool;

/// v0.4.0's section merge, collecting unknown keys as notices.
pub(crate) struct Merger<'a> {
    where_: &'a str,
    pub(crate) notices: Vec<Notice>,
    /// Keys, relative to the merged table, that 1.0 keeps in the file and does not use.
    kept_unused: Option<KeptUnused<'a>>,
    pub(crate) unused: Vec<String>,
}

impl<'a> Merger<'a> {
    pub(crate) fn new(where_: &'a str) -> Self {
        Merger {
            where_,
            notices: Vec::new(),
            kept_unused: None,
            unused: Vec::new(),
        }
    }

    /// A merger that leaves the keys `kept_unused` accepts out of the result without a notice, recording them in
    /// `unused` instead.
    pub(crate) fn keeping_unused(where_: &'a str, kept_unused: KeptUnused<'a>) -> Self {
        Merger {
            kept_unused: Some(kept_unused),
            ..Merger::new(where_)
        }
    }

    /// Merges `given` over `defaults`; returns the merged table and the part of `given` that was used.
    pub(crate) fn merge(
        &mut self,
        defaults: &Table,
        given: &Table,
        dotted: &str,
    ) -> Result<(Table, Table), ConfigError> {
        let mut relative = Vec::new();
        self.merge_at(defaults, given, dotted, &mut relative)
    }

    fn merge_at(
        &mut self,
        defaults: &Table,
        given: &Table,
        dotted: &str,
        relative: &mut Vec<String>,
    ) -> Result<(Table, Table), ConfigError> {
        let mut merged = defaults.clone();
        let mut used = Table::new();
        for (key, value) in given.iter() {
            let name = format!("{dotted}.{key}");
            relative.push(key.to_string());
            let outcome = self.merge_key(
                defaults,
                key,
                value,
                &name,
                relative,
                &mut merged,
                &mut used,
            );
            relative.pop();
            outcome?;
        }
        Ok((merged, used))
    }

    #[allow(clippy::too_many_arguments)]
    fn merge_key(
        &mut self,
        defaults: &Table,
        key: &str,
        value: &Value,
        name: &str,
        relative: &mut Vec<String>,
        merged: &mut Table,
        used: &mut Table,
    ) -> Result<(), ConfigError> {
        if let Some(kept_unused) = self.kept_unused
            && kept_unused(relative)
        {
            self.unused.push(relative.join("."));
            return Ok(());
        }
        match defaults.get(key) {
            None if defaults.is_empty() => {
                merged.insert(key, value.clone());
                used.insert(key, value.clone());
            }
            None => {
                let mut known: Vec<&str> = defaults.keys().collect();
                known.sort_unstable();
                self.notices.push(Notice::new(
                    notice_codes::UNKNOWN_KEY,
                    format!(
                        "{}: unknown key {name}; remove it or use one of: {}",
                        self.where_,
                        known.join(", ")
                    ),
                    vec![name.to_string()],
                ));
            }
            Some(Value::Table(default)) => {
                let Value::Table(given) = value else {
                    return Err(type_error(
                        self.where_,
                        name,
                        &Value::Table(default.clone()),
                        value,
                    ));
                };
                let (inner, inner_used) = self.merge_at(default, given, name, relative)?;
                merged.insert(key, Value::Table(inner));
                // A given table whose keys were all left out is not a used key itself.
                if !inner_used.is_empty() || given.is_empty() {
                    used.insert(key, Value::Table(inner_used));
                }
            }
            Some(default) if same_type(default, value) => {
                let value = match (default, value) {
                    (Value::Float(_), Value::Integer(number)) => Value::Float(*number as f64),
                    _ => value.clone(),
                };
                merged.insert(key, value.clone());
                used.insert(key, value);
            }
            Some(default) => return Err(type_error(self.where_, name, default, value)),
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn file(text: &str) -> ConfigFile {
        ConfigFile::from_bytes(
            Path::new(".oah/config.toml"),
            Some(text.as_bytes().to_vec()),
        )
        .unwrap()
    }

    fn defaults(text: &str) -> Table {
        parse_values(text, "defaults").unwrap()
    }

    #[test]
    fn a_missing_file_is_empty_and_every_section_is_its_defaults() {
        let config = ConfigFile::from_bytes(Path::new("x/config.toml"), None).unwrap();
        assert!(!config.exists());
        let wanted = defaults("a = 1\n");
        assert_eq!(config.section("s", &wanted).unwrap().table, wanted);
    }

    #[test]
    fn nested_tables_merge_key_by_key() {
        let config = file("[s.inner]\nb = 3\n");
        let merged = config
            .section("s", &defaults("[inner]\na = 1\nb = 2\n"))
            .unwrap();
        assert_eq!(merged.table, defaults("[inner]\na = 1\nb = 3\n"));
        assert!(merged.notices.is_empty());
    }

    #[test]
    fn an_unknown_key_is_a_notice_and_is_left_out() {
        let config = file("[s]\na = 5\nzz = 1\n");
        let merged = config.section("s", &defaults("a = 1\nb = 2\n")).unwrap();
        assert_eq!(merged.table, defaults("a = 5\nb = 2\n"));
        assert_eq!(merged.notices.len(), 1);
        assert_eq!(merged.notices[0].code(), notice_codes::UNKNOWN_KEY);
        assert_eq!(merged.notices[0].keys(), ["s.zz".to_string()]);
    }

    #[test]
    fn an_empty_default_table_accepts_any_key() {
        let config = file("[s.open]\n\"model-a\" = \"high\"\n");
        let merged = config.section("s", &defaults("[open]\n")).unwrap();
        assert_eq!(merged.table, defaults("[open]\n\"model-a\" = \"high\"\n"));
    }

    #[test]
    fn the_used_part_holds_only_given_known_keys() {
        let config = file("[s]\na = 5\nzz = 1\n[s.inner]\nq = 1\n");
        let mut merger = Merger::new("f");
        let given = config
            .values()
            .get("s")
            .unwrap()
            .as_table()
            .unwrap()
            .clone();
        let (_, used) = merger
            .merge(&defaults("a = 1\n[inner]\nb = 2\n"), &given, "s")
            .unwrap();
        assert_eq!(used, defaults("a = 5\n"));
    }

    #[test]
    fn a_crlf_file_with_a_bom_reads_like_lf() {
        let mut bytes = BOM.to_vec();
        bytes.extend_from_slice(b"[s]\r\na = 2\r\n");
        let config = ConfigFile::from_bytes(Path::new("f"), Some(bytes)).unwrap();
        assert!(config.has_bom());
        assert_eq!(config.values(), &defaults("[s]\na = 2\n"));
    }
}
