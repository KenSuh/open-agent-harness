// 프로젝트 위키 작업 검사: 세션의 연 프로젝트에서 개요·미리보기·적용·검색을 돌려 계획 묶음, 설정 저장 순서, 중단·재시도를 본다.
#![cfg(any(target_os = "macos", windows))]

use oah_core::app::{self, Session};
use oah_core::dto::{
    Empty, ProjectWikiDto, WikiActionKind, WikiActionReq, WikiApplyDto, WikiApplyReq,
    WikiFileAction, WikiPlanDto, WikiPlanMode, WikiSearchReq, WikiSettingValue, WikiSettingsState,
    WikiTargetState, WorkflowProjectReq,
};
use oah_core::paths::{self, Env};
use oah_core::project_tools::wiki::ItemStatus;
use oah_core::tasks::{CancelToken, NoProgress, Progress, ProgressSink};
use oah_core::{Coded, Error};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const TODAY: &str = "2026-01-02";
const V04: &str = "# written by oah init\n[stages]\ninitial = \"claude\"\nreview = \"codex\"\ncorrection = \"codex\"\n";
const STANDARD: [&str; 5] = [
    "index.md",
    "log.md",
    "wiki-guide.md",
    "templates/note.md",
    "templates/decision.md",
];

struct Fixture {
    session: Session,
    project: PathBuf,
    id: String,
    _dir: tempfile::TempDir,
}

/// Every file under `root` with its bytes; a folder with a trailing `/`.
fn tree(root: &Path) -> BTreeMap<String, Vec<u8>> {
    fn visit(folder: &Path, prefix: &str, out: &mut BTreeMap<String, Vec<u8>>) {
        for entry in fs::read_dir(folder).unwrap() {
            let entry = entry.unwrap();
            let name = format!("{prefix}{}", entry.file_name().to_string_lossy());
            if entry.file_type().unwrap().is_dir() {
                out.insert(format!("{name}/"), Vec::new());
                visit(&entry.path(), &format!("{name}/"), out);
            } else {
                out.insert(name, fs::read(entry.path()).unwrap());
            }
        }
    }
    let mut out = BTreeMap::new();
    visit(root, "", &mut out);
    out
}

impl Fixture {
    /// A project folder prepared by `prepare` and then opened in a new session.
    fn with(prepare: impl FnOnce(&Path)) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = paths::canonical_folder(dir.path()).unwrap();
        let [home, project] = ["home", "project"].map(|name| root.join(name));
        for folder in [&home, &project] {
            fs::create_dir(folder).unwrap();
        }
        prepare(&project);
        let mut env = Env::new(&root).unwrap();
        for name in [
            "PATH",
            "SystemRoot",
            "windir",
            "ComSpec",
            "PATHEXT",
            "TEMP",
            "TMP",
        ] {
            if let Some(value) = std::env::var_os(name) {
                env.set(name, value);
            }
        }
        env.set("HOME", &home);
        env.set("USERPROFILE", &home);
        env.set("GIT_CONFIG_NOSYSTEM", "1");
        env.set("GIT_CEILING_DIRECTORIES", &root);
        let session = Session::new(env);
        let id = app::project_open(&session.ctx(&CancelToken::new(), &NoProgress), &project)
            .unwrap()
            .id;
        Fixture {
            session,
            project,
            id,
            _dir: dir,
        }
    }

    fn empty() -> Self {
        Fixture::with(|_| {})
    }

    fn req<T>(&self, input: T) -> WorkflowProjectReq<T> {
        WorkflowProjectReq {
            project_id: self.id.clone(),
            input,
        }
    }

    fn overview(&self) -> ProjectWikiDto {
        let (cancel, sink) = (CancelToken::new(), NoProgress);
        app::project_wiki_overview(&self.session.ctx(&cancel, &sink), self.req(Empty {})).unwrap()
    }

    fn plan(&self, request: &WikiActionReq) -> Result<WikiPlanDto, Error> {
        let (cancel, sink) = (CancelToken::new(), NoProgress);
        app::project_wiki_plan(&self.session.ctx(&cancel, &sink), self.req(request.clone()))
    }

    fn apply_with(
        &self,
        request: WikiApplyReq,
        cancel: &CancelToken,
        sink: &dyn ProgressSink,
    ) -> Result<WikiApplyDto, Error> {
        app::project_wiki_apply(&self.session.ctx(cancel, sink), self.req(request))
    }

    /// Applies `plan` as it was shown, with the upgrade approved when it has one.
    fn apply(&self, request: &WikiActionReq, plan: &WikiPlanDto) -> Result<WikiApplyDto, Error> {
        self.apply_with(approved(request, plan), &CancelToken::new(), &NoProgress)
    }

    fn write(&self, rel: &str, bytes: &[u8]) {
        write(&self.project, rel, bytes);
    }

    fn config(&self) -> String {
        fs::read_to_string(self.project.join(".oah").join("config.toml")).unwrap()
    }
}

