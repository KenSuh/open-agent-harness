// 설정 읽기 시험: 기본값·BOM/CRLF·잘못된 바이트·알 수 없는 키 알림·v0.4.0 단계/엔진 해석 규칙을 확인하고, 읽기가 파일을 바꾸지 않는지 본다.
use oah_core::config::{
    AgentId, AgentSource, AssignmentSource, ConfigFile, Format, ProjectConfig, StageFormat, Table,
    UserSettings, Value, codes, notice_codes,
};
use std::fs;
use std::path::{Path, PathBuf};

struct Project {
    dir: tempfile::TempDir,
}

impl Project {
    fn empty() -> Project {
        Project {
            dir: tempfile::tempdir().unwrap(),
        }
    }

    fn with_bytes(bytes: &[u8]) -> Project {
        let project = Project::empty();
        fs::create_dir_all(project.config().parent().unwrap()).unwrap();
        fs::write(project.config(), bytes).unwrap();
        project
    }

    fn with(text: &str) -> Project {
        Project::with_bytes(text.as_bytes())
    }

    fn config(&self) -> PathBuf {
        self.dir.path().join(".oah").join("config.toml")
    }

    fn read(&self) -> ProjectConfig {
        ProjectConfig::read(&self.config()).unwrap()
    }

    fn error(&self) -> oah_core::config::ConfigError {
        ProjectConfig::read(&self.config()).unwrap_err()
    }
}

/// Every file below `root` with its bytes and modification time.
fn listing(root: &Path) -> Vec<(PathBuf, Vec<u8>, std::time::SystemTime)> {
    let mut out = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        for entry in fs::read_dir(&folder).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                pending.push(path);
            } else {
                let modified = fs::metadata(&path).unwrap().modified().unwrap();
                out.push((path.clone(), fs::read(&path).unwrap(), modified));
            }
        }
    }
    out.sort();
    out
}

fn codes_of(config: &ProjectConfig) -> Vec<&str> {
    config
        .notices()
        .iter()
        .map(|notice| notice.code())
        .collect()
}

#[test]
fn a_missing_file_reads_as_the_defaults_and_nothing_is_created() {
    let project = Project::empty();
    let config = project.read();
    assert_eq!(config.format(), Format::Missing);
    assert!(config.upgrade_warning().is_none());
    let effective = config.effective();
    let ids: Vec<&str> = effective
        .sections
        .iter()
        .map(|section| section.id.as_str())
        .collect();
    assert_eq!(ids, ["plan", "build", "review", "fix"]);
    assert_eq!(effective.agent_for("plan"), Some(AgentId::Claude));
    assert_eq!(effective.agent_for("build"), Some(AgentId::Claude));
    assert_eq!(effective.agent_for("review"), Some(AgentId::Codex));
    assert_eq!(effective.agent_for("fix"), Some(AgentId::Codex));
    assert_eq!(effective.agents.len(), 7);
    assert_eq!(effective.agent(AgentId::Codex).tiers[3].effort, "highest");
    assert_eq!(effective.models.profile, "balanced");
    assert!(config.notices().is_empty());
    assert!(
        listing(project.dir.path()).is_empty(),
        "reading created a file"
    );
    assert!(
        !project.config().parent().unwrap().exists(),
        "reading created .oah"
    );
}

#[test]
fn a_byte_order_mark_and_crlf_line_ends_are_accepted() {
    let project =
        Project::with_bytes(b"\xEF\xBB\xBF[stages]\r\nplan = \"codex\"\r\nfix = \"claude\"\r\n");
    let config = project.read();
    assert_eq!(config.format(), Format::Current);
    assert!(config.file().has_bom());
    assert_eq!(config.effective().agent_for("plan"), Some(AgentId::Codex));
    assert_eq!(config.effective().agent_for("fix"), Some(AgentId::Claude));
}

#[test]
fn bytes_that_are_not_utf8_or_not_toml_are_invalid() {
    let error = Project::with_bytes(b"[stages]\nplan = \"\xC4\xDA\"\n").error();
    assert_eq!(error.code(), codes::INVALID);
    assert!(
        error.message().contains("is not UTF-8 (byte 17)"),
        "{error}"
    );

    let error = Project::with("[stages\nplan = 1\n").error();
    assert_eq!(error.code(), codes::INVALID);
    assert!(error.message().contains("is not valid TOML"), "{error}");
}

#[test]
fn a_wrong_type_is_refused_with_the_v040_wording() {
    let error = Project::with("[models]\nttl_hours = \"soon\"\n").error();
    assert_eq!(error.code(), codes::INVALID);
    assert!(
        error
            .message()
            .ends_with("models.ttl_hours must be a number, not a string; change it to a number or remove it to use the default"),
        "{error}"
    );
    let error = Project::with("stages = \"claude\"\n").error();
    assert!(
        error
            .message()
            .contains("stages must be a table, not a string"),
        "{error}"
    );
}

