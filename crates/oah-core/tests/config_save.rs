// 설정 저장 시험: 형식 보존·멱등·BOM/CRLF/끝 줄바꿈 유지·v0.4.0 승격의 승인과 백업·거부 경로가 모두 파일을 지키는지 확인한다.
use oah_core::config::{
    AgentId, AgentSource, Backup, Edit, Format, ProjectConfig, UserSettings, backup_path, codes,
    plan_project_save, save_project, save_settings,
};
use oah_core::fsx::{FileLock, LockOptions};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

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

    fn backup(&self) -> PathBuf {
        backup_path(&self.config())
    }

    fn read(&self) -> ProjectConfig {
        ProjectConfig::read(&self.config()).unwrap()
    }

    fn bytes(&self) -> Vec<u8> {
        fs::read(self.config()).unwrap()
    }

    fn text(&self) -> String {
        String::from_utf8(self.bytes()).unwrap()
    }

    /// The names in `.oah/`, sorted.
    fn oah_entries(&self) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(self.config().parent().unwrap())
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }
}

fn quick() -> LockOptions {
    LockOptions {
        wait: Duration::from_millis(200),
        poll: Duration::from_millis(10),
    }
}

fn save(
    project: &Project,
    edits: &[Edit],
) -> Result<oah_core::config::SaveOutcome, oah_core::config::ConfigError> {
    save_project(&project.read(), edits, None, quick())
}

const LEGACY: &str = "# Team settings\n\
[stages]\n\
initial = \"codex\"   # writes the first draft\n\
review = \"claude\"\n\
correction = \"codex\"\n\
\n\
# v0.4.0 engine settings\n\
[engines.claude]\n\
command = [\"claude\", \"--verbose\"]\n\
permission_mode = \"acceptEdits\"\n\
\n\
[engines.claude.tiers.high]\n\
effort = \"high\"\n\
timeout_min = 60\n\
\n\
[team]\n\
owner = \"someone\"\n";

#[test]
fn a_new_project_file_is_created_without_approval_or_backup() {
    let project = Project::empty();
    let outcome = save(&project, &[Edit::stage("plan", AgentId::Codex)]).unwrap();
    assert!(outcome.written.is_some());
    assert!(!outcome.upgraded);
    assert!(outcome.backup.is_none());
    assert_eq!(project.text(), "[stages]\nplan = \"codex\"\n");
    assert_eq!(
        project.oah_entries(),
        ["config.toml"],
        "no backup and no lock file left behind"
    );
    let config = project.read();
    assert_eq!(config.format(), Format::Current);
    assert_eq!(config.effective().agent_for("plan"), Some(AgentId::Codex));
}

#[test]
fn an_edit_changes_only_its_own_bytes_and_keeps_everything_else() {
    let original = "# top comment\n\n[stages]   # where work goes\nplan  =  'claude'  # keep me\nbuild = \"claude\"\n\n\
                    [custom]\nweird =   [ 1,2 ,3 ]   # unowned\n\n[models]\nprofile = \"balanced\"\n";
    let project = Project::with(original);
    save(&project, &[Edit::stage("plan", AgentId::Gemini)]).unwrap();
    let expected = original.replace("plan  =  'claude'", "plan  =  \"gemini\"");
    assert_eq!(project.text(), expected);
}

#[test]
fn new_keys_land_in_their_table_and_new_tables_at_the_end() {
    let original =
        "[stages]\nplan = \"claude\"\n\n# models below\n[models]\nprofile = \"balanced\"\n";
    let project = Project::with(original);
    save(
        &project,
        &[
            Edit::stage("fix", AgentId::Claude),
            Edit::set(&["skills", "update_interval_days"], 7i64),
        ],
    )
    .unwrap();
    assert_eq!(
        project.text(),
        "[stages]\nplan = \"claude\"\nfix = \"claude\"\n\n# models below\n[models]\nprofile = \"balanced\"\n\n\
         [skills]\nupdate_interval_days = 7\n"
    );
}