fn write(project: &Path, rel: &str, bytes: &[u8]) {
    let path = rel
        .split('/')
        .fold(project.to_path_buf(), |path, part| path.join(part));
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, bytes).unwrap();
}

fn setup(selected: Option<&str>) -> WikiActionReq {
    WikiActionReq::Setup {
        selected: selected.map(str::to_string),
        today: Some(TODAY.to_string()),
    }
}

fn copy(source: &str) -> WikiActionReq {
    WikiActionReq::Copy {
        source: source.to_string(),
    }
}

fn approved(request: &WikiActionReq, plan: &WikiPlanDto) -> WikiApplyReq {
    WikiApplyReq {
        request: request.clone(),
        plan_id: plan.plan_id.clone(),
        approve_upgrade: plan
            .settings
            .as_ref()
            .and_then(|settings| settings.upgrade.as_ref())
            .map(|upgrade| upgrade.fingerprint.clone()),
    }
}

fn actions(plan: &WikiPlanDto) -> Vec<(&str, WikiFileAction)> {
    plan.files
        .iter()
        .map(|file| (file.path.as_str(), file.action))
        .collect()
}

fn code<T: std::fmt::Debug>(result: Result<T, Error>) -> &'static str {
    result.expect_err("a refusal").code()
}

#[test]
fn a_project_without_a_wiki_gets_the_default_folder_from_one_reviewed_plan() {
    let fixture = Fixture::empty();
    let overview = fixture.overview();
    assert_eq!(overview.project_id, fixture.id);
    assert_eq!(overview.target.state, WikiTargetState::None);
    assert_eq!(overview.target.dir.as_deref(), Some("docs/wiki"));
    assert!(overview.candidates.is_empty() && overview.settings_error.is_none());
    assert_eq!(
        tree(&fixture.project),
        BTreeMap::new(),
        "the overview wrote"
    );

    let request = setup(None);
    let plan = fixture.plan(&request).unwrap();
    assert_eq!(
        (plan.action, plan.mode),
        (WikiActionKind::Setup, WikiPlanMode::Create)
    );
    assert_eq!(plan.dir, "docs/wiki");
    assert_eq!(plan.folders, ["docs", "docs/wiki", "docs/wiki/templates"]);
    assert_eq!(
        actions(&plan),
        STANDARD.map(|path| (path, WikiFileAction::Create))
    );
    assert_eq!(plan.writes, 5);
    assert_eq!(plan.today.as_deref(), Some(TODAY));
    assert!(
        plan.settings.is_none(),
        "the default folder needs no setting"
    );
    assert_eq!(tree(&fixture.project), BTreeMap::new(), "the preview wrote");
    assert_eq!(fixture.plan(&request).unwrap().plan_id, plan.plan_id);

    let done = fixture.apply(&request, &plan).unwrap();
    assert!(done.complete && done.stop.is_none());
    assert_eq!((done.counts.created, done.counts.kept), (5, 0));
    assert_eq!(done.settings.state, WikiSettingsState::Unchanged);
    assert!(
        !fixture.project.join(".oah").exists(),
        "no settings file was needed"
    );
    let log = fs::read_to_string(fixture.project.join("docs/wiki/log.md")).unwrap();
    assert!(log.contains(TODAY));

    assert_eq!(fixture.overview().target.state, WikiTargetState::Ready);
    // The reviewed plan is used up: the same approval no longer matches the disk.
    assert_eq!(code(fixture.apply(&request, &plan)), "OAH_PLAN_STALE");
    let again = fixture.plan(&request).unwrap();
    assert_eq!(again.writes, 0);
    assert_eq!(
        actions(&again),
        STANDARD.map(|path| (path, WikiFileAction::Keep))
    );
    let kept = fixture.apply(&request, &again).unwrap();
    assert!(kept.complete);
    assert_eq!((kept.counts.created, kept.counts.kept), (0, 5));
}

