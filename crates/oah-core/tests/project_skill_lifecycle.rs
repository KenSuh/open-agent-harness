#![cfg(any(target_os = "macos", windows))]

use oah_core::app::{self, Session};
use oah_core::dto::{
    ApplyProjectSkillDto, ApplyProjectSkillReq, ApplyProjectSkillStatus as Status, Empty,
    InstalledSkillDto, ReviewOutcomeDto, ReviewReq, ReviewSpecDto, SkillScopeDto,
};
use oah_core::fsx::skill_publish::Engine;
use oah_core::net::RecordingTransport;
use oah_core::paths::{self, Env};
use oah_core::registry::github::Recorded;
use oah_core::review::Store;
use oah_core::tasks::{CancelToken, NoProgress};
use oah_core::{Coded, Error};
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

const REPO: &str = "acme/skills";
const SKILL_PATH: &str = "skills/tidy";
const COMMIT_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const COMMIT_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const SKILL_A: &str = "---\r\nname: tidy\r\ndescription: Tidies notes\r\n---\r\nShort notes.\r\n";
const SKILL_B: &str = "---\nname: tidy\ndescription: Tidies notes\n---\nKeep notes short.\n";

struct Fixture {
    session: Session,
    transport: Arc<RecordingTransport>,
    project: PathBuf,
    home: PathBuf,
    id: String,
    _dir: tempfile::TempDir,
}

impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = paths::canonical_folder(dir.path()).unwrap();
        let [home, project] = ["home", "project"].map(|name| root.join(name));
        for folder in [&home, &project] {
            fs::create_dir(folder).unwrap();
        }
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
        let transport = Arc::new(RecordingTransport::new());
        let session = Session::with_transport(env, transport.clone());
        let id = app::project_open(&session.ctx(&CancelToken::new(), &NoProgress), &project)
            .unwrap()
            .id;
        Self {
            session,
            transport,
            project,
            home,
            id,
            _dir: dir,
        }
    }

    fn ctx<T>(&self, op: impl FnOnce(&app::Ctx<'_>) -> T) -> T {
        op(&self.session.ctx(&CancelToken::new(), &NoProgress))
    }

    fn review(&self, repo: &str, recorded: &Recorded) -> ReviewOutcomeDto {
        recorded.script(&self.transport);
        self.ctx(|ctx| {
            app::review(
                ctx,
                ReviewReq {
                    spec: ReviewSpecDto {
                        repo: repo.into(),
                        path: SKILL_PATH.into(),
                        reference: None,
                    },
                    search_name: None,
                },
            )
        })
        .unwrap()
    }

    fn request(
        &self,
        engine: Engine,
        next: &ReviewOutcomeDto,
        prior: Option<&ReviewOutcomeDto>,
    ) -> ApplyProjectSkillReq {
        ApplyProjectSkillReq {
            project: self.id.clone(),
            engine,
            review_id: next.record.id.clone(),
            expected_review_id: prior.map(|review| review.record.id.clone()),
            acknowledge_warnings: false,
        }
    }

    fn apply(&self, req: &ApplyProjectSkillReq) -> Result<ApplyProjectSkillDto, Error> {
        let contacted = self.transport.requests().len();
        let result = self.ctx(|ctx| app::apply_project_skill(ctx, req));
        assert_eq!(self.transport.requests().len(), contacted);
        result
    }

    fn assert_refused(&self, req: &ApplyProjectSkillReq, code: &str) {
        let error = self.apply(req).unwrap_err();
        assert_eq!(error.code(), code, "{error}");
    }

    fn inventory(&self) -> Vec<InstalledSkillDto> {
        self.ctx(|ctx| app::skills_installed(ctx, Empty {}))
            .unwrap()
    }

    fn set_policy(&self, allow: &str) {
        fs::create_dir_all(self.project.join(".oah")).unwrap();
        fs::write(
            self.project.join(".oah/config.toml"),
            format!("[skills.review]\nlicense_allow = {allow}\n"),
        )
        .unwrap();
    }
}