#[test]
fn saving_the_same_edit_twice_writes_once() {
    let project = Project::with("[stages]\nplan = \"claude\"\n");
    let first = save(&project, &[Edit::stage("plan", AgentId::Codex)]).unwrap();
    assert!(first.written.is_some());
    let after_first = project.bytes();
    let second = save(&project, &[Edit::stage("plan", AgentId::Codex)]).unwrap();
    assert!(
        second.written.is_none(),
        "an edit that changes nothing must not write"
    );
    assert_eq!(project.bytes(), after_first);
}

#[test]
fn bom_crlf_and_a_missing_final_newline_are_kept() {
    let project =
        Project::with_bytes(b"\xEF\xBB\xBF[stages]\r\nplan = \"claude\"\r\nbuild = \"claude\"");
    save(
        &project,
        &[
            Edit::stage("build", AgentId::Codex),
            Edit::set(&["models", "profile"], "efficient"),
        ],
    )
    .unwrap();
    assert_eq!(
        project.bytes(),
        b"\xEF\xBB\xBF[stages]\r\nplan = \"claude\"\r\nbuild = \"codex\"\r\n\r\n[models]\r\nprofile = \"efficient\""
    );
}

#[test]
fn removing_a_key_removes_only_its_line() {
    let project =
        Project::with("[stages]\nplan = \"codex\"  # mine\nbuild = \"claude\"\n# trailing note\n");
    save(&project, &[Edit::remove(&["stages", "plan"])]).unwrap();
    assert_eq!(
        project.text(),
        "[stages]\nbuild = \"claude\"\n# trailing note\n"
    );
}

#[test]
fn a_legacy_file_is_not_upgraded_without_approval() {
    let project = Project::with(LEGACY);
    let error = save(&project, &[Edit::stage("review", AgentId::Codex)]).unwrap_err();
    assert_eq!(error.code(), codes::UPGRADE_REQUIRED);
    assert_eq!(project.text(), LEGACY);
    assert_eq!(
        project.oah_entries(),
        ["config.toml"],
        "a refusal writes no backup and leaves no lock"
    );
}

#[test]
fn an_approval_is_valid_only_for_the_bytes_it_was_given_for() {
    let project = Project::with(LEGACY);
    let approval = project.read().upgrade_warning().unwrap().approve();
    let changed = LEGACY.replace("owner = \"someone\"", "owner = \"someone-else\"");
    fs::write(project.config(), &changed).unwrap();
    let error = save_project(
        &project.read(),
        &[Edit::stage("review", AgentId::Codex)],
        Some(&approval),
        quick(),
    )
    .unwrap_err();
    assert_eq!(error.code(), codes::UPGRADE_REQUIRED);
    assert_eq!(project.text(), changed);
    assert!(!project.backup().exists());
}

#[test]
fn an_approved_upgrade_backs_up_first_then_writes_the_new_layout() {
    let project = Project::with(LEGACY);
    let snapshot = project.read();
    let before = snapshot.effective().clone();
    let approval = snapshot.upgrade_warning().unwrap().approve();
    let outcome = save_project(
        &snapshot,
        &[Edit::stage("review", AgentId::Codex)],
        Some(&approval),
        quick(),
    )
    .unwrap();
    assert!(outcome.upgraded);
    assert!(matches!(outcome.backup, Some(Backup::Written(_))));
    assert_eq!(
        fs::read(project.backup()).unwrap(),
        LEGACY.as_bytes(),
        "the backup holds the original bytes"
    );

    // Only the keys the file gives are carried over; the line of a removed v0.4.0 key goes with its comment.
    let text = project.text();
    assert_eq!(
        text,
        "# Team settings\n\
[stages]\n\
plan = \"codex\"\n\
build = \"codex\"\n\
review = \"codex\"\n\
fix = \"codex\"\n\
\n\
# v0.4.0 engine settings\n\
[engines.claude]\n\
command = [\"claude\", \"--verbose\"]\n\
permission_mode = \"acceptEdits\"\n\
\n\
[engines.claude.tiers.high]\n\
effort = \"high\"\n\
timeout_min = 60\n\
\n\
[team]\n\
owner = \"someone\"\n\
\n\
[agents.claude]\n\
command = [\"claude\", \"--verbose\"]\n\
\n\
[agents.claude.tiers]\n\
high = { effort = \"high\" }\n"
    );
    let after = project.read();
    assert_eq!(after.format(), Format::Current);
    assert!(after.notices().is_empty(), "{:?}", after.notices());
    let effective = after.effective();
    assert_eq!(effective.agent_for("review"), Some(AgentId::Codex));
    for section in ["plan", "build", "fix"] {
        assert_eq!(
            effective.agent_for(section),
            before.agent_for(section),
            "{section}"
        );
    }
    let claude = effective.agent(AgentId::Claude);
    assert_eq!(claude.source, AgentSource::Agents);
    assert_eq!(claude.command, before.agent(AgentId::Claude).command);
    assert_eq!(claude.tiers, before.agent(AgentId::Claude).tiers);
}