#[test]
fn every_action_needs_the_open_project_by_id() {
    let fixture = Fixture::empty();
    let (cancel, sink) = (CancelToken::new(), NoProgress);
    let ctx = fixture.session.ctx(&cancel, &sink);
    fn other<T>(input: T) -> WorkflowProjectReq<T> {
        WorkflowProjectReq {
            project_id: String::from("another-project"),
            input,
        }
    }
    assert_eq!(
        code(app::project_wiki_overview(&ctx, other(Empty {}))),
        "OAH_PROJECT_CHANGED"
    );
    assert_eq!(
        code(app::project_wiki_plan(&ctx, other(setup(None)))),
        "OAH_PROJECT_CHANGED"
    );
    let apply = WikiApplyReq {
        request: setup(None),
        plan_id: String::from("sha256:0"),
        approve_upgrade: None,
    };
    assert_eq!(
        code(app::project_wiki_apply(&ctx, other(apply))),
        "OAH_PROJECT_CHANGED"
    );
    let search = WikiSearchReq {
        dir: None,
        query: String::from("note"),
        max_hits: None,
    };
    assert_eq!(
        code(app::project_wiki_search(&ctx, other(search))),
        "OAH_PROJECT_CHANGED"
    );
    assert_eq!(tree(&fixture.project), BTreeMap::new());

    let dir = tempfile::tempdir().unwrap();
    let closed = Session::new(Env::new(dir.path()).unwrap());
    let ctx = closed.ctx(&cancel, &sink);
    assert_eq!(
        code(app::project_wiki_overview(&ctx, fixture.req(Empty {}))),
        "OAH_PROJECT_NOT_OPEN"
    );
}