#[test]
fn unknown_keys_are_notices_and_reading_continues() {
    let config = Project::with("[models]\nprofil = \"efficient\"\nfetch_scores = false\n").read();
    assert_eq!(codes_of(&config), [notice_codes::UNKNOWN_KEY]);
    assert!(
        config.notices()[0]
            .message()
            .contains("unknown key models.profil")
    );
    assert_eq!(config.notices()[0].keys(), ["models.profil"]);
    assert_eq!(config.effective().models.profile, "balanced");
    assert!(!config.effective().models.fetch_scores);
}

#[test]
fn the_three_legacy_stage_keys_map_through_the_legacy_table() {
    let project = Project::with(
        "[stages]\ninitial = \"codex\"\nreview = \"claude\"\ncorrection = \"gemini\"\n",
    );
    let config = project.read();
    assert_eq!(config.format(), Format::Legacy);
    assert!(config.notices().is_empty());
    let effective = config.effective();
    assert_eq!(effective.stage_format, StageFormat::Legacy);
    let read: Vec<(&str, AgentId, AssignmentSource)> = effective
        .stages
        .iter()
        .map(|assignment| {
            (
                assignment.section.as_str(),
                assignment.agent,
                assignment.source,
            )
        })
        .collect();
    assert_eq!(
        read,
        [
            ("plan", AgentId::Codex, AssignmentSource::Legacy("initial")),
            ("build", AgentId::Codex, AssignmentSource::Legacy("initial")),
            ("review", AgentId::Claude, AssignmentSource::File),
            (
                "fix",
                AgentId::Gemini,
                AssignmentSource::Legacy("correction")
            ),
        ]
    );
    let warning = config
        .upgrade_warning()
        .expect("a v0.4.0 file must warn before its first save");
    assert!(
        warning
            .message()
            .contains("oah 0.4 commands will stop reading")
    );
    assert!(warning.message().contains(".oah/config.toml.v0.4.bak"));
}

#[test]
fn review_alone_is_not_a_new_format_signal() {
    let config = Project::with("[stages]\nreview = \"claude\"\n").read();
    assert_eq!(config.format(), Format::Legacy);
    assert_eq!(
        config.effective().agent_for("review"),
        Some(AgentId::Claude)
    );
    assert_eq!(config.effective().agent_for("plan"), Some(AgentId::Claude));
    assert_eq!(config.effective().agent_for("fix"), Some(AgentId::Codex));
    assert!(config.upgrade_warning().is_some());
}

#[test]
fn new_stage_keys_win_and_legacy_keys_are_reported_ignored() {
    let config = Project::with(
        "[stages]\ninitial = \"codex\"\nbuild = \"gemini\"\ncorrection = \"claude\"\n",
    )
    .read();
    assert_eq!(config.format(), Format::Current);
    assert_eq!(codes_of(&config), [notice_codes::STAGES_LEGACY_IGNORED]);
    assert_eq!(
        config.notices()[0].keys(),
        ["stages.initial", "stages.correction"]
    );
    let effective = config.effective();
    assert_eq!(effective.stage_format, StageFormat::New);
    assert_eq!(effective.agent_for("build"), Some(AgentId::Gemini));
    assert_eq!(
        effective.agent_for("plan"),
        Some(AgentId::Claude),
        "unassigned built-ins keep their default"
    );
    assert_eq!(
        effective.agent_for("fix"),
        Some(AgentId::Codex),
        "correction is ignored, not read"
    );
}

#[test]
fn engines_are_read_as_agents_only_when_the_agents_table_is_absent() {
    let legacy = "[engines.claude]\ncommand = [\"claude\", \"--verbose\"]\npermission_mode = \"acceptEdits\"\n\
                  tools = [\"Read\"]\n\n[engines.claude.tiers.high]\neffort = \"max\"\ntimeout_min = 60\n";
    let config = Project::with(legacy).read();
    let claude = config.effective().agent(AgentId::Claude);
    assert_eq!(claude.source, AgentSource::Engines);
    assert_eq!(claude.command, ["claude", "--verbose"]);
    assert_eq!(claude.tiers[2].effort, "max");
    assert_eq!(
        claude.unused_kept_in_file,
        ["permission_mode", "tiers.high.timeout_min", "tools"]
    );
    assert!(
        config.notices().is_empty(),
        "kept v0.4.0 keys are not unknown keys"
    );

    let both = format!("{legacy}\n[agents.claude]\nquality_order = [\"model-a\"]\n");
    let config = Project::with(&both).read();
    let claude = config.effective().agent(AgentId::Claude);
    assert_eq!(claude.source, AgentSource::Agents);
    assert_eq!(
        claude.command,
        ["claude"],
        "[engines.claude] is not merged under [agents.claude]"
    );
    assert_eq!(claude.quality_order, ["model-a"]);
    assert!(claude.unused_kept_in_file.is_empty());
    assert_eq!(config.format(), Format::Current);
}

