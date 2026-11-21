// 얇은 파일 시험: C12 다섯 줄 자격표, GEMINI.md 기존 파일 예외, .mdc 줄끝, 링크 거절, 에이전트 홈 쓰기 없음과 고정 경로를 본다.
use crate::config::AgentId;
use crate::rules::thin::{CURSOR_MDC, GEMINI_MD, MDC_TEXT};
use crate::rules::{
    ProjectFile, RulesErrorKind as Kind, ThinFileTrigger as Trigger, ThinInput, plan_thin,
};
use crate::support::write_files;

fn planned(
    detected: &[AgentId],
    gemini: &ProjectFile,
    mdc: &ProjectFile,
) -> Vec<(&'static str, Trigger)> {
    let input = ThinInput {
        detected,
        gemini_md: gemini,
        cursor_mdc: mdc,
    };
    plan_thin(&input)
        .expect("a plan")
        .into_iter()
        .map(|p| (p.path, p.trigger))
        .collect()
}

#[test]
fn the_c12_eligibility_table_holds_row_by_row() {
    let present = ProjectFile::Regular(b"# Gemini notes\n".to_vec());
    let missing = ProjectFile::Missing;
    let mdc = ProjectFile::Regular(MDC_TEXT.as_bytes().to_vec());
    // Gemini absent, GEMINI.md present -> planned (existing-file exception).
    assert_eq!(
        planned(&[], &present, &missing),
        [(GEMINI_MD, Trigger::FileExists)]
    );
    // Gemini absent, file missing -> not planned.
    assert_eq!(planned(&[AgentId::Claude], &missing, &missing), []);
    // Gemini present, file missing -> planned.
    assert_eq!(
        planned(&[AgentId::Gemini], &missing, &missing),
        [(GEMINI_MD, Trigger::AgentDetected)]
    );
    // Cursor absent with an .mdc present -> untouched.
    assert_eq!(planned(&[AgentId::Codex], &missing, &mdc), []);
    // Cursor present -> planned.
    assert_eq!(
        planned(&[AgentId::Cursor], &missing, &missing),
        [(CURSOR_MDC, Trigger::AgentDetected)]
    );
}

#[test]
fn an_existing_gemini_file_gets_exactly_one_import_line_in_its_own_line_ending() {
    let input = ProjectFile::Regular(b"Notes\r\nmore".to_vec());
    let plans = plan_thin(&ThinInput {
        detected: &[],
        gemini_md: &input,
        cursor_mdc: &ProjectFile::Missing,
    })
    .expect("plan");
    assert_eq!(plans[0].after, b"Notes\r\nmore\r\n@AGENTS.md\r\n");
    assert!(plans[0].needs_write());
    let done = ProjectFile::Regular(plans[0].after.clone());
    let again = plan_thin(&ThinInput {
        detected: &[],
        gemini_md: &done,
        cursor_mdc: &ProjectFile::Missing,
    })
    .expect("plan");
    assert!(!again[0].needs_write(), "a second plan changes nothing");
    let twice = ProjectFile::Regular(b"@AGENTS.md\n@AGENTS.md\n".to_vec());
    let refused = plan_thin(&ThinInput {
        detected: &[AgentId::Gemini],
        gemini_md: &twice,
        cursor_mdc: &ProjectFile::Missing,
    });
    assert_eq!(refused.expect_err("duplicate").kind, Kind::MarkerDuplicate);
}

#[test]
fn the_mdc_file_is_the_fixed_rule_in_the_existing_line_ending() {
    let crlf = ProjectFile::Regular(b"old\r\n".to_vec());
    let plans = plan_thin(&ThinInput {
        detected: &[AgentId::Cursor],
        gemini_md: &ProjectFile::Missing,
        cursor_mdc: &crlf,
    })
    .expect("plan");
    assert_eq!(plans[0].after, MDC_TEXT.replace('\n', "\r\n").as_bytes());
    assert!(MDC_TEXT.starts_with("---\ndescription: OAH team workflow\nalwaysApply: true\n---\n"));
}

#[test]
fn a_linked_thin_file_or_linked_folder_is_refused_and_unlinked_absence_is_fine() {
    let link = ProjectFile::Link {
        target: "/elsewhere/GEMINI.md".into(),
    };
    let gemini = plan_thin(&ThinInput {
        detected: &[],
        gemini_md: &link,
        cursor_mdc: &ProjectFile::Missing,
    });
    assert_eq!(
        gemini
            .expect_err("a link counts as present and is refused")
            .kind,
        Kind::Link
    );
    let mdc = plan_thin(&ThinInput {
        detected: &[AgentId::Cursor],
        gemini_md: &ProjectFile::Missing,
        cursor_mdc: &link,
    });
    assert_eq!(mdc.expect_err("refused").kind, Kind::Link);
    assert_eq!(
        planned(&[], &ProjectFile::Missing, &link),
        [],
        "Cursor absent: a linked .mdc is left alone"
    );
}

#[cfg(unix)]
#[test]
fn the_reader_reports_a_linked_parent_folder_as_a_link() {
    let outside = tempfile::tempdir().expect("an outside folder");
    write_files(outside.path(), &[("rules/oah.mdc", b"global\n")]);
    let dir = tempfile::tempdir().expect("a project");
    std::os::unix::fs::symlink(outside.path(), dir.path().join(".cursor")).expect("a folder link");
    let file = ProjectFile::read(dir.path(), CURSOR_MDC).expect("reads");
    assert!(matches!(file, ProjectFile::Link { .. }), "{file:?}");
    assert_eq!(
        ProjectFile::read(dir.path(), GEMINI_MD).expect("reads"),
        ProjectFile::Missing
    );
}

#[test]
fn every_planned_path_is_a_fixed_project_relative_path_and_planning_writes_nothing() {
    let project = tempfile::tempdir().expect("a project");
    let all = [AgentId::Claude, AgentId::Gemini, AgentId::Cursor];
    let gemini = ProjectFile::read(project.path(), GEMINI_MD).expect("reads");
    let mdc = ProjectFile::read(project.path(), CURSOR_MDC).expect("reads");
    let plans = plan_thin(&ThinInput {
        detected: &all,
        gemini_md: &gemini,
        cursor_mdc: &mdc,
    })
    .expect("plan");
    for plan in &plans {
        assert!([GEMINI_MD, CURSOR_MDC].contains(&plan.path));
        assert!(
            !plan.path.starts_with(['/', '~', '.']) || plan.path.starts_with(".cursor/"),
            "{}",
            plan.path
        );
        assert!(!plan.path.split('/').any(|part| part == ".."));
    }
    assert_eq!(
        std::fs::read_dir(project.path()).expect("list").count(),
        0,
        "planning writes nothing"
    );
}