#[test]
fn an_ambiguous_wiki_is_chosen_from_the_folders_found_and_its_content_stays() {
    let fixture = Fixture::with(|project| {
        write(project, "wiki/index.md", b"# Team wiki\n");
        write(project, "wiki/pages/a.md", b"alpha\n");
        write(project, "notes/.obsidian/app.json", b"{}");
        write(project, "notes/idea.md", b"# Idea\n");
    });
    let before = tree(&fixture.project);
    let overview = fixture.overview();
    assert_eq!(overview.target.state, WikiTargetState::Choose);
    let found: Vec<&str> = overview.candidates.iter().map(|c| c.dir.as_str()).collect();
    assert_eq!(found, ["wiki", "notes"]);
    assert_eq!(code(fixture.plan(&setup(None))), "OAH_WIKI_AMBIGUOUS");
    // Only a folder from that list can be chosen; a path of the caller's own is refused.
    for outside in ["docs", "src", "../outside", "/tmp", "notes/"] {
        assert_eq!(
            code(fixture.plan(&setup(Some(outside)))),
            "OAH_WIKI_DIR_INVALID",
            "{outside}"
        );
    }

    let request = setup(Some("notes"));
    let plan = fixture.plan(&request).unwrap();
    assert_eq!(
        (plan.mode, plan.dir.as_str()),
        (WikiPlanMode::Adopt, "notes")
    );
    assert_eq!(plan.folders, ["notes/templates"]);
    assert!(!plan.choice_not_saved);
    let settings = plan
        .settings
        .as_ref()
        .expect("the choice is saved as a setting");
    assert_eq!(settings.config_path, ".oah/config.toml");
    assert!(settings.upgrade.is_none() && settings.backup.is_none() && settings.kept.is_empty());
    let changed: Vec<_> = settings
        .changes
        .iter()
        .map(|change| {
            (
                change.key.as_str(),
                change.before.clone(),
                change.after.clone(),
            )
        })
        .collect();
    // The Impact Map's wiki folders follow the wiki folder, as the settings writer decides.
    assert_eq!(
        changed,
        [
            (
                "wiki.dir",
                None,
                Some(WikiSettingValue::Text(String::from("notes")))
            ),
            (
                "impact.wiki_dirs",
                None,
                Some(WikiSettingValue::List(vec![String::from("notes")]))
            )
        ]
    );
    assert_eq!(tree(&fixture.project), before, "the preview wrote");

    let done = fixture.apply(&request, &plan).unwrap();
    assert!(done.complete);
    assert_eq!(done.settings.state, WikiSettingsState::Saved);
    assert_eq!(done.folders_created, ["notes/templates"]);
    assert!(fixture.config().contains("dir = \"notes\""));
    let after = tree(&fixture.project);
    for (path, bytes) in &before {
        assert_eq!(after.get(path), Some(bytes), "{path} changed");
    }
    let overview = fixture.overview();
    assert_eq!(overview.target.state, WikiTargetState::Ready);
    assert_eq!(overview.target.dir.as_deref(), Some("notes"));
    assert!(
        overview
            .configured
            .is_some_and(|configured| configured.explicit)
    );
}

#[test]
fn a_changed_folder_or_settings_file_refuses_the_approval_before_any_file_is_made() {
    let fixture = Fixture::with(|project| write(project, "notes/.obsidian/app.json", b"{}"));
    let request = setup(None);
    let plan = fixture.plan(&request).unwrap();
    assert_eq!(plan.dir, "notes");
    assert!(plan.settings.is_some());

    // The settings file appears after the preview.
    fixture.write(".oah/config.toml", b"[rerank]\nenabled = false\n");
    let before = tree(&fixture.project);
    assert_eq!(code(fixture.apply(&request, &plan)), "OAH_PLAN_STALE");
    assert_eq!(tree(&fixture.project), before);

    // A standard file appears after the next preview.
    let plan = fixture.plan(&request).unwrap();
    fixture.write("notes/index.md", b"mine\n");
    let before = tree(&fixture.project);
    assert_eq!(code(fixture.apply(&request, &plan)), "OAH_PLAN_STALE");
    assert_eq!(tree(&fixture.project), before);

    // Another request than the one reviewed is not covered by the approval either.
    let plan = fixture.plan(&request).unwrap();
    let other = WikiApplyReq {
        request: setup(Some("notes")),
        ..approved(&request, &plan)
    };
    let later = WikiApplyReq {
        request: WikiActionReq::Setup {
            selected: None,
            today: Some(String::from("2026-01-03")),
        },
        ..approved(&request, &plan)
    };
    for changed in [other, later] {
        let refused = fixture.apply_with(changed, &CancelToken::new(), &NoProgress);
        assert_eq!(code(refused), "OAH_PLAN_STALE");
    }
    assert_eq!(tree(&fixture.project), before);
    assert!(fixture.apply(&request, &plan).unwrap().complete);
    assert_eq!(
        fs::read(fixture.project.join("notes/index.md")).unwrap(),
        b"mine\n"
    );
}

