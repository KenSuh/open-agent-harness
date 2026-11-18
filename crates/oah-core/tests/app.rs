// 앱 작업 검사: 프로젝트 열기는 한 바이트도 쓰지 않고, 설정은 OAH 표만 바꾸며, CLI 복사는 게시 전에 확인하고 게시 뒤 실패를 unknown 으로 둔다.
use oah_core::app::{self, Session};
use oah_core::config::ConfigFile;
use oah_core::dto::{
    CliState, ConfigStateDto, Empty, ProjectKindDto, RefreshDto, SettingEdit, SettingValue,
    SettingsDto, SettingsSetReq, UiDto,
};
use oah_core::fsx::SelfWriteMatch;
use oah_core::paths::{self, Env};
use oah_core::tasks::{CancelToken, NoProgress, TaskEvent};
use oah_core::{Coded, Error};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};

const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };

/// A temporary root holding `home` (the home folder, so `OAH_HOME` is `home/.oah`) and room for projects and
/// bundles. As in `tests/paths.rs`, the environment is built from nothing: the process's `PATH` (to find Git),
/// the platform's home variable, no system Git configuration, and a ceiling so Git never looks above the root.
struct Fixture {
    _dir: tempfile::TempDir,
    root: PathBuf,
    home: PathBuf,
    session: Session,
}

impl Fixture {
    fn new() -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let root = paths::canonical_folder(&fs::canonicalize(dir.path()).unwrap()).unwrap();
        let home = root.join("home");
        fs::create_dir_all(&home).unwrap();
        let mut env = Env::new(&root).unwrap();
        if let Some(path) = std::env::var_os("PATH") {
            env.set("PATH", path);
        }
        if cfg!(windows) {
            for name in ["SystemRoot", "windir", "ComSpec", "PATHEXT", "TEMP", "TMP"] {
                if let Some(value) = std::env::var_os(name) {
                    env.set(name, value);
                }
            }
            env.set("HOME", &home);
        }
        env.set(HOME_VAR, &home);
        env.set("GIT_CONFIG_NOSYSTEM", "1");
        env.set("GIT_CEILING_DIRECTORIES", &root);
        Fixture {
            _dir: dir,
            root,
            home,
            session: Session::new(env),
        }
    }

    fn oah_home(&self) -> PathBuf {
        self.home.join(".oah")
    }

    fn settings(&self) -> PathBuf {
        self.oah_home().join("settings.toml")
    }

    /// A folder below the home folder; `relative` separates its parts with `/`.
    fn folder(&self, relative: &str) -> PathBuf {
        let folder = relative
            .split('/')
            .fold(self.home.clone(), |path, part| path.join(part));
        fs::create_dir_all(&folder).unwrap();
        folder
    }
}

/// How a path below the home folder is shown: `~`, then the parts joined with the platform's separator.
fn shown(parts: &[&str]) -> String {
    parts
        .iter()
        .fold(PathBuf::from("~"), |path, part| path.join(part))
        .display()
        .to_string()
}

/// Every file and folder below `root` with each file's bytes.
fn tree(root: &Path) -> BTreeMap<PathBuf, Option<Vec<u8>>> {
    let mut out = BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        for entry in fs::read_dir(&folder).unwrap() {
            let path = entry.unwrap().path();
            if fs::symlink_metadata(&path).unwrap().is_dir() {
                out.insert(path.clone(), None);
                pending.push(path);
            } else {
                out.insert(path.clone(), Some(fs::read(&path).unwrap()));
            }
        }
    }
    out
}

