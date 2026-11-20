// 사실 파일 거절 시험: 잘못된 항목·열린 항목 참조·모르는 표를 그 표 이름과 함께 거절하는지 본다.
use super::{DEFAULT, entry, parsed, refused, text_with};
use crate::agents::capability::CapabilityName;
use crate::agents::facts::{FactStatus, Facts};
use crate::config::AgentId;

#[test]
fn malformed_entries_are_refused_with_their_table() {
    let cases: [(&str, String, &str); 9] = [
        (
            "codex.pack_update",
            String::from("status = \"unknown\"\n"),
            "[codex.pack_update]",
        ),
        ("claude.detect", format!("{DEFAULT}extra = 1\n"), "extra"),
        (
            "claude.detect",
            DEFAULT.replace("\"unknown\"", "\"maybe\""),
            "status",
        ),
        (
            "claude.detect",
            DEFAULT.replace("\"docs\"", "\"rumour\""),
            "basis",
        ),
        (
            "claude.detect",
            entry("verified", "docs", "[\"macos\", \"macos\"]", ""),
            "os",
        ),
        ("claude.detect", entry("verified", "docs", "[]", ""), "os"),
        (
            "claude.detect",
            entry("verified", "measured", "[\"macos\"]", ""),
            "verified_on",
        ),
        (
            "claude.detect",
            entry("verified", "carried", "[\"macos\"]", "1.0.0"),
            "verified_on",
        ),
        (
            "claude.detect",
            DEFAULT.replace("\"U-1\"", "\"U-99\""),
            "U-99",
        ),
    ];
    for (table, body, needle) in cases {
        let error = refused(&[(table, &body)], "U-1 = \"test item\"");
        assert!(error.contains(needle), "{table} {needle}: {error}");
    }
    let verified_with_open =
        entry("verified", "docs", "[\"macos\"]", "").replace("[]\n", "[\"U-1\"]\n");
    assert!(
        refused(&[("claude.detect", &verified_with_open)], "U-1 = \"t\"").contains("unverified")
    );
}

#[test]
fn every_open_item_must_be_referenced_and_every_table_must_be_known() {
    let error = refused(&[], "U-1 = \"test item\"\nU-2 = \"never referenced\"");
    assert!(error.contains("U-2"), "{error}");
    let mut text = text_with(&[], "U-1 = \"test item\"");
    text.push_str("[windsurf.detect]\nstatus = \"none\"\n");
    assert!(Facts::parse(&text).unwrap_err().0.contains("windsurf"));
    assert_eq!(
        parsed(&[])
            .get(AgentId::Claude, CapabilityName::Detect)
            .unwrap()
            .status,
        FactStatus::Unknown
    );
}

#[test]
fn a_missing_entry_is_refused_with_its_table() {
    let full = text_with(&[], "U-1 = \"test item\"");
    let table = format!("[cursor.pack_update]\n{DEFAULT}\n");
    assert!(full.contains(&table));
    let error = Facts::parse(&full.replace(&table, "")).unwrap_err().0;
    assert_eq!(error, "facts.toml [cursor.pack_update] is missing");
}