fn recorded(repo: &str, commit: &str, skill: &str) -> Recorded {
    let input: Value = serde_json::from_str(include_str!(
        "../../../conformance/v0.4.0/review/record-mit-reviewed/input.json"
    ))
    .unwrap();
    let license = input["upstream"]["files"]["LICENSE"].as_str().unwrap();
    Recorded::new(repo, commit)
        .file("LICENSE", license)
        .file("skills/tidy/SKILL.md", skill)
        .file("skills/tidy/notes/source.txt", commit)
}

fn assert_installed(folder: &Path, body: &str, commit: &str) {
    assert_eq!(fs::read(folder.join("SKILL.md")).unwrap(), body.as_bytes());
    assert_eq!(
        fs::read(folder.join("notes/source.txt")).unwrap(),
        commit.as_bytes()
    );
}

#[test]
fn reviewed_install_and_update_publish_exact_snapshots_for_each_engine() {
    let fixture = Fixture::new();
    let old = fixture.review(REPO, &recorded(REPO, COMMIT_A, SKILL_A));
    let new = fixture.review(REPO, &recorded(REPO, COMMIT_B, SKILL_B));

    for (engine, path) in [(Engine::Claude, ".claude"), (Engine::Codex, ".agents")] {
        let destination = fixture.project.join(path).join("skills/tidy");
        let install = fixture.request(engine, &old, None);
        let installed = fixture.apply(&install).unwrap();
        assert_eq!(installed.status, Status::Installed);
        assert_eq!(installed.review_id, old.record.id);
        assert_installed(&destination, SKILL_A, COMMIT_A);
        let unchanged = fixture.apply(&install).unwrap();
        assert_eq!(unchanged.status, Status::Unchanged);
        assert_eq!(unchanged.review_id, old.record.id);

        let update = fixture.request(engine, &new, Some(&old));
        let (first, second) = std::thread::scope(|scope| {
            let first = scope.spawn(|| fixture.apply(&update));
            let second = fixture.apply(&update);
            (first.join().unwrap().unwrap(), second.unwrap())
        });
        assert!(matches!(
            (first.status, second.status),
            (Status::Updated, Status::Unchanged) | (Status::Unchanged, Status::Updated)
        ));
        assert_eq!(first.review_id, new.record.id);
        assert_eq!(second.review_id, new.record.id);
        assert_installed(&destination, SKILL_B, COMMIT_B);
    }

    for alias in [".codex", ".opencode"] {
        let folder = fixture.project.join(alias).join("skills/tidy");
        fs::create_dir_all(&folder).unwrap();
        fs::write(folder.join("SKILL.md"), SKILL_B).unwrap();
    }
    let rows = fixture.inventory();
    assert_eq!(rows.len(), 4);
    for row in rows {
        assert_eq!(row.scope, SkillScopeDto::Project);
        let path = Path::new(&row.location);
        if path.ends_with(".codex/skills/tidy") || path.ends_with(".opencode/skills/tidy") {
            assert!(row.managed.is_none(), "{row:?}");
            continue;
        }
        let managed = row.managed.unwrap();
        assert_eq!(managed.review_id, new.record.id);
        assert_eq!(managed.snapshot_id, new.record.source.snapshot);
        assert_eq!((&*managed.repo, &*managed.path), (REPO, SKILL_PATH));
    }
}