/// `git init` in `folder` with the fixture's environment only.
fn git_init(fixture: &Fixture, folder: &Path) {
    let output = Command::new("git")
        .arg("-C")
        .arg(folder)
        .args(["init", "-q"])
        .env_clear()
        .envs(fixture.session.env().vars())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git init: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn with_ctx<T>(fixture: &Fixture, op: impl FnOnce(&app::Ctx<'_>) -> T) -> T {
    let cancel = CancelToken::new();
    op(&fixture.session.ctx(&cancel, &NoProgress))
}

fn edit(key: &str, value: SettingValue) -> SettingEdit {
    SettingEdit {
        key: key.to_string(),
        value,
    }
}

#[test]
fn opening_a_git_work_tree_with_a_legacy_config_writes_nothing() {
    let fixture = Fixture::new();
    let repo = fixture.folder("code/app");
    git_init(&fixture, &repo);
    fs::create_dir_all(repo.join(".oah")).unwrap();
    fs::write(
        repo.join(".oah").join("config.toml"),
        "# v0.4.0\n[stages]\ninitial = \"codex\"\n",
    )
    .unwrap();
    let before = tree(&fixture.root);
    let project = with_ctx(&fixture, |ctx| app::project_open(ctx, &repo)).unwrap();
    assert_eq!(project.kind, ProjectKindDto::Git);
    assert_eq!(project.config, ConfigStateDto::Legacy);
    assert_eq!(project.root, shown(&["code", "app"]));
    assert_eq!(
        project.id,
        paths::folder_id(&paths::canonical_folder(&repo).unwrap())
    );
    assert_eq!(tree(&fixture.root), before, "opening changed a byte");
    assert!(!repo.join(".git").join("oah").exists());
    let current = with_ctx(&fixture, |ctx| app::project_current(ctx, Empty {})).unwrap();
    assert_eq!(current, Some(project));
}

#[test]
fn opening_a_plain_folder_creates_nothing() {
    let fixture = Fixture::new();
    let notes = fixture.folder("notes");
    fs::write(notes.join("todo.txt"), "x").unwrap();
    let before = tree(&fixture.root);
    let project = with_ctx(&fixture, |ctx| app::project_open(ctx, &notes)).unwrap();
    assert_eq!(project.kind, ProjectKindDto::Folder);
    assert_eq!(project.config, ConfigStateDto::Missing);
    assert_eq!(project.root, shown(&["notes"]));
    assert!(project.notices.is_empty());
    assert_eq!(tree(&fixture.root), before, "opening changed a byte");
}

#[test]
fn an_invalid_config_still_opens_and_carries_its_error() {
    let fixture = Fixture::new();
    let notes = fixture.folder("notes");
    fs::create_dir_all(notes.join(".oah")).unwrap();
    fs::write(notes.join(".oah").join("config.toml"), "stages = [\n").unwrap();
    let before = tree(&fixture.root);
    let project = with_ctx(&fixture, |ctx| app::project_open(ctx, &notes)).unwrap();
    let ConfigStateDto::Invalid { error } = &project.config else {
        panic!("expected an invalid config, got {:?}", project.config);
    };
    assert_eq!(error.code, "OAH_CONFIG_INVALID");
    assert!(
        error
            .message
            .starts_with(&shown(&["notes", ".oah", "config.toml"])),
        "{}",
        error.message
    );
    assert!(error.hint.is_some());
    assert_eq!(tree(&fixture.root), before);
}

#[test]
fn a_file_is_not_a_folder_and_a_missing_path_is_missing() {
    let fixture = Fixture::new();
    let file = fixture.home.join("file.txt");
    fs::write(&file, "x").unwrap();
    let not_folder = with_ctx(&fixture, |ctx| app::project_open(ctx, &file)).unwrap_err();
    assert_eq!(not_folder.code(), "OAH_PROJECT_NOT_FOLDER");
    assert_eq!(
        not_folder.to_dto(fixture.session.env()).message,
        format!("{} is not a folder", shown(&["file.txt"]))
    );
    let missing = fixture.home.join("gone");
    let error = with_ctx(&fixture, |ctx| app::project_open(ctx, &missing)).unwrap_err();
    assert_eq!(error.code(), "OAH_PROJECT_MISSING");
    assert_eq!(
        with_ctx(&fixture, |ctx| app::project_current(ctx, Empty {})).unwrap(),
        None
    );
}

#[test]
fn refresh_rereads_the_open_project_and_reports_real_counts() {
    let fixture = Fixture::new();
    let nothing = with_ctx(&fixture, |ctx| app::refresh(ctx, Empty {})).unwrap();
    assert_eq!(
        nothing,
        RefreshDto {
            projects: 0,
            notices: 0
        }
    );
    let notes = fixture.folder("notes");
    with_ctx(&fixture, |ctx| app::project_open(ctx, &notes)).unwrap();
    fs::create_dir_all(notes.join(".oah")).unwrap();
    fs::write(
        notes.join(".oah").join("config.toml"),
        "[models]\nprofil = \"efficient\"\n",
    )
    .unwrap();
    let events: Arc<Mutex<Vec<TaskEvent<RefreshDto>>>> = Arc::default();
    let sink = Arc::clone(&events);
    let begin = fixture
        .session
        .tasks()
        .begin(
            app::refresh_key(),
            Arc::new(move |event: &TaskEvent<RefreshDto>| sink.lock().unwrap().push(event.clone())),
        )
        .unwrap();
    let oah_core::tasks::Begin::Started(handle) = begin else {
        panic!("nothing else is refreshing");
    };
    let before = tree(&fixture.root);
    app::run_task(&fixture.session, handle, |ctx| app::refresh(ctx, Empty {}));
    let events = events.lock().unwrap();
    assert_eq!(
        events.last(),
        Some(&TaskEvent::Done(RefreshDto {
            projects: 1,
            notices: 1
        }))
    );
    assert!(matches!(events.first(), Some(TaskEvent::Progress(_))));
    let current = with_ctx(&fixture, |ctx| app::project_current(ctx, Empty {}))
        .unwrap()
        .unwrap();
    assert_eq!(current.notices.len(), 1);
    assert_eq!(current.notices[0].code, "OAH_CONFIG_UNKNOWN_KEY");
    assert_eq!(
        tree(&fixture.root),
        before,
        "this slice's refresh writes nothing"
    );
}

#[test]
fn settings_set_changes_only_the_ui_keys_and_records_its_write() {
    let fixture = Fixture::new();
    fs::create_dir_all(fixture.oah_home()).unwrap();
    let original = "# mine\n[automation]\nlevel = \"recommend-only\"\n\n[later]\nkept = true\n";
    fs::write(fixture.settings(), original).unwrap();
    let saved = with_ctx(&fixture, |ctx| {
        app::settings_set(
            ctx,
            SettingsSetReq {
                edits: vec![
                    edit("ui.theme", SettingValue::Text(String::from("dark"))),
                    edit("ui.quick_start_dismissed", SettingValue::Bool(true)),
                ],
            },
        )
    })
    .unwrap();
    let expected = SettingsDto {
        automation_level: String::from("recommend-only"),
        ui: UiDto {
            theme: String::from("dark"),
            quick_start_dismissed: true,
        },
    };
    assert_eq!(saved, expected);
    let after = fs::read_to_string(fixture.settings()).unwrap();
    let added = after
        .strip_prefix(original)
        .unwrap_or_else(|| panic!("the original bytes changed:\n{after}"));
    let parsed =
        ConfigFile::from_bytes(Path::new("added.toml"), Some(added.as_bytes().to_vec())).unwrap();
    let tables: Vec<&str> = parsed.values().keys().collect();
    assert_eq!(tables, ["ui"]);
    let ui = parsed
        .values()
        .get("ui")
        .and_then(|ui| ui.as_table())
        .unwrap();
    let mut keys: Vec<&str> = ui.keys().collect();
    keys.sort_unstable();
    assert_eq!(keys, ["quick_start_dismissed", "theme"]);
    assert_eq!(
        with_ctx(&fixture, |ctx| app::settings_get(ctx, Empty {})).unwrap(),
        expected
    );
    assert_eq!(
        fixture
            .session
            .self_writes()
            .observe(&fixture.settings(), after.as_bytes()),
        SelfWriteMatch::Own
    );
}

#[test]
fn settings_outside_the_owned_tables_or_with_bad_values_are_refused_without_a_write() {
    let fixture = Fixture::new();
    let before = tree(&fixture.root);
    let refused = |key: &str, value: SettingValue| -> Error {
        with_ctx(&fixture, |ctx| {
            app::settings_set(
                ctx,
                SettingsSetReq {
                    edits: vec![edit(key, value)],
                },
            )
        })
        .unwrap_err()
    };
    for key in [
        "theme",
        "agents.claude.model",
        "ui..theme",
        ".ui",
        "projects.recent",
    ] {
        let error = refused(key, SettingValue::Text(String::from("x")));
        assert_eq!(error.code(), "OAH_CONFIG_KEY_NOT_OWNED", "{key}");
    }
    let blue = refused("ui.theme", SettingValue::Text(String::from("blue")));
    assert_eq!(blue.code(), "OAH_CONFIG_INVALID");
    let text_flag = refused(
        "ui.quick_start_dismissed",
        SettingValue::Text(String::from("yes")),
    );
    assert_eq!(text_flag.code(), "OAH_CONFIG_INVALID");
    assert_eq!(
        tree(&fixture.root),
        before,
        "a refused edit wrote something"
    );
    let defaults = with_ctx(&fixture, |ctx| app::settings_get(ctx, Empty {})).unwrap();
    assert_eq!(defaults.automation_level, "auto-safe");
    assert_eq!(defaults.ui.theme, "auto");
    assert!(!defaults.ui.quick_start_dismissed);
}

#[test]
fn read_commands_change_nothing() {
    let fixture = Fixture::new();
    fs::create_dir_all(fixture.oah_home()).unwrap();
    fs::write(fixture.settings(), "[ui]\ntheme = \"light\"\n").unwrap();
    let notes = fixture.folder("notes");
    with_ctx(&fixture, |ctx| app::project_open(ctx, &notes)).unwrap();
    let before = tree(&fixture.root);
    with_ctx(&fixture, |ctx| {
        let info = app::app_info(ctx, Empty {}).unwrap();
        assert_eq!(info.oah_home, shown(&[".oah"]));
        assert_eq!(info.version, oah_core::VERSION);
        assert!(info.cli.is_none(), "no CLI copy ran in this session");
        assert_eq!(app::settings_get(ctx, Empty {}).unwrap().ui.theme, "light");
        assert!(app::project_current(ctx, Empty {}).unwrap().is_some());
        assert!(app::tasks_list(ctx, Empty {}).unwrap().is_empty());
    });
    assert_eq!(tree(&fixture.root), before);
}

#[cfg(unix)]
mod cli_copy {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn script(fixture: &Fixture, name: &str, printed: &str) -> PathBuf {
        let folder = fixture.root.join("bundle");
        fs::create_dir_all(&folder).unwrap();
        let path = folder.join(name);
        fs::write(&path, format!("#!/bin/sh\necho '{printed}'\n")).unwrap();
        path
    }

    fn good(fixture: &Fixture) -> PathBuf {
        script(fixture, "oah", &format!("oah {}", oah_core::VERSION))
    }

    fn dest(fixture: &Fixture) -> PathBuf {
        fixture.oah_home().join("bin").join("oah")
    }

    fn bin_names(fixture: &Fixture) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(fixture.oah_home().join("bin"))
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort_unstable();
        names
    }

    #[test]
    fn a_missing_bundled_cli_keeps_what_is_there_and_reports_no_version() {
        let fixture = Fixture::new();
        let before = tree(&fixture.root);
        let missing = fixture.root.join("bundle").join("oah");
        let copy = with_ctx(&fixture, |ctx| app::cli_copy(ctx, &missing));
        assert_eq!(copy.state, CliState::KeptPrevious);
        assert_eq!(copy.version, None);
        let notice = copy.notice.clone().unwrap();
        assert_eq!(notice.code, "OAH_CLI_COPY_FAILED");
        assert!(
            notice.message.contains("(bundled CLI not found)"),
            "{}",
            notice.message
        );
        assert_eq!(
            tree(&fixture.root),
            before,
            "nothing may be read, run or written"
        );
        let info = with_ctx(&fixture, |ctx| app::app_info(ctx, Empty {})).unwrap();
        let cli = info.cli.unwrap();
        assert_eq!(cli.state, CliState::KeptPrevious);
        assert_eq!(cli.path, "~/.oah/bin/oah");
        assert_eq!(cli.version, None);
        assert_eq!(info.notices, [notice]);
    }

    #[test]
    fn a_candidate_that_fails_its_version_check_is_never_published() {
        let fixture = Fixture::new();
        fs::create_dir_all(dest(&fixture).parent().unwrap()).unwrap();
        fs::write(dest(&fixture), "previous copy").unwrap();
        let wrong = script(&fixture, "oah", "oah 9.9.9");
        let copy = with_ctx(&fixture, |ctx| app::cli_copy(ctx, &wrong));
        assert_eq!(copy.state, CliState::KeptPrevious);
        assert_eq!(copy.version, None);
        let notice = copy.notice.unwrap();
        assert!(
            notice.message.contains("(the --version check)"),
            "{}",
            notice.message
        );
        assert_eq!(fs::read(dest(&fixture)).unwrap(), b"previous copy");
        assert_eq!(
            bin_names(&fixture),
            ["oah"],
            "the temporary candidate must be removed"
        );
    }

    #[test]
    fn a_valid_candidate_is_published_and_a_second_start_leaves_it_untouched() {
        let fixture = Fixture::new();
        let bundled = good(&fixture);
        let copy = with_ctx(&fixture, |ctx| app::cli_copy(ctx, &bundled));
        assert_eq!(copy.state, CliState::Copied, "{:?}", copy.notice);
        assert_eq!(copy.version.as_deref(), Some(oah_core::VERSION));
        assert_eq!(
            fs::read(dest(&fixture)).unwrap(),
            fs::read(&bundled).unwrap()
        );
        let mode = fs::metadata(dest(&fixture)).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o755);
        assert_eq!(bin_names(&fixture), ["oah"]);
        let modified = fs::metadata(dest(&fixture)).unwrap().modified().unwrap();
        let again = with_ctx(&fixture, |ctx| app::cli_copy(ctx, &bundled));
        assert_eq!(again.state, CliState::Unchanged);
        assert_eq!(again.version.as_deref(), Some(oah_core::VERSION));
        assert_eq!(
            fs::metadata(dest(&fixture)).unwrap().modified().unwrap(),
            modified
        );
    }

    #[test]
    fn a_failure_after_the_rename_is_unknown_and_the_next_start_checks_again() {
        let fixture = Fixture::new();
        let bundled = good(&fixture);
        let bin = dest(&fixture).parent().unwrap().to_path_buf();
        fs::create_dir_all(&bin).unwrap();
        fs::set_permissions(&bin, fs::Permissions::from_mode(0o300)).unwrap();
        if fs::read_dir(&bin).is_ok() {
            fs::set_permissions(&bin, fs::Permissions::from_mode(0o755)).unwrap();
            eprintln!("skipped: this user can read a folder without read permission");
            return;
        }
        let copy = with_ctx(&fixture, |ctx| app::cli_copy(ctx, &bundled));
        fs::set_permissions(&bin, fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(copy.state, CliState::Unknown, "{:?}", copy.notice);
        assert_eq!(copy.version, None);
        let notice = copy.notice.unwrap();
        assert_eq!(notice.code, "OAH_FILE_UNVERIFIED");
        assert!(
            notice
                .message
                .starts_with("~/.oah/bin/oah: the CLI copy may be"),
            "{}",
            notice.message
        );
        let next = with_ctx(&fixture, |ctx| app::cli_copy(ctx, &bundled));
        assert_eq!(next.state, CliState::Unchanged);
        assert_eq!(next.version.as_deref(), Some(oah_core::VERSION));
    }
}
