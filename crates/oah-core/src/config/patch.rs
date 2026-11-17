// 형식 보존 패치: toml_edit 위치 정보로 바꿀 키의 바이트만 고치고, 결과를 다시 읽어 의도한 변경만 일어났는지 확인한다.
use super::error::{ConfigError, codes};
use super::file::{invalid_toml, parse_values};
use super::value::{Table, Value, encode_key};
use std::collections::BTreeMap;

/// One logical change to a configuration file.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Op {
    /// Sets the value at a path; the last part is the key, the rest names its table.
    Set { path: Vec<String>, value: Value },
    /// Removes the key at a path, if present.
    Remove { path: Vec<String> },
    /// Replaces the key at a path with other keys of the same table, written where it stood.
    Rename {
        path: Vec<String>,
        entries: Vec<(String, Value)>,
    },
}

impl Op {
    fn path(&self) -> &[String] {
        match self {
            Op::Set { path, .. } | Op::Remove { path } | Op::Rename { path, .. } => path,
        }
    }
}

/// The tree `values` becomes after `ops`. Fails when a path runs through a value that is not a table.
pub(crate) fn apply_to_tree(values: &Table, ops: &[Op]) -> Result<Table, String> {
    let mut tree = values.clone();
    for op in ops {
        let path = op.path();
        let (key, parents) = path.split_last().ok_or("an empty key path")?;
        match op {
            Op::Set { value, .. } => {
                table_at(&mut tree, parents, true)?
                    .expect("created")
                    .insert(key.clone(), value.clone());
            }
            Op::Remove { .. } => {
                if let Some(table) = table_at(&mut tree, parents, false)? {
                    table.remove(key);
                }
            }
            Op::Rename { entries, .. } => {
                let table = table_at(&mut tree, parents, true)?.expect("created");
                table.remove(key);
                for (name, value) in entries {
                    table.insert(name.clone(), value.clone());
                }
            }
        }
    }
    Ok(tree)
}

fn table_at<'a>(
    tree: &'a mut Table,
    path: &[String],
    create: bool,
) -> Result<Option<&'a mut Table>, String> {
    let mut table = tree;
    for part in path {
        if !table.contains_key(part) {
            if !create {
                return Ok(None);
            }
            table.insert(part.clone(), Value::Table(Table::new()));
        }
        table = match table.get_mut(part) {
            Some(Value::Table(inner)) => inner,
            _ => return Err(format!("{} is not a table", dotted(path))),
        };
    }
    Ok(Some(table))
}

pub(crate) fn dotted(path: &[String]) -> String {
    path.join(".")
}