#[test]
fn re_reviewing_the_same_snapshot_keeps_the_original_receipt() {
    let fixture = Fixture::new();
    let files = recorded(REPO, COMMIT_A, SKILL_A);
    let first = fixture.review(REPO, &files);
    fixture
        .apply(&fixture.request(Engine::Claude, &first, None))
        .unwrap();
    fixture.set_policy(r#"["MIT"]"#);
    let repeated = fixture.review(REPO, &files);
    assert_ne!(repeated.record.id, first.record.id);
    assert_eq!(
        repeated.record.source.snapshot,
        first.record.source.snapshot
    );

    let result = fixture
        .apply(&fixture.request(Engine::Claude, &repeated, Some(&first)))
        .unwrap();
    assert_eq!(result.status, Status::Unchanged);
    assert_eq!(result.review_id, first.record.id);
    assert_eq!(
        fixture.inventory()[0].managed.as_ref().unwrap().review_id,
        first.record.id
    );
}

#[test]
fn blocked_reviews_and_unacknowledged_warnings_leave_the_project_untouched() {
    let fixture = Fixture::new();
    let blocked = recorded(
        REPO,
        COMMIT_A,
        &format!("{SKILL_A}\n<!-- oah reserved -->\n"),
    );
    let blocked = fixture.review(REPO, &blocked);
    let mut blocked_req = fixture.request(Engine::Claude, &blocked, None);
    blocked_req.acknowledge_warnings = true;
    fixture.assert_refused(&blocked_req, "OAH_SKILL_INSTALL_BLOCKED");

    let warned =
        recorded(REPO, COMMIT_B, SKILL_B).file("skills/tidy/scripts/note.sh", "# fixture only\n");
    let warned = fixture.review(REPO, &warned);
    assert_eq!(warned.gate.result, "warn");
    let mut warn_req = fixture.request(Engine::Claude, &warned, None);
    fixture.assert_refused(&warn_req, "OAH_SKILL_APPROVAL_REQUIRED");
    assert_eq!(fs::read_dir(&fixture.project).unwrap().count(), 0);

    warn_req.acknowledge_warnings = true;
    fixture.set_policy(r#"["Apache-2.0"]"#);
    fixture.assert_refused(&warn_req, "OAH_SKILL_INSTALL_BLOCKED");
    assert!(!fixture.project.join(".claude").exists());
    assert!(!fixture.project.join(".oah/project-skills-v1").exists());
    fixture.set_policy("[]");
    assert_eq!(fixture.apply(&warn_req).unwrap().status, Status::Installed);
    let destination = fixture.project.join(".claude/skills/tidy");
    assert_installed(&destination, SKILL_B, COMMIT_B);
    assert_eq!(
        fs::read(destination.join("scripts/note.sh")).unwrap(),
        b"# fixture only\n"
    );
}

#[test]
fn edits_missing_review_evidence_and_untracked_skills_preserve_existing_bytes() {
    let fixture = Fixture::new();
    let old = fixture.review(REPO, &recorded(REPO, COMMIT_A, SKILL_A));
    let new = fixture.review(REPO, &recorded(REPO, COMMIT_B, SKILL_B));
    let user = fixture.home.join(".claude/skills/tidy");
    let untracked = fixture.project.join(".agents/skills/tidy");
    for (folder, bytes) in [(&user, "user skill"), (&untracked, "untracked skill")] {
        fs::create_dir_all(folder).unwrap();
        fs::write(folder.join("SKILL.md"), bytes).unwrap();
    }
    let occupied = fixture.request(Engine::Codex, &old, None);
    fixture.assert_refused(&occupied, "OAH_SKILL_DESTINATION_OCCUPIED");
    fixture
        .apply(&fixture.request(Engine::Claude, &old, None))
        .unwrap();

    let live = fixture.project.join(".claude/skills/tidy/SKILL.md");
    fs::write(&live, "local edit").unwrap();
    let update = fixture.request(Engine::Claude, &new, Some(&old));
    fixture.assert_refused(&update, "OAH_SKILL_ACQUISITION_CHANGED");
    let rows = fixture.inventory();
    assert_eq!(rows.len(), 3);
    assert!(
        rows.iter()
            .all(|row| serde_json::to_value(row).unwrap().get("managed").is_none())
    );
    assert_eq!(fs::read(&live).unwrap(), b"local edit");
    assert_eq!(fs::read(user.join("SKILL.md")).unwrap(), b"user skill");
    assert_eq!(
        fs::read(untracked.join("SKILL.md")).unwrap(),
        b"untracked skill"
    );

    fs::write(&live, SKILL_A).unwrap();
    let store = Store::new(&paths::user_state_dir(fixture.session.env()).unwrap());
    let review_hex = old.record.id.strip_prefix("sha256:").unwrap();
    fs::remove_file(store.reviews_dir().join(format!("{review_hex}.json"))).unwrap();
    fixture.assert_refused(&update, "OAH_SKILL_RECEIPT_INVALID");
    assert!(fixture.inventory().iter().all(|row| row.managed.is_none()));
    assert_eq!(fs::read(&live).unwrap(), SKILL_A.as_bytes());
}

#[test]
fn stale_revisions_foreign_sources_and_project_switches_cannot_apply() {
    let fixture = Fixture::new();
    let old = fixture.review(REPO, &recorded(REPO, COMMIT_A, SKILL_A));
    let new = fixture.review(REPO, &recorded(REPO, COMMIT_B, SKILL_B));
    let foreign = fixture.review("acme/other", &recorded("acme/other", COMMIT_A, SKILL_A));
    fixture
        .apply(&fixture.request(Engine::Claude, &new, None))
        .unwrap();

    let stale = fixture.request(Engine::Claude, &old, Some(&old));
    fixture.assert_refused(&stale, "OAH_SKILL_REVISION_STALE");
    let mismatch = fixture.request(Engine::Claude, &foreign, Some(&new));
    fixture.assert_refused(&mismatch, "OAH_SKILL_SOURCE_MISMATCH");
    let other = fixture.project.with_file_name("other");
    fs::create_dir(&other).unwrap();
    fixture.ctx(|ctx| {
        app::project_open(ctx, &other).unwrap();
    });
    fixture.assert_refused(&stale, "OAH_PROJECT_CHANGED");
    assert_installed(
        &fixture.project.join(".claude/skills/tidy"),
        SKILL_B,
        COMMIT_B,
    );
    assert_eq!(fs::read_dir(other).unwrap().count(), 0);
}

#[test]
fn the_physical_home_cannot_be_selected_for_skill_writes() {
    let fixture = Fixture::new();
    let review = fixture.review(REPO, &recorded(REPO, COMMIT_A, SKILL_A));
    let alias = fixture.home.with_file_name("home-alias");
    #[cfg(unix)]
    std::os::unix::fs::symlink(&fixture.home, &alias).unwrap();
    #[cfg(windows)]
    junction::create(&fixture.home, &alias).unwrap();
    let mut req = fixture.request(Engine::Codex, &review, None);
    fixture.ctx(|ctx| {
        req.project = app::project_open(ctx, &alias).unwrap().id;
    });
    fixture.assert_refused(&req, "OAH_SKILL_HOME_SCOPE");
    assert!(!fixture.home.join(".agents").exists());
    #[cfg(windows)]
    junction::delete(&alias).unwrap();
}

#[test]
fn the_session_home_and_reserved_subtrees_are_read_only_for_skill_apply() {
    let fixture = Fixture::new();
    assert_ne!(
        paths::user_home(&Env::from_process().unwrap()).ok(),
        Some(fixture.home.clone())
    );
    let old = fixture.review(REPO, &recorded(REPO, COMMIT_A, SKILL_A));
    let new = fixture.review(REPO, &recorded(REPO, COMMIT_B, SKILL_B));
    let protected = [
        "",
        ".claude",
        ".agents",
        ".oah",
        ".claude/nested",
        ".agents/nested",
        ".oah/nested",
    ]
    .map(|relative| fixture.home.join(relative));
    for folder in &protected {
        fs::create_dir_all(folder).unwrap();
        fs::write(folder.join("keep.txt"), b"existing bytes").unwrap();
    }
    let entries = |folder: &Path| {
        fs::read_dir(folder)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect::<std::collections::BTreeSet<_>>()
    };
    let before = protected.each_ref().map(|folder| entries(folder));

    let mut install = fixture.request(Engine::Codex, &old, None);
    let mut update = fixture.request(Engine::Codex, &new, Some(&old));
    for folder in &protected {
        let project = fixture.ctx(|ctx| app::project_open(ctx, folder)).unwrap();
        install.project = project.id.clone();
        update.project = project.id;
        let code = if *folder == fixture.home {
            "OAH_SKILL_HOME_SCOPE"
        } else {
            "OAH_SKILL_PATH_UNSAFE"
        };
        fixture.assert_refused(&install, code);
        fixture.assert_refused(&update, code);
    }

    assert_eq!(protected.each_ref().map(|folder| entries(folder)), before);
    for folder in &protected {
        assert_eq!(
            fs::read(folder.join("keep.txt")).unwrap(),
            b"existing bytes"
        );
    }

    let project = fixture
        .ctx(|ctx| app::project_open(ctx, &fixture.project))
        .unwrap();
    install.project = project.id.clone();
    update.project = project.id;
    let destination = fixture.project.join(".agents/skills/tidy");
    assert_eq!(fixture.apply(&install).unwrap().status, Status::Installed);
    assert_installed(&destination, SKILL_A, COMMIT_A);
    assert_eq!(fixture.apply(&update).unwrap().status, Status::Updated);
    assert_installed(&destination, SKILL_B, COMMIT_B);
}

#[test]
fn an_unverified_session_home_leaves_the_project_readable() {
    let mut fixture = Fixture::new();
    let mut env = fixture.session.env().clone();
    let missing_home = fixture.home.join("missing");
    env.set("HOME", &missing_home);
    env.set("USERPROFILE", &missing_home);
    env.set("OAH_HOME", fixture.project.with_file_name("state"));
    fixture.session = Session::with_transport(env, fixture.transport.clone());
    let skill = fixture.project.join(".agents/skills/untracked");
    fs::create_dir_all(&skill).unwrap();
    fs::write(skill.join("SKILL.md"), SKILL_A).unwrap();

    let project = fixture
        .ctx(|ctx| app::project_open(ctx, &fixture.project))
        .unwrap();
    let rows = fixture.inventory();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].name, "untracked");
    assert_eq!(rows[0].scope, SkillScopeDto::Project);
    assert!(rows[0].managed.is_none());

    let review = fixture.review(REPO, &recorded(REPO, COMMIT_A, SKILL_A));
    let mut install = fixture.request(Engine::Codex, &review, None);
    install.project = project.id;
    fixture.assert_refused(&install, "OAH_SKILL_HOME_UNVERIFIED");
    assert_eq!(
        fs::read(skill.join("SKILL.md")).unwrap(),
        SKILL_A.as_bytes()
    );
    assert!(!fixture.project.join(".agents/skills/tidy").exists());
    assert!(!fixture.project.join(".oah").exists());
}

#[cfg(target_os = "macos")]
#[test]
fn refreshing_a_replaced_path_never_adopts_the_replacement_as_its_write_target() {
    let fixture = Fixture::new();
    let review = fixture.review(REPO, &recorded(REPO, COMMIT_A, SKILL_A));
    let moved = fixture.project.with_file_name("original");
    fs::rename(&fixture.project, &moved).unwrap();
    fs::create_dir(&fixture.project).unwrap();
    fixture.ctx(|ctx| {
        app::refresh(ctx, Empty {}).unwrap();
    });

    let req = fixture.request(Engine::Claude, &review, None);
    fixture.assert_refused(&req, "OAH_SKILL_PATH_UNSAFE");
    assert_eq!(fs::read_dir(&fixture.project).unwrap().count(), 0);
    assert_eq!(fs::read_dir(&moved).unwrap().count(), 0);
}