#[test]
fn a_v04_settings_file_needs_the_upgrade_approved_before_any_wiki_file_is_made() {
    let fixture = Fixture::with(|project| {
        write(project, ".oah/config.toml", V04.as_bytes());
        write(project, "notes/.obsidian/app.json", b"{}");
    });
    let request = setup(None);
    let plan = fixture.plan(&request).unwrap();
    let settings = plan.settings.as_ref().unwrap();
    let upgrade = settings.upgrade.as_ref().expect("the upgrade is shown");
    assert!(upgrade.message.contains(".oah/config.toml.v0.4.bak"));
    assert_eq!(upgrade.fingerprint.len(), 64);
    let backup = settings.backup.as_ref().expect("the backup is shown");
    assert_eq!(backup.path, ".oah/config.toml.v0.4.bak");
    assert_eq!(serde_json::to_value(backup.state).unwrap(), "write");

    let before = tree(&fixture.project);
    let unapproved = WikiApplyReq {
        approve_upgrade: None,
        ..approved(&request, &plan)
    };
    let wrong = WikiApplyReq {
        approve_upgrade: Some("0".repeat(64)),
        ..approved(&request, &plan)
    };
    for refused in [unapproved, wrong] {
        let refused = fixture.apply_with(refused, &CancelToken::new(), &NoProgress);
        assert_eq!(code(refused), "OAH_CONFIG_UPGRADE_REQUIRED");
    }
    assert_eq!(
        tree(&fixture.project),
        before,
        "a refused upgrade left files"
    );

    // A backup path taken by another file is refused before the files too.
    fixture.write(".oah/config.toml.v0.4.bak", b"something else\n");
    let conflict = fixture.plan(&request).unwrap();
    let state = conflict
        .settings
        .as_ref()
        .unwrap()
        .backup
        .as_ref()
        .unwrap()
        .state;
    assert_eq!(serde_json::to_value(state).unwrap(), "conflict");
    let before = tree(&fixture.project);
    assert_eq!(
        code(fixture.apply(&request, &conflict)),
        "OAH_CONFIG_BACKUP_CONFLICT"
    );
    assert_eq!(tree(&fixture.project), before);
    fs::remove_file(fixture.project.join(".oah").join("config.toml.v0.4.bak")).unwrap();

    let plan = fixture.plan(&request).unwrap();
    let done = fixture.apply(&request, &plan).unwrap();
    assert!(done.complete && done.settings.upgraded);
    assert_eq!(done.settings.state, WikiSettingsState::Saved);
    assert_eq!(
        done.settings.backup.as_deref(),
        Some(".oah/config.toml.v0.4.bak")
    );
    assert_eq!(
        fs::read(fixture.project.join(".oah").join("config.toml.v0.4.bak")).unwrap(),
        V04.as_bytes()
    );
    assert!(fixture.config().contains("dir = \"notes\""));
}

/// Cancels the token, or runs a step, when a given progress arrives.
struct Trigger<'a> {
    phase: &'static str,
    done: u32,
    act: Mutex<Option<Box<dyn FnOnce() + Send + 'a>>>,
    seen: Mutex<Vec<(&'static str, u32, Option<u32>)>>,
}

impl<'a> Trigger<'a> {
    fn new(phase: &'static str, done: u32, act: impl FnOnce() + Send + 'a) -> Self {
        Trigger {
            phase,
            done,
            act: Mutex::new(Some(Box::new(act))),
            seen: Mutex::new(Vec::new()),
        }
    }
}

impl ProgressSink for Trigger<'_> {
    fn progress(&self, progress: Progress) {
        self.seen
            .lock()
            .unwrap()
            .push((progress.phase, progress.done, progress.total));
        if progress.phase == self.phase
            && progress.done == self.done
            && let Some(act) = self.act.lock().unwrap().take()
        {
            act();
        }
    }
}

fn vault(project: &Path) {
    write(project, ".oah/config.toml", b"[wiki]\ndir = \"notes\"\n");
    write(project, "notes/.obsidian/app.json", b"{}");
    write(project, "notes/.obsidian/workspace.json", b"{\"open\":1}");
    write(project, "notes/a.md", b"# A\nalpha\n");
    write(project, "notes/b.md", b"# B\nbeta\n");
    write(project, "notes/deep/c.md", b"# C\ngamma\n");
}