/// Applies `ops` to `text`, changing only the bytes of the keys they name, and checks that the result parses to
/// exactly the expected tree. `where_` names the file in messages.
pub(crate) fn patch(
    text: &str,
    values: &Table,
    ops: &[Op],
    where_: &str,
) -> Result<String, ConfigError> {
    let document =
        toml_edit::Document::parse(text).map_err(|error| invalid_toml(text, where_, &error))?;
    let newline = if text.contains("\r\n") { "\r\n" } else { "\n" };
    let mut edits: Vec<TextEdit> = Vec::new();
    let mut body_inserts: BTreeMap<usize, Vec<String>> = BTreeMap::new();
    let mut appends: Vec<(Vec<String>, Vec<String>)> = Vec::new();
    let mut append_line = |table: &[String], line: String| match appends
        .iter_mut()
        .find(|(known, _)| known.as_slice() == table)
    {
        Some((_, lines)) => lines.push(line),
        None => appends.push((table.to_vec(), vec![line])),
    };

    // A later change to the same key replaces an earlier one, as in `apply_to_tree`; only the last is written.
    let last_per_key = ops
        .iter()
        .enumerate()
        .filter(|(index, op)| {
            !ops[index + 1..]
                .iter()
                .any(|later| later.path() == op.path())
        })
        .map(|(_, op)| op);
    for op in last_per_key {
        let path = op.path();
        let (key, parents) = path
            .split_last()
            .ok_or_else(|| layout(where_, path, "the key path is empty"))?;
        let planned = match op {
            Op::Set { value, .. } => vec![plan_set(text, &document, values, path, value, where_)?],
            Op::Remove { .. } | Op::Rename { .. } => {
                let found = match locate(document.as_table(), parents) {
                    Container::Table(table) => table
                        .get(key)
                        .filter(|item| !item.is_none())
                        .map(|item| (table, item)),
                    Container::Missing => None,
                    Container::Inline(_) | Container::NotATable => {
                        return Err(layout(where_, path, "it is not written as a plain key"));
                    }
                };
                match (found, op) {
                    (Some((table, item)), _) => {
                        vec![Some(plan_line_replacement(
                            text, table, item, op, newline, where_,
                        )?)]
                    }
                    // A rename of an absent key is a plain set of its replacements.
                    (None, Op::Rename { entries, .. }) => entries
                        .iter()
                        .map(|(name, value)| {
                            let mut target = parents.to_vec();
                            target.push(name.clone());
                            plan_set(text, &document, values, &target, value, where_)
                        })
                        .collect::<Result<Vec<_>, _>>()?,
                    (None, _) => Vec::new(),
                }
            }
        };
        for item in planned.into_iter().flatten() {
            match item {
                Planned::Edit(edit) => edits.push(edit),
                Planned::Body(at, line) => body_inserts.entry(at).or_default().push(line),
                Planned::Append(table, line) => append_line(&table, line),
            }
        }
    }

    for (at, lines) in body_inserts {
        let text_here = if at == text.len() && !text.is_empty() && !text.ends_with('\n') {
            let mut out = String::new();
            for line in &lines {
                out.push_str(newline);
                out.push_str(line);
            }
            out
        } else {
            let mut out = String::new();
            for line in &lines {
                out.push_str(line);
                out.push_str(newline);
            }
            out
        };
        edits.push(TextEdit::replace(at, at, text_here));
    }

    let mut out = apply_edits(text, edits)
        .ok_or_else(|| layout(where_, &[], "two changes touch the same part of the file"))?;
    append_tables(&mut out, appends, newline);

    let expected = apply_to_tree(values, ops).map_err(|reason| layout(where_, &[], &reason))?;
    let parsed = parse_values(&out, where_).map_err(|_| {
        layout(
            where_,
            &[],
            "the change would not be valid TOML in this file's layout",
        )
    })?;
    if parsed != expected {
        return Err(layout(
            where_,
            &[],
            "the result would change more than the requested keys",
        ));
    }
    Ok(out)
}

enum Planned {
    Edit(TextEdit),
    Body(usize, String),
    Append(Vec<String>, String),
}

/// Plans one value change: an in-place replacement, a new line in its header table, or a line in a new
/// appended table. `None` when the value is already there.
fn plan_set(
    text: &str,
    document: &toml_edit::Document<&str>,
    values: &Table,
    path: &[String],
    value: &Value,
    where_: &str,
) -> Result<Option<Planned>, ConfigError> {
    if current(values, path) == Some(value) {
        return Ok(None);
    }
    let (key, parents) = path
        .split_last()
        .ok_or_else(|| layout(where_, path, "the key path is empty"))?;
    let replace = |span: Option<std::ops::Range<usize>>| {
        let span = span.ok_or_else(|| layout(where_, path, "its position is unknown"))?;
        Ok(Some(Planned::Edit(TextEdit::replace(
            span.start,
            span.end,
            value.to_toml(),
        ))))
    };
    let line = format!("{} = {}", encode_key(key), value.to_toml());
    match locate(document.as_table(), parents) {
        Container::Table(table) => match table.get(key) {
            Some(toml_edit::Item::Value(existing)) => replace(existing.span()),
            Some(toml_edit::Item::None) | None => match table_style(table) {
                TableStyle::Header => {
                    let at = body_insert_position(text, table)
                        .ok_or_else(|| layout(where_, path, "its table has no position"))?;
                    Ok(Some(Planned::Body(at, line)))
                }
                TableStyle::Implicit => Ok(Some(Planned::Append(parents.to_vec(), line))),
                TableStyle::Dotted => Err(layout(
                    where_,
                    path,
                    "its table is written with dotted keys",
                )),
                TableStyle::Root => Err(layout(where_, path, "top-level keys are not written")),
            },
            Some(_) => Err(layout(where_, path, "it is a table, not a value")),
        },
        Container::Inline(table) => match table.get(key) {
            Some(existing) => replace(existing.span()),
            None => Err(layout(where_, path, "its table is an inline table")),
        },
        Container::Missing => Ok(Some(Planned::Append(parents.to_vec(), line))),
        Container::NotATable => Err(layout(where_, path, "a part of the path is not a table")),
    }
}