#[test]
fn cursor_cannot_own_a_section_and_unknown_agents_are_invalid() {
    let error = Project::with("[stages]\nreview = \"cursor\"\n").error();
    assert_eq!(error.code(), codes::AGENT_DEPLOY_ONLY);
    let error = Project::with("[stages]\nplan = \"someone\"\n").error();
    assert_eq!(error.code(), codes::INVALID);
    assert!(
        error
            .message()
            .ends_with("stages.plan = \"someone\"; use one of: claude, codex, gemini, copilot, opencode, antigravity"),
        "{error}"
    );
}

#[test]
fn custom_sections_follow_the_order_and_stay_unassigned_until_given_an_agent() {
    let text = "[sections]\norder = [\"plan\", \"build\", \"docs\", \"review\", \"fix\"]\n\n\
                [sections.docs]\nlabel = \"Write Docs\"\nkind = \"design\"\n\n\
                [sections.retired]\nlabel = \"Old\"\n\n[stages]\nretired = \"claude\"\n";
    let config = Project::with(text).read();
    let effective = config.effective();
    let ids: Vec<&str> = effective
        .sections
        .iter()
        .map(|section| section.id.as_str())
        .collect();
    assert_eq!(ids, ["plan", "build", "docs", "review", "fix"]);
    let docs = &effective.sections[2];
    assert_eq!(docs.label, "Write Docs");
    assert_eq!(docs.kind.as_str(), "design");
    assert_eq!(
        effective.agent_for("docs"),
        None,
        "a custom section without a stage key stays unassigned"
    );
    assert_eq!(
        codes_of(&config),
        [
            notice_codes::SECTION_UNLISTED,
            notice_codes::SECTION_UNLISTED
        ]
    );
    let keys: Vec<&str> = config
        .notices()
        .iter()
        .flat_map(|notice| notice.keys())
        .map(String::as_str)
        .collect();
    assert_eq!(keys, ["sections.retired", "stages.retired"]);
}

#[test]
fn an_invalid_section_order_is_refused() {
    let error = Project::with("[sections]\norder = [\"plan\", \"Plan\"]\n").error();
    assert_eq!(error.code(), codes::INVALID);
    let error = Project::with("[sections]\norder = [\"plan\", \"plan\"]\n").error();
    assert!(error.message().contains("twice"), "{error}");
}

#[test]
fn the_generic_section_read_does_not_check_agent_names() {
    let project = Project::with("[stages]\nplan = \"someone\"\n");
    let file = ConfigFile::read(&project.config()).unwrap();
    let mut defaults = Table::new();
    defaults.insert("plan", Value::from("claude"));
    defaults.insert("review", Value::from("codex"));
    let merged = file.section("stages", &defaults).unwrap();
    assert_eq!(merged.table.get("plan"), Some(&Value::from("someone")));
    assert_eq!(merged.table.get("review"), Some(&Value::from("codex")));
    assert!(merged.notices.is_empty());
}

#[test]
fn reading_never_writes_the_project_or_the_settings() {
    let project = Project::with("# note\n[stages]\ninitial = \"codex\"\n[unknown]\nx = 1\n");
    let settings = project.dir.path().join("home").join("settings.toml");
    fs::create_dir_all(settings.parent().unwrap()).unwrap();
    fs::write(
        &settings,
        "[automation]\nlevel = \"recommend-only\"\n[later]\nkept = true\n",
    )
    .unwrap();
    let before = listing(project.dir.path());
    let config = project.read();
    let _ = config.upgrade_warning();
    let read = UserSettings::read(&settings).unwrap();
    assert_eq!(read.values().automation_level, "recommend-only");
    assert_eq!(
        listing(project.dir.path()),
        before,
        "a read changed, added or touched a file"
    );
}

#[test]
fn user_settings_use_the_same_rules() {
    let dir = tempfile::tempdir().unwrap();
    let missing = UserSettings::read(&dir.path().join("settings.toml")).unwrap();
    assert_eq!(missing.values().automation_level, "auto-safe");
    assert_eq!(missing.values().autoload.max_skills, 3);
    assert!(missing.values().history.enabled);
    assert!(!dir.path().join("settings.toml").exists());

    let path = dir.path().join("settings.toml");
    fs::write(
        &path,
        "[history]\nretention_days = 7\nkeep_forever = true\n",
    )
    .unwrap();
    let read = UserSettings::read(&path).unwrap();
    assert_eq!(read.values().history.retention_days, 7);
    assert_eq!(read.notices().len(), 1);
    assert_eq!(read.notices()[0].code(), notice_codes::UNKNOWN_KEY);

    fs::write(&path, "[automation]\nlevel = \"everything\"\n").unwrap();
    assert_eq!(
        UserSettings::read(&path).unwrap_err().code(),
        codes::INVALID
    );
}
