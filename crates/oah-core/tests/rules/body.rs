// 1.0 본문 시험: 실제 config 해석 결과로 섹션 표·독립 규칙·패킷 문장·스킬을 그리고, 다섯째 섹션과 크기 한도를 본다.
use crate::config::{ConfigFile, Effective, ProjectConfig};
use crate::rules::body::{AlwaysSkill, SkillsForBlock, render_bodies, render_body};
use crate::rules::{Lang, RulesErrorKind as Kind};
use crate::support::plan_dir;
use std::path::Path;

/// The real config reader over TOML text (`None`: no file, the built-in defaults).
fn effective(toml: Option<&str>) -> Effective {
    let file = ConfigFile::from_bytes(
        Path::new(".oah/config.toml"),
        toml.map(|t| t.as_bytes().to_vec()),
    )
    .expect("the config parses");
    ProjectConfig::from_file(file)
        .expect("the config is valid")
        .effective()
        .clone()
}

fn body(toml: Option<&str>, lang: Lang) -> String {
    let effective = effective(toml);
    render_body(
        &effective,
        &SkillsForBlock::from_config(&effective, Vec::new()),
        lang,
    )
}

const DEFAULT_EN: &str = "## Team workflow (managed by OAH)\n\n| Section | Agent |\n|---|---|\n\
| Plan & Clarify | Claude Code |\n| Write Code | Claude Code |\n| Code Review | Codex |\n| Fix Issues | Codex |\n\n\
- Do the section your agent is listed for. If asked to do another section, say which agent is listed for it.\n\
- Code Review is done by a different agent than Write Code.\n";

#[test]
fn the_default_config_renders_the_design_body_without_packet_bullet_or_skills() {
    assert_eq!(body(None, Lang::En), DEFAULT_EN);
}

#[test]
fn the_korean_body_renders_the_same_rows_and_bullets_in_korean() {
    let ko = body(Some("[stages]\nreview = \"gemini\"\n"), Lang::Ko);
    assert_eq!(
        ko,
        "## 팀 작업 흐름 (OAH 관리)\n\n| 단계 | 에이전트 |\n|---|---|\n\
| Plan & Clarify | Claude Code |\n| Write Code | Claude Code |\n| Code Review | Gemini CLI |\n| Fix Issues | Codex |\n\n\
- 자기 에이전트가 맡은 단계만 한다. 다른 단계를 요청받으면 그 단계를 맡은 에이전트를 알려 준다.\n\
- Code Review 단계는 Write Code 단계와 다른 에이전트가 맡는다.\n"
    );
}

#[test]
fn a_fifth_section_in_order_renders_a_fifth_row_through_the_real_plan() {
    let toml = "[sections]\norder = [\"plan\", \"build\", \"review\", \"fix\", \"docs\"]\n[stages]\ndocs = \"gemini\"\n";
    let effective = effective(Some(toml));
    let bodies = render_bodies(
        &effective,
        &SkillsForBlock::from_config(&effective, Vec::new()),
    );
    let rows: Vec<&str> = bodies
        .en
        .lines()
        .filter(|line| line.starts_with("| ") && !line.starts_with("| Section"))
        .collect();
    assert_eq!(rows.len(), 5);
    assert_eq!(rows[4], "| docs | Gemini CLI |");
    let dir = tempfile::tempdir().expect("a project");
    let plan = plan_dir(dir.path(), None, &bodies).expect("a plan");
    assert!(
        std::str::from_utf8(&plan.agents_after)
            .expect("UTF-8")
            .contains("\n| docs | Gemini CLI |\n")
    );
}

#[test]
fn plan_skills_add_the_packet_bullet_and_a_skills_line_per_section_in_order() {
    let toml = "[sections.plan]\nskills = [\"packet-writer\", \"gates\"]\n[sections.fix]\nskills = [\"debugging\"]\n";
    let text = body(Some(toml), Lang::En);
    let tail = text
        .split_once("- Code Review is done by a different agent than Write Code.\n")
        .expect("bullet")
        .1;
    assert_eq!(
        tail,
        "- Plan & Clarify ends with a written work packet. Use the skills listed for Plan & Clarify.\n\n\
### Skills\n- Plan & Clarify: `packet-writer`, `gates`\n- Fix Issues: `debugging`\n"
    );
}