/// Plans the removal of a key's whole line, or its replacement by the lines of a rename.
fn plan_line_replacement(
    text: &str,
    table: &toml_edit::Table,
    item: &toml_edit::Item,
    op: &Op,
    newline: &str,
    where_: &str,
) -> Result<Planned, ConfigError> {
    let path = op.path();
    let key = path.last().expect("non-empty");
    let toml_edit::Item::Value(existing) = item else {
        return Err(layout(where_, path, "it is a table, not a value"));
    };
    if table_style(table) != TableStyle::Header {
        return Err(layout(
            where_,
            path,
            "it is not written as a plain key under a table header",
        ));
    }
    let key_span = table
        .key(key)
        .and_then(toml_edit::Key::span)
        .ok_or_else(|| layout(where_, path, "its position is unknown"))?;
    let value_span = existing
        .span()
        .ok_or_else(|| layout(where_, path, "its position is unknown"))?;
    let start = line_start(text, key_span.start);
    let end = line_end(text, value_span.end);
    let replacement: Vec<String> = match op {
        Op::Rename { entries, .. } => entries
            .iter()
            .map(|(name, value)| format!("{} = {}", encode_key(name), value.to_toml()))
            .collect(),
        _ => Vec::new(),
    };
    let edit = if replacement.is_empty() {
        if end == text.len() && !text.ends_with('\n') && start > 0 {
            // The last line has no final newline: remove the newline before it instead, keeping the file
            // without one.
            TextEdit::replace(start - 1, end, String::new())
        } else {
            TextEdit::replace(start, end, String::new())
        }
    } else {
        let mut joined = replacement.join(newline);
        if text[..end].ends_with('\n') {
            joined.push_str(newline);
        }
        TextEdit::replace(start, end, joined)
    };
    Ok(Planned::Edit(edit))
}

fn current<'a>(values: &'a Table, path: &[String]) -> Option<&'a Value> {
    let parts: Vec<&str> = path.iter().map(String::as_str).collect();
    values.get_path(&parts)
}

fn layout(where_: &str, path: &[String], reason: &str) -> ConfigError {
    let subject = if path.is_empty() {
        String::from("the change")
    } else {
        dotted(path)
    };
    ConfigError::new(
        codes::LAYOUT_UNSUPPORTED,
        format!(
            "{where_}: {subject} cannot be saved without rewriting other parts of the file ({reason}); change it \
             by hand"
        ),
    )
}

enum Container<'a> {
    Table(&'a toml_edit::Table),
    Inline(&'a toml_edit::InlineTable),
    Missing,
    NotATable,
}

fn locate<'a>(root: &'a toml_edit::Table, path: &[String]) -> Container<'a> {
    let mut container = Container::Table(root);
    for part in path {
        container = match container {
            Container::Table(table) => match table.get(part) {
                None | Some(toml_edit::Item::None) => return Container::Missing,
                Some(toml_edit::Item::Table(inner)) => Container::Table(inner),
                Some(toml_edit::Item::Value(toml_edit::Value::InlineTable(inner))) => {
                    Container::Inline(inner)
                }
                Some(_) => return Container::NotATable,
            },
            Container::Inline(table) => match table.get(part) {
                None => return Container::Missing,
                Some(toml_edit::Value::InlineTable(inner)) => Container::Inline(inner),
                Some(_) => return Container::NotATable,
            },
            other => return other,
        };
    }
    container
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TableStyle {
    /// The document root.
    Root,
    /// A `[header]` table.
    Header,
    /// Created only by a deeper header, such as `[a]` for `[a.b]`.
    Implicit,
    /// Created by dotted keys.
    Dotted,
}

fn table_style(table: &toml_edit::Table) -> TableStyle {
    if table.is_dotted() {
        TableStyle::Dotted
    } else if table.is_implicit() {
        TableStyle::Implicit
    } else {
        match table.span() {
            Some(span) if span.end > span.start => TableStyle::Header,
            _ => TableStyle::Root,
        }
    }
}