#[test]
fn a_copy_switches_the_setting_only_after_every_file_landed_and_a_stop_is_finished_by_a_retry() {
    let fixture = Fixture::with(vault);
    let source = tree(&fixture.project.join("notes"));
    let request = copy("notes");
    let plan = fixture.plan(&request).unwrap();
    assert_eq!(
        (plan.action, plan.mode),
        (WikiActionKind::Copy, WikiPlanMode::Copy)
    );
    assert_eq!(
        (plan.dir.as_str(), plan.source.as_deref()),
        ("docs/wiki", Some("notes"))
    );
    assert_eq!(
        actions(&plan),
        [
            (".obsidian/app.json", WikiFileAction::Copy),
            ("a.md", WikiFileAction::Copy),
            ("b.md", WikiFileAction::Copy),
            ("deep/c.md", WikiFileAction::Copy)
        ]
    );
    assert_eq!(plan.files[1].bytes, Some(10));
    // Obsidian's window state is left behind; the plan says so.
    assert_eq!(plan.left, [".obsidian/workspace.json"]);
    let settings = plan.settings.as_ref().unwrap();
    assert_eq!(settings.changes[0].key, "wiki.dir");
    assert_eq!(
        settings.changes[0].before,
        Some(WikiSettingValue::Text(String::from("notes")))
    );
    assert_eq!(
        settings.changes[0].after,
        Some(WikiSettingValue::Text(String::from("docs/wiki")))
    );

    // The user stops after the first file.
    let cancel = CancelToken::new();
    let stop = Trigger::new("write-files", 1, || cancel.cancel());
    let stopped = fixture
        .apply_with(approved(&request, &plan), &cancel, &stop)
        .unwrap();
    assert!(!stopped.complete);
    assert_eq!(
        stopped.stop.as_ref().map(|stop| stop.code.as_str()),
        Some("OAH_WIKI_STOPPED")
    );
    assert_eq!((stopped.counts.created, stopped.counts.pending), (1, 3));
    assert_eq!(stopped.items[0].status, ItemStatus::Created);
    assert_eq!(stopped.settings.state, WikiSettingsState::Skipped);
    assert!(
        fixture.config().contains("dir = \"notes\""),
        "the setting moved early"
    );
    assert!(
        stop.seen
            .lock()
            .unwrap()
            .contains(&("write-files", 1, Some(4)))
    );

    // The retry lists what landed and finishes the rest.
    let rest = fixture.plan(&request).unwrap();
    assert_ne!(rest.plan_id, plan.plan_id);
    assert_eq!(rest.files[0].action, WikiFileAction::Same);
    assert_eq!(rest.writes, 3);
    let done = fixture.apply(&request, &rest).unwrap();
    assert!(done.complete);
    assert_eq!((done.counts.created, done.counts.kept), (3, 1));
    assert_eq!(done.settings.state, WikiSettingsState::Saved);
    assert!(fixture.config().contains("dir = \"docs/wiki\""));
    assert_eq!(
        tree(&fixture.project.join("notes")),
        source,
        "the source changed"
    );
    let copied = tree(&fixture.project.join("docs").join("wiki"));
    for path in [".obsidian/app.json", "a.md", "b.md", "deep/c.md"] {
        assert_eq!(copied.get(path), source.get(path), "{path}");
    }
    assert!(!copied.contains_key(".obsidian/workspace.json"));
}

#[test]
fn a_stop_before_the_first_file_ends_the_task_as_cancelled_with_nothing_written() {
    let fixture = Fixture::with(vault);
    let request = copy("notes");
    let plan = fixture.plan(&request).unwrap();
    let before = tree(&fixture.project);
    let cancel = CancelToken::new();
    cancel.cancel();
    let stopped = fixture.apply_with(approved(&request, &plan), &cancel, &NoProgress);
    assert!(stopped.expect_err("cancelled").is_cancelled());
    assert_eq!(tree(&fixture.project), before);
}

