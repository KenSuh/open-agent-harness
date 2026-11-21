// 관리 블록 시험: 블록 밖 바이트·BOM·줄끝·마지막 줄바꿈 보존, 재계획 고정점, 형식 오류의 타입 거절, 실제 링크 거절을 본다.
use crate::rules::{BlockBodies, Drift, RulesErrorKind as Kind, RulesFiles, RulesInput, check};
use crate::support::{fixture_bodies, plan_agents, plan_dir, write_files};

const BOM: &str = "\u{feff}";

fn text(bytes: &[u8]) -> &str {
    std::str::from_utf8(bytes).expect("UTF-8")
}

#[test]
fn replacing_a_crlf_block_keeps_bom_outside_text_and_missing_final_newline() {
    let head = format!("{BOM}# Notes\r\n\r\nHand-written intro.\r\n");
    let tail = "\r\nUser text below.\r\n  trailing spaces  ";
    let before =
        format!("{head}<!-- OAH:RULES:BEGIN v1 -->\r\nold rules\r\n<!-- OAH:RULES:END -->{tail}");
    let plan = plan_agents(Some(before.as_bytes()), None).expect("a plan");
    let after = text(&plan.agents_after);
    assert!(
        after.starts_with(&format!("{head}<!-- OAH:RULES:BEGIN v1 -->\r\n")),
        "{after:?}"
    );
    assert!(
        after.ends_with(&format!("<!-- OAH:RULES:END -->{tail}")),
        "{after:?}"
    );
    assert!(!after.contains("old rules"));
    let bare_lf = after
        .match_indices('\n')
        .filter(|(at, _)| !after[..*at].ends_with('\r'))
        .count();
    assert_eq!(bare_lf, 0, "every line of a CRLF block keeps CRLF");
}

#[test]
fn appending_to_a_bom_file_without_final_newline_adds_one_line_end_and_one_blank_line() {
    let plan = plan_agents(Some(format!("{BOM}Project notes").as_bytes()), None).expect("a plan");
    let after = text(&plan.agents_after);
    assert!(
        after.starts_with(&format!(
            "{BOM}Project notes\n\n<!-- OAH:RULES:BEGIN v1 -->\n"
        )),
        "{after:?}"
    );
    assert!(after.ends_with("<!-- OAH:RULES:END -->\n"));
}

#[test]
fn a_planned_file_is_a_fixed_point_of_the_next_plan() {
    let first = plan_agents(Some(b"Intro\r\n\r\nMore text\r\n"), Some("ko")).expect("a plan");
    let dir = tempfile::tempdir().expect("a temporary folder");
    write_files(
        dir.path(),
        &[
            ("AGENTS.md", &first.agents_after),
            ("CLAUDE.md", &first.claude_after),
        ],
    );
    let second = plan_dir(dir.path(), None, &fixture_bodies()).expect("a plan");
    assert_eq!(
        second.agents_after, first.agents_after,
        "the detected language is kept"
    );
    assert!(second.writes().is_empty());
    let files = RulesFiles::read(dir.path()).expect("reads");
    let input = RulesInput {
        files: &files,
        lang: None,
        bodies: &fixture_bodies(),
    };
    assert_eq!(check(&input).expect("no refusal").1, Vec::<Drift>::new());
}

#[test]
fn a_hand_edited_block_and_missing_files_are_drift_and_nothing_is_written() {
    let dir = tempfile::tempdir().expect("a temporary folder");
    let edited = "<!-- OAH:RULES:BEGIN v1 -->\nedited by hand\n<!-- OAH:RULES:END -->\n";
    write_files(dir.path(), &[("AGENTS.md", edited.as_bytes())]);
    let files = RulesFiles::read(dir.path()).expect("reads");
    let (plan, found) = check(&RulesInput {
        files: &files,
        lang: None,
        bodies: &fixture_bodies(),
    })
    .expect("plan");
    assert_eq!(found, [Drift::BlockDiffers, Drift::ClaudeMissing]);
    assert_eq!(
        plan.writes()
            .iter()
            .map(|(name, _)| *name)
            .collect::<Vec<_>>(),
        ["AGENTS.md", "CLAUDE.md"]
    );
    assert_eq!(
        std::fs::read(dir.path().join("AGENTS.md")).expect("still there"),
        edited.as_bytes()
    );
    assert!(
        !dir.path().join("CLAUDE.md").exists(),
        "checking writes nothing"
    );
}