#[test]
fn a_later_save_neither_upgrades_again_nor_touches_the_backup() {
    let project = Project::with(LEGACY);
    let snapshot = project.read();
    let approval = snapshot.upgrade_warning().unwrap().approve();
    save_project(
        &snapshot,
        &[Edit::stage("review", AgentId::Codex)],
        Some(&approval),
        quick(),
    )
    .unwrap();
    let backup = fs::read(project.backup()).unwrap();
    let later = project.read();
    assert!(later.upgrade_warning().is_none());
    let outcome = save_project(
        &later,
        &[Edit::stage("fix", AgentId::Claude)],
        None,
        quick(),
    )
    .unwrap();
    assert!(!outcome.upgraded);
    assert!(outcome.backup.is_none());
    assert_eq!(fs::read(project.backup()).unwrap(), backup);
    assert!(project.text().contains("fix = \"claude\"\n"));
}

#[test]
fn an_existing_backup_is_reused_when_identical_and_never_overwritten() {
    let project = Project::with(LEGACY);
    fs::write(project.backup(), LEGACY).unwrap();
    let snapshot = project.read();
    let approval = snapshot.upgrade_warning().unwrap().approve();
    let outcome = save_project(
        &snapshot,
        &[Edit::stage("review", AgentId::Codex)],
        Some(&approval),
        quick(),
    )
    .unwrap();
    assert!(matches!(outcome.backup, Some(Backup::Reused(_))));

    let project = Project::with(LEGACY);
    fs::write(project.backup(), "something else\n").unwrap();
    let snapshot = project.read();
    let approval = snapshot.upgrade_warning().unwrap().approve();
    let error = save_project(
        &snapshot,
        &[Edit::stage("review", AgentId::Codex)],
        Some(&approval),
        quick(),
    )
    .unwrap_err();
    assert_eq!(error.code(), codes::BACKUP_CONFLICT);
    assert_eq!(
        fs::read_to_string(project.backup()).unwrap(),
        "something else\n"
    );
    assert_eq!(
        project.text(),
        LEGACY,
        "the config is not replaced after a backup conflict"
    );
}

#[test]
fn a_file_changed_after_reading_is_not_overwritten() {
    let project = Project::with("[stages]\nplan = \"claude\"\n");
    let snapshot = project.read();
    fs::write(
        project.config(),
        "[stages]\nplan = \"gemini\"\n# edited elsewhere\n",
    )
    .unwrap();
    let error = save_project(
        &snapshot,
        &[Edit::stage("plan", AgentId::Codex)],
        None,
        quick(),
    )
    .unwrap_err();
    assert_eq!(error.code(), codes::CHANGED);
    assert_eq!(
        project.text(),
        "[stages]\nplan = \"gemini\"\n# edited elsewhere\n"
    );
}

#[test]
fn a_held_writer_lock_makes_the_save_busy() {
    let project = Project::with("[stages]\nplan = \"claude\"\n");
    let held = FileLock::acquire(&project.config(), LockOptions::default()).unwrap();
    let error = save(&project, &[Edit::stage("plan", AgentId::Codex)]).unwrap_err();
    assert_eq!(error.code(), "OAH_FILE_BUSY");
    assert_eq!(project.text(), "[stages]\nplan = \"claude\"\n");
    held.release().unwrap();
    save(&project, &[Edit::stage("plan", AgentId::Codex)]).unwrap();
}