#[test]
fn settings_that_change_while_the_files_are_written_are_reported_and_saved_by_the_next_preview() {
    let fixture = Fixture::with(vault);
    let request = copy("notes");
    let plan = fixture.plan(&request).unwrap();
    let config = fixture.project.join(".oah").join("config.toml");
    let edit = Trigger::new("save-settings", 0, || {
        fs::write(&config, b"# edited meanwhile\n[wiki]\ndir = \"notes\"\n").unwrap();
    });
    let done = fixture
        .apply_with(approved(&request, &plan), &CancelToken::new(), &edit)
        .unwrap();
    assert!(!done.complete && done.stop.is_none());
    assert_eq!(done.counts.created, 4);
    assert_eq!(done.settings.state, WikiSettingsState::Stale);
    let error = done
        .settings
        .error
        .expect("why the settings were not saved");
    assert_eq!(error.code, "OAH_PLAN_STALE");
    assert!(fixture.config().starts_with("# edited meanwhile"));

    let rest = fixture.plan(&request).unwrap();
    assert_eq!(rest.writes, 0);
    assert!(
        rest.settings.is_some(),
        "the settings change is still to make"
    );
    let done = fixture.apply(&request, &rest).unwrap();
    assert!(done.complete);
    assert_eq!((done.counts.created, done.counts.kept), (0, 4));
    assert!(
        fixture
            .config()
            .starts_with("# edited meanwhile\n[wiki]\ndir = \"docs/wiki\"\n")
    );
}

#[test]
fn a_blocked_copy_is_refused_and_a_link_is_never_followed() {
    let fixture = Fixture::with(|project| {
        vault(project);
        write(project, "docs/wiki/a.md", b"other bytes\n");
    });
    // docs/wiki exists now, so it is a candidate too; the copy is still planned from the folder named.
    let request = copy("notes");
    let plan = fixture.plan(&request).unwrap();
    assert_eq!(plan.blocked_total, 1);
    assert_eq!(plan.blocked[0].path, "a.md");
    let before = tree(&fixture.project);
    assert_eq!(
        code(fixture.apply(&request, &plan)),
        "OAH_WIKI_PLAN_BLOCKED"
    );
    assert_eq!(tree(&fixture.project), before);
    assert_eq!(
        code(fixture.plan(&copy("docs/wiki"))),
        "OAH_WIKI_COPY_OVERLAP"
    );
    assert_eq!(code(fixture.plan(&copy("src"))), "OAH_WIKI_DIR_INVALID");
}

#[test]
fn choosing_the_default_folder_among_several_says_that_the_choice_is_not_saved() {
    let fixture = Fixture::with(|project| {
        write(project, "docs/wiki/index.md", b"# Docs\n");
        write(project, "wiki/index.md", b"# Other\n");
    });
    assert_eq!(fixture.overview().target.state, WikiTargetState::Choose);
    let plan = fixture.plan(&setup(Some("docs/wiki"))).unwrap();
    assert!(plan.settings.is_none() && plan.choice_not_saved);
    let plan = fixture.plan(&setup(Some("wiki"))).unwrap();
    assert!(plan.settings.is_some() && !plan.choice_not_saved);
}