/// Where a new key line goes in a header table: after the line of its last value (dotted keys included), or
/// right after the header when the table has no values.
fn body_insert_position(text: &str, table: &toml_edit::Table) -> Option<usize> {
    fn last_value_end(table: &toml_edit::Table) -> Option<usize> {
        let mut end = None;
        for (_, item) in table.iter() {
            let here = match item {
                toml_edit::Item::Value(value) => value.span().map(|span| span.end),
                toml_edit::Item::Table(inner) if inner.is_dotted() => last_value_end(inner),
                _ => None,
            };
            end = end.max(here);
        }
        end
    }
    let end = last_value_end(table).or_else(|| table.span().map(|span| span.end))?;
    Some(line_end(text, end))
}

fn line_start(text: &str, at: usize) -> usize {
    text[..at].rfind('\n').map_or(0, |index| index + 1)
}

fn line_end(text: &str, at: usize) -> usize {
    text[at..]
        .find('\n')
        .map_or(text.len(), |index| at + index + 1)
}

#[derive(Debug, Clone)]
struct TextEdit {
    start: usize,
    end: usize,
    text: String,
}

impl TextEdit {
    fn replace(start: usize, end: usize, text: String) -> Self {
        TextEdit { start, end, text }
    }
}

/// Applies non-overlapping edits; `None` when two overlap.
fn apply_edits(text: &str, mut edits: Vec<TextEdit>) -> Option<String> {
    edits.sort_by_key(|edit| (edit.start, edit.end));
    for pair in edits.windows(2) {
        if pair[0].end > pair[1].start {
            return None;
        }
    }
    let mut out = text.to_string();
    for edit in edits.iter().rev() {
        out.replace_range(edit.start..edit.end, &edit.text);
    }
    Some(out)
}