#[test]
fn keys_outside_the_owned_tables_are_refused() {
    let project =
        Project::with("[engines.claude]\ncommand = [\"claude\"]\n[stages]\nplan = \"claude\"\n");
    for edit in [
        Edit::set(&["engines", "claude", "command"], vec!["x".into()]),
        Edit::set(&["stages"], "claude"),
        Edit::set(&["team", "owner"], "someone"),
    ] {
        let error = save(&project, &[edit]).unwrap_err();
        assert_eq!(error.code(), codes::KEY_NOT_OWNED);
    }
}

#[test]
fn a_result_that_would_not_read_back_is_refused_before_writing() {
    let original = "[stages]\nplan = \"claude\"\n";
    let project = Project::with(original);
    let error = save(&project, &[Edit::set(&["stages", "plan"], "cursor")]).unwrap_err();
    assert_eq!(error.code(), codes::AGENT_DEPLOY_ONLY);
    let error = save(&project, &[Edit::set(&["models", "profile"], "fastest")]).unwrap_err();
    assert_eq!(error.code(), codes::INVALID);
    assert_eq!(project.text(), original);
}

#[test]
fn a_layout_that_cannot_be_patched_in_place_is_refused_not_rewritten() {
    let original = "[agents]\nclaude.command = [\"claude\"]\n";
    let project = Project::with(original);
    let error = save(
        &project,
        &[Edit::set(
            &["agents", "claude", "quality_order"],
            vec!["m".into()],
        )],
    )
    .unwrap_err();
    assert_eq!(error.code(), codes::LAYOUT_UNSUPPORTED);
    assert_eq!(project.text(), original);
}

#[test]
fn planning_a_save_touches_nothing() {
    let project = Project::with(LEGACY);
    let plan =
        plan_project_save(&project.read(), &[Edit::stage("review", AgentId::Codex)]).unwrap();
    assert!(plan.upgrade);
    assert!(plan.bytes.is_some());
    assert_eq!(project.text(), LEGACY);
    assert_eq!(project.oah_entries(), ["config.toml"]);
}

#[test]
fn user_settings_are_saved_with_the_same_guarantees() {
    let home = tempfile::tempdir().unwrap();
    let path = home.path().join("oah-home").join("settings.toml");
    let missing = UserSettings::read(&path).unwrap();
    save_settings(
        &missing,
        &[Edit::set(&["automation", "level"], "recommend-only")],
        quick(),
    )
    .unwrap();
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "[automation]\nlevel = \"recommend-only\"\n"
    );

    fs::write(
        &path,
        "# mine\n[automation]\nlevel = \"recommend-only\"\n\n[later]\nkept = true\n",
    )
    .unwrap();
    let read = UserSettings::read(&path).unwrap();
    save_settings(
        &read,
        &[Edit::set(&["history", "retention_days"], 30i64)],
        quick(),
    )
    .unwrap();
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "# mine\n[automation]\nlevel = \"recommend-only\"\n\n[later]\nkept = true\n\n[history]\nretention_days = 30\n"
    );
    let error = save_settings(&read, &[Edit::set(&["later", "kept"], false)], quick()).unwrap_err();
    assert_eq!(error.code(), codes::KEY_NOT_OWNED);
    let error =
        save_settings(&read, &[Edit::set(&["history", "enabled"], true)], quick()).unwrap_err();
    assert_eq!(
        error.code(),
        codes::CHANGED,
        "the snapshot is older than the file"
    );
}

/// The saved file must hold no temporary files beside it.
fn no_temporary_files(folder: &Path) -> bool {
    fs::read_dir(folder).unwrap().all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .ends_with(".tmp")
    })
}

#[test]
fn saves_leave_no_temporary_files() {
    let project = Project::with(LEGACY);
    let snapshot = project.read();
    let approval = snapshot.upgrade_warning().unwrap().approve();
    save_project(
        &snapshot,
        &[Edit::stage("review", AgentId::Codex)],
        Some(&approval),
        quick(),
    )
    .unwrap();
    assert!(no_temporary_files(project.config().parent().unwrap()));
    assert_eq!(
        project.oah_entries(),
        ["config.toml", "config.toml.v0.4.bak"]
    );
}