#[test]
fn malformed_inputs_are_typed_refusals_with_no_plan() {
    let block = "<!-- OAH:RULES:BEGIN v1 -->\nx\n<!-- OAH:RULES:END -->\n";
    let cases: Vec<(&str, Vec<u8>, Option<&str>, Kind)> = vec![
        (
            "two blocks",
            format!("{block}{block}").into_bytes(),
            None,
            Kind::MarkerDuplicate,
        ),
        (
            "end before begin",
            b"<!-- OAH:RULES:END -->\n<!-- OAH:RULES:BEGIN v1 -->\n".to_vec(),
            None,
            Kind::MarkerBroken,
        ),
        (
            "bom hides the begin marker",
            format!("{BOM}{block}").into_bytes(),
            None,
            Kind::MarkerBroken,
        ),
        (
            "huge version",
            b"<!-- OAH:RULES:BEGIN v99999999999999999999999 -->\n<!-- OAH:RULES:END -->\n".to_vec(),
            None,
            Kind::Newer,
        ),
        ("not utf-8", b"caf\xe9\n".to_vec(), None, Kind::Encoding),
        ("over the budget", vec![b'a'; 32 * 1024], None, Kind::Budget),
        (
            "unknown language",
            b"notes\n".to_vec(),
            Some("fr"),
            Kind::Lang,
        ),
    ];
    for (name, agents, lang, kind) in cases {
        let refused = plan_agents(Some(&agents), lang).expect_err(name);
        assert_eq!(refused.kind, kind, "{name}: {refused}");
        assert!(refused.code().starts_with("OAH_RULES_"), "{name}");
    }
}

#[test]
fn a_reserved_marker_in_local_rules_and_a_duplicate_import_are_refused() {
    let dir = tempfile::tempdir().expect("a temporary folder");
    write_files(
        dir.path(),
        &[(".oah/rules.local.md", b"- ok\n<!-- OAH:RULES:END -->  \n")],
    );
    let refused = plan_dir(dir.path(), None, &fixture_bodies()).expect_err("refused");
    assert_eq!(refused.kind, Kind::MarkerBroken);
    let dir = tempfile::tempdir().expect("a temporary folder");
    write_files(
        dir.path(),
        &[("CLAUDE.md", b"@AGENTS.md\r\n  @AGENTS.md \r\n")],
    );
    assert_eq!(
        plan_dir(dir.path(), None, &fixture_bodies())
            .expect_err("refused")
            .kind,
        Kind::MarkerDuplicate
    );
}

#[test]
fn a_body_that_would_break_the_markers_is_refused_by_the_read_back_check() {
    let bodies = BlockBodies {
        en: "<!-- OAH:RULES:END -->\n".to_string(),
        ko: String::new(),
    };
    let dir = tempfile::tempdir().expect("a temporary folder");
    assert_eq!(
        plan_dir(dir.path(), None, &bodies)
            .expect_err("refused")
            .kind,
        Kind::MarkerDuplicate
    );
}

#[cfg(unix)]
#[test]
fn symbolic_links_are_refused_and_their_targets_are_not_read() {
    use std::os::unix::fs::symlink;
    for name in ["AGENTS.md", "CLAUDE.md"] {
        let outside = tempfile::tempdir().expect("an outside folder");
        let target = outside.path().join("global.md");
        std::fs::write(&target, b"global rules\n").expect("write the target");
        let dir = tempfile::tempdir().expect("a project");
        symlink(&target, dir.path().join(name)).expect("a link");
        let refused = plan_dir(dir.path(), None, &fixture_bodies()).expect_err(name);
        assert_eq!(refused.kind, Kind::Link, "{name}");
        assert!(refused.message.starts_with(name));
        assert_eq!(std::fs::read(&target).expect("target"), b"global rules\n");
    }
}