/// Appends new `[table]` blocks at the end, each after a blank line. A file without a final newline keeps
/// having none.
fn append_tables(text: &mut String, mut blocks: Vec<(Vec<String>, Vec<String>)>, newline: &str) {
    if blocks.is_empty() {
        return;
    }
    // Parents before children, keeping the first appearance order of each top-level table.
    let mut roots: Vec<String> = Vec::new();
    for (path, _) in &blocks {
        if let Some(first) = path.first()
            && !roots.contains(first)
        {
            roots.push(first.clone());
        }
    }
    blocks.sort_by(|(a, _), (b, _)| {
        let rank = |path: &Vec<String>| {
            path.first()
                .and_then(|first| roots.iter().position(|root| root == first))
        };
        rank(a).cmp(&rank(b)).then_with(|| a.cmp(b))
    });
    let keep_final_newline = text.is_empty() || text.ends_with('\n');
    let mut out = String::new();
    if !text.is_empty() {
        if !text.ends_with('\n') {
            out.push_str(newline);
        }
        let blank = format!("{newline}{newline}");
        if !(text.ends_with(&blank) || text.ends_with("\n\n")) {
            out.push_str(newline);
        }
    }
    for (index, (path, lines)) in blocks.iter().enumerate() {
        if index > 0 {
            out.push_str(newline);
        }
        out.push('[');
        out.push_str(
            &path
                .iter()
                .map(|part| encode_key(part))
                .collect::<Vec<_>>()
                .join("."),
        );
        out.push(']');
        out.push_str(newline);
        for line in lines {
            out.push_str(line);
            out.push_str(newline);
        }
    }
    if !keep_final_newline {
        out.truncate(out.len() - newline.len());
    }
    text.push_str(&out);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn values(text: &str) -> Table {
        parse_values(text, "t").unwrap()
    }

    fn set(path: &[&str], value: Value) -> Op {
        Op::Set {
            path: path.iter().map(|part| part.to_string()).collect(),
            value,
        }
    }

    fn run(text: &str, ops: &[Op]) -> Result<String, ConfigError> {
        patch(text, &values(text), ops, "t")
    }

    #[test]
    fn replacing_a_value_touches_only_its_bytes() {
        let text = "# head\n[stages] # trailing\nplan = \"claude\"   # why\nreview = 'codex'\n";
        let out = run(text, &[set(&["stages", "plan"], Value::from("codex"))]).unwrap();
        assert_eq!(
            out,
            "# head\n[stages] # trailing\nplan = \"codex\"   # why\nreview = 'codex'\n"
        );
    }

    #[test]
    fn a_new_key_goes_after_the_last_value_of_its_table() {
        let text =
            "[stages]\nplan = \"claude\"\n\n# about models\n[models]\nprofile = \"balanced\"\n";
        let out = run(text, &[set(&["stages", "fix"], Value::from("codex"))]).unwrap();
        assert_eq!(
            out,
            "[stages]\nplan = \"claude\"\nfix = \"codex\"\n\n# about models\n[models]\nprofile = \"balanced\"\n"
        );
    }

    #[test]
    fn a_new_table_is_appended_after_a_blank_line_with_crlf_kept() {
        let text = "[models]\r\nprofile = \"balanced\"\r\n";
        let out = run(text, &[set(&["stages", "plan"], Value::from("codex"))]).unwrap();
        assert_eq!(
            out,
            "[models]\r\nprofile = \"balanced\"\r\n\r\n[stages]\r\nplan = \"codex\"\r\n"
        );
    }

    #[test]
    fn a_file_without_a_final_newline_keeps_having_none() {
        let text = "[stages]\nplan = \"claude\"";
        let out = run(
            text,
            &[
                set(&["stages", "fix"], Value::from("codex")),
                set(&["models", "profile"], Value::from("efficient")),
            ],
        )
        .unwrap();
        assert_eq!(
            out,
            "[stages]\nplan = \"claude\"\nfix = \"codex\"\n\n[models]\nprofile = \"efficient\""
        );
    }

    #[test]
    fn setting_the_value_already_there_changes_nothing() {
        let text = "[stages]\nplan = 'claude'\n";
        assert_eq!(
            run(text, &[set(&["stages", "plan"], Value::from("claude"))]).unwrap(),
            text
        );
    }

    #[test]
    fn a_rename_writes_the_replacements_where_the_key_stood() {
        let text = "[stages]\ninitial = \"codex\" # old\nreview = \"claude\"\n";
        let op = Op::Rename {
            path: vec!["stages".into(), "initial".into()],
            entries: vec![
                ("plan".into(), Value::from("codex")),
                ("build".into(), Value::from("codex")),
            ],
        };
        let out = run(text, &[op]).unwrap();
        assert_eq!(
            out,
            "[stages]\nplan = \"codex\"\nbuild = \"codex\"\nreview = \"claude\"\n"
        );
    }

    #[test]
    fn removing_the_last_line_without_a_newline_keeps_no_final_newline() {
        let text = "[stages]\nreview = \"codex\"\ncorrection = \"codex\"";
        let op = Op::Remove {
            path: vec!["stages".into(), "correction".into()],
        };
        assert_eq!(run(text, &[op]).unwrap(), "[stages]\nreview = \"codex\"");
    }

    #[test]
    fn a_key_in_a_dotted_table_is_refused_rather_than_rewritten() {
        let text = "[agents]\nclaude.command = [\"claude\"]\n";
        let error = run(
            text,
            &[set(
                &["agents", "claude", "quality_order"],
                Value::Array(vec![]),
            )],
        )
        .unwrap_err();
        assert_eq!(error.code(), codes::LAYOUT_UNSUPPORTED);
    }

    #[test]
    fn the_last_change_to_a_key_wins() {
        let text = "[stages]\nplan = \"claude\"\n";
        let ops = [
            set(&["stages", "plan"], Value::from("codex")),
            set(&["stages", "fix"], Value::from("codex")),
            set(&["stages", "plan"], Value::from("gemini")),
            set(&["stages", "fix"], Value::from("claude")),
        ];
        assert_eq!(
            run(text, &ops).unwrap(),
            "[stages]\nplan = \"gemini\"\nfix = \"claude\"\n"
        );
    }

    #[test]
    fn an_inline_table_value_can_be_replaced_in_place() {
        let text = "[agents.claude.tiers]\nhigh = { model = \"\", effort = \"high\" }\n";
        let out = run(
            text,
            &[set(
                &["agents", "claude", "tiers", "high", "effort"],
                Value::from("max"),
            )],
        )
        .unwrap();
        assert_eq!(
            out,
            "[agents.claude.tiers]\nhigh = { model = \"\", effort = \"max\" }\n"
        );
    }
}