#[test]
fn search_names_the_file_the_line_and_the_heading_inside_a_known_wiki_folder() {
    let fixture = Fixture::with(|project| {
        vault(project);
        write(project, "wiki/index.md", b"# Other\n\ngamma here too\n");
    });
    let search = |dir: Option<&str>, query: &str| {
        let (cancel, sink) = (CancelToken::new(), NoProgress);
        app::project_wiki_search(
            &fixture.session.ctx(&cancel, &sink),
            fixture.req(WikiSearchReq {
                dir: dir.map(str::to_string),
                query: query.to_string(),
                max_hits: None,
            }),
        )
    };
    let before = tree(&fixture.project);
    // Without a folder the one the settings name is searched.
    let found = search(None, "GAMMA").unwrap();
    assert_eq!(
        (found.project_id.as_str(), found.dir.as_str()),
        (fixture.id.as_str(), "notes")
    );
    assert_eq!(found.hits.len(), 1);
    let hit = &found.hits[0];
    assert_eq!((hit.file.as_str(), hit.line), ("deep/c.md", 2));
    assert_eq!(hit.heading.as_deref(), Some("C"));
    assert_eq!(hit.context, "gamma");
    assert!(!found.truncated && found.files_searched == 3);

    let other = search(Some("wiki"), "gamma").unwrap();
    assert_eq!((other.dir.as_str(), other.hits[0].line), ("wiki", 3));
    assert!(
        search(Some("notes"), "nothing-like-this")
            .unwrap()
            .hits
            .is_empty()
    );
    assert_eq!(
        code(search(Some("notes/deep"), "gamma")),
        "OAH_WIKI_DIR_INVALID"
    );
    assert_eq!(code(search(Some(".oah"), "dir")), "OAH_WIKI_DIR_INVALID");
    assert_eq!(code(search(None, "   ")), "OAH_WIKI_INPUT_INVALID");
    assert_eq!(tree(&fixture.project), before, "a search wrote");
}

#[test]
fn a_settings_file_that_cannot_be_read_is_said_and_nothing_is_offered_or_written() {
    let fixture = Fixture::with(|project| {
        write(project, ".oah/config.toml", b"[wiki\ndir = 3\n");
        write(project, "wiki/index.md", b"# Wiki\n\nfindable\n");
    });
    let before = tree(&fixture.project);
    let overview = fixture.overview();
    assert_eq!(overview.target.state, WikiTargetState::Unavailable);
    assert!(overview.configured.is_none());
    let error = overview.settings_error.expect("the reason");
    assert_eq!(error.code, "OAH_CONFIG_INVALID");
    assert_eq!(overview.target.error, Some(error));
    // The folders are still listed, and one of them can still be searched.
    assert_eq!(overview.candidates.len(), 1);
    assert_eq!(
        code(fixture.plan(&setup(Some("wiki")))),
        "OAH_CONFIG_INVALID"
    );
    let (cancel, sink) = (CancelToken::new(), NoProgress);
    let found = app::project_wiki_search(
        &fixture.session.ctx(&cancel, &sink),
        fixture.req(WikiSearchReq {
            dir: Some(String::from("wiki")),
            query: String::from("findable"),
            max_hits: Some(5),
        }),
    )
    .unwrap();
    assert_eq!(found.hits.len(), 1);
    assert_eq!(tree(&fixture.project), before);
}

#[test]
fn requests_read_only_the_documented_fields() {
    let read = |text: &str| serde_json::from_str::<WikiApplyReq>(text);
    let request = read(r#"{"request":{"action":"setup"},"plan_id":"sha256:1"}"#).unwrap();
    assert_eq!(
        request.request,
        WikiActionReq::Setup {
            selected: None,
            today: None
        }
    );
    let request = read(
        r#"{"request":{"action":"copy","source":"notes"},"plan_id":"p","approve_upgrade":"f"}"#,
    );
    assert_eq!(request.unwrap().request, copy("notes"));
    for refused in [
        r#"{"request":{"action":"setup","root":"/tmp"},"plan_id":"p"}"#,
        r#"{"request":{"action":"copy","source":"notes","destination":"x"},"plan_id":"p"}"#,
        r#"{"request":{"action":"move","source":"notes"},"plan_id":"p"}"#,
        r#"{"request":{"action":"setup"},"plan_id":"p","config":"x"}"#,
    ] {
        assert!(read(refused).is_err(), "{refused}");
    }
    assert!(serde_json::from_str::<WikiSearchReq>(r#"{"query":"a","root":"/"}"#).is_err());
}