#[test]
fn always_on_skills_render_one_line_each_with_a_one_line_description() {
    let effective = effective(None);
    let always = vec![AlwaysSkill {
        name: "tdd".to_string(),
        description: "Write the test first.\nThen the code.".to_string(),
        dir: ".claude/skills/tdd".to_string(),
    }];
    let text = render_body(
        &effective,
        &SkillsForBlock::from_config(&effective, always),
        Lang::En,
    );
    assert!(text.ends_with("\n\n### Skills\n- Always: `tdd`: Write the test first. Then the code. (`.claude/skills/tdd/SKILL.md`)\n"), "{text}");
}

#[test]
fn independence_bullets_follow_each_pair_and_skip_unlisted_ids() {
    let toml = "[sections.fix]\nindependent_of = [\"review\", \"docs\"]\n";
    let text = body(Some(toml), Lang::En);
    let bullets: Vec<&str> = text
        .lines()
        .filter(|line| line.contains("different agent"))
        .collect();
    assert_eq!(
        bullets,
        [
            "- Code Review is done by a different agent than Write Code.",
            "- Fix Issues is done by a different agent than Code Review.",
        ]
    );
}

#[test]
fn only_the_plan_id_gets_the_packet_bullet_and_unassigned_sections_say_so() {
    let toml = "[sections]\norder = [\"research\", \"code\"]\n[sections.research]\nskills = [\"notes\"]\n[stages]\ncode = \"claude\"\n";
    let text = body(Some(toml), Lang::En);
    assert!(
        text.contains("| research | Not assigned |\n| code | Claude Code |\n"),
        "{text}"
    );
    assert!(!text.contains("work packet"));
    assert!(text.ends_with("### Skills\n- research: `notes`\n"));
}

#[test]
fn table_cells_stay_on_one_line_and_escape_the_column_bar() {
    let toml = "[sections.plan]\nlabel = \"Plan | Spec\\nNotes\"\n";
    assert!(body(Some(toml), Lang::En).contains("\n| Plan \\| Spec Notes | Claude Code |\n"));
}

#[test]
fn the_size_limit_applies_to_the_rendered_block() {
    let ids: Vec<String> = (0..60).map(|n| format!("s{n}")).collect();
    let mut toml = format!(
        "[sections]\norder = [{}]\n",
        ids.iter()
            .map(|id| format!("\"{id}\""))
            .collect::<Vec<_>>()
            .join(", ")
    );
    for id in &ids {
        toml.push_str(&format!(
            "[sections.{id}]\nlabel = \"{}\"\n",
            "L".repeat(100)
        ));
    }
    let effective = effective(Some(&toml));
    let bodies = render_bodies(
        &effective,
        &SkillsForBlock::from_config(&effective, Vec::new()),
    );
    let dir = tempfile::tempdir().expect("a project");
    assert_eq!(
        plan_dir(dir.path(), None, &bodies)
            .expect_err("too large")
            .kind,
        Kind::BlockTooLarge
    );
}

#[test]
fn the_korean_packet_bullet_and_skills_heading_are_korean() {
    let effective = effective(Some("[sections.plan]\nskills = [\"packet-writer\"]\n"));
    let always = vec![AlwaysSkill {
        name: "tdd".to_string(),
        description: "Test first.".to_string(),
        dir: "s/tdd".to_string(),
    }];
    let text = render_body(
        &effective,
        &SkillsForBlock::from_config(&effective, always),
        Lang::Ko,
    );
    assert!(text.ends_with(
        "- Plan & Clarify 단계는 작성된 작업 패킷으로 끝난다. Plan & Clarify 단계에 등록된 스킬을 쓴다.\n\n\
### 스킬\n- 항상: `tdd`: Test first. (`s/tdd/SKILL.md`)\n- Plan & Clarify: `packet-writer`\n"
    ), "{text}");
}
