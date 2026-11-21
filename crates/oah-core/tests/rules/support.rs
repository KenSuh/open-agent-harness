// 규칙 시험 공용 도구: 임시 프로젝트 폴더를 만들고 실제 읽기 함수로 입력을 읽어 계획을 세운다.
use crate::rules::RulesError;
use crate::rules::{BlockBodies, RulesFiles, RulesInput, RulesPlan, plan};
use std::fs;
use std::path::Path;

/// The synthetic fixture body used where the body itself is not under test.
pub fn fixture_bodies() -> BlockBodies {
    BlockBodies {
        en: "## Fixture workflow\n\n1. Read the fixture rules first.\n".to_string(),
        ko: "## 고정 규칙\n\n1. 고정 규칙을 먼저 읽는다.\n".to_string(),
    }
}

/// Writes `files` (relative path, bytes) under `root`, creating folders.
pub fn write_files(root: &Path, files: &[(&str, &[u8])]) {
    for (relative, bytes) in files {
        let path = root.join(relative);
        fs::create_dir_all(path.parent().expect("a parent")).expect("create folders");
        fs::write(&path, bytes).expect("write a fixture file");
    }
}

/// Plans with `AGENTS.md` given as bytes (or absent) and nothing else in the project.
pub fn plan_agents(agents: Option<&[u8]>, lang: Option<&str>) -> Result<RulesPlan, RulesError> {
    let dir = tempfile::tempdir().expect("a temporary folder");
    if let Some(bytes) = agents {
        write_files(dir.path(), &[("AGENTS.md", bytes)]);
    }
    plan_dir(dir.path(), lang, &fixture_bodies())
}

/// Reads the project with the production reader, then plans.
pub fn plan_dir(
    root: &Path,
    lang: Option<&str>,
    bodies: &BlockBodies,
) -> Result<RulesPlan, RulesError> {
    let files = RulesFiles::read(root).expect("the project reads");
    plan(&RulesInput {
        files: &files,
        lang,
        bodies,
    })
}
