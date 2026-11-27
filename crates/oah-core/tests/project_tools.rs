// 프로젝트 도구 상태 시험: 빈 폴더·첫 커밋 전 Git·지도 파일·위키 후보·Jev 설정을 읽기만 하고, 프로젝트가 바뀌면 옛 요청을 거절하며, 아무 바이트도 바꾸지 않는다.
use oah_core::app::{self, Session};
use oah_core::dto::{
    Empty, ProjectToolDto, ProjectToolIdDto, ProjectToolsDto, ToolPathBaseDto, WorkflowProjectReq,
};
use oah_core::paths::{self, Env, Project, ProjectKind};
use oah_core::tasks::{CancelToken, NoProgress};
use oah_core::workflow::tools_status_at;
use oah_core::{Coded, Error};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };

/// A temporary base with a home folder, and a session whose environment sees only that base and Git.
struct Fixture {
    _dir: tempfile::TempDir,
    base: PathBuf,
    session: Session,
}

impl Fixture {
    fn new() -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let base = paths::canonical_folder(dir.path()).unwrap();
        fs::create_dir_all(base.join("home")).unwrap();
        let mut env = Env::new(&base).unwrap();
        if let Some(path) = std::env::var_os("PATH") {
            env.set("PATH", path);
        }
        if cfg!(windows) {
            for name in ["SystemRoot", "windir", "ComSpec", "PATHEXT", "TEMP", "TMP"] {
                if let Some(value) = std::env::var_os(name) {
                    env.set(name, value);
                }
            }
        }
        env.set(HOME_VAR, base.join("home"));
        env.set("GIT_CONFIG_NOSYSTEM", "1");
        env.set("GIT_CEILING_DIRECTORIES", &base);
        Fixture {
            _dir: dir,
            base,
            session: Session::new(env),
        }
    }

    /// A project folder below the base with `files`.
    fn folder(&self, name: &str, files: &[(&str, &str)]) -> PathBuf {
        let root = self.base.join(name);
        fs::create_dir_all(&root).unwrap();
        write_files(&root, files);
        root
    }

    fn git(&self, folder: &Path, args: &[&str]) {
        let output = Command::new("git")
            .arg("-C")
            .arg(folder)
            .args([
                "-c",
                "user.name=Example",
                "-c",
                "user.email=example@example.invalid",
            ])
            .args(["-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"])
            .args(args)
            .env_clear()
            .envs(self.session.env().vars())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    fn with_ctx<T>(&self, op: impl FnOnce(&app::Ctx<'_>) -> T) -> T {
        let cancel = CancelToken::new();
        op(&self.session.ctx(&cancel, &NoProgress))
    }

    /// Opens `folder` and returns its project id.
    fn open(&self, folder: &Path) -> String {
        self.with_ctx(|ctx| app::project_open(ctx, folder))
            .unwrap()
            .id
    }

    fn status(&self, project_id: &str) -> Result<ProjectToolsDto, Error> {
        let req = WorkflowProjectReq {
            project_id: project_id.to_string(),
            input: Empty {},
        };
        self.with_ctx(|ctx| app::project_tools_status(ctx, req))
    }

    /// Every entry below the base (the project, its Git folder, the home folder): folders as `None`, files with
    /// their bytes.
    fn tree(&self) -> BTreeMap<PathBuf, Option<Vec<u8>>> {
        let (mut out, mut pending) = (BTreeMap::new(), vec![self.base.clone()]);
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
}

fn write_files(root: &Path, files: &[(&str, &str)]) {
    for (relative, text) in files {
        let path = root.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
}

fn tool(tools: &ProjectToolsDto, id: ProjectToolIdDto) -> &ProjectToolDto {
    tools.tools.iter().find(|tool| tool.id == id).unwrap()
}

/// The cards of a plain folder with `files`, read without a session.
fn plain(files: &[(&str, &str)]) -> (tempfile::TempDir, PathBuf, ProjectToolsDto) {
    let dir = tempfile::tempdir().unwrap();
    let root = paths::canonical_folder(dir.path()).unwrap();
    write_files(&root, files);
    let tools = cards(&root);
    (dir, root, tools)
}

fn cards(root: &Path) -> ProjectToolsDto {
    let project = Project {
        root: root.to_path_buf(),
        kind: ProjectKind::Folder {
            id: String::from("65d80d2c48b3d23b"),
        },
        state_dir: root.join("no-state").join("v1"),
    };
    tools_status_at(&project, "65d80d2c48b3d23b")
}

fn wiki_state(files: &[(&str, &str)]) -> (String, Vec<String>) {
    let (_dir, _root, tools) = plain(files);
    let wiki = tool(&tools, ProjectToolIdDto::Wiki);
    (
        wiki.state.clone(),
        wiki.paths.iter().map(|path| path.path.clone()).collect(),
    )
}

fn jev_state(config: &str) -> String {
    let (_dir, _root, tools) = plain(&[(".oah/config.toml", config)]);
    tool(&tools, ProjectToolIdDto::Jev).state.clone()
}

#[test]
fn an_empty_plain_folder_is_diagnosed_and_nothing_is_created() {
    let fixture = Fixture::new();
    let root = fixture.folder("empty", &[]);
    let before = fixture.tree();
    let id = fixture.open(&root);
    let tools = fixture.status(&id).unwrap();

    assert_eq!(tools.project_id, id);
    let ids: Vec<_> = tools.tools.iter().map(|tool| tool.id).collect();
    assert_eq!(
        ids,
        [
            ProjectToolIdDto::Wiki,
            ProjectToolIdDto::ImpactMap,
            ProjectToolIdDto::Jev
        ]
    );
    assert_eq!(tool(&tools, ProjectToolIdDto::Wiki).state, "none");
    let impact = tool(&tools, ProjectToolIdDto::ImpactMap);
    assert_eq!(impact.state, "no-git");
    assert!(impact.status.contains("empty"), "{}", impact.status);
    assert_eq!(tool(&tools, ProjectToolIdDto::Jev).state, "off");
    assert_eq!(fixture.tree(), before, "reading the status wrote something");
}

#[test]
fn a_git_project_without_rules_files_and_with_a_wiki_changes_no_bytes() {
    let fixture = Fixture::new();
    let root = fixture.folder(
        "repo",
        &[
            ("docs/wiki/index.md", "# Wiki\n"),
            ("src/main.rs", "fn main() {}\n"),
        ],
    );
    fixture.git(&root, &["init", "-q"]);
    let before = fixture.tree();

    let id = fixture.open(&root);
    let tools = fixture.status(&id).unwrap();

    assert_eq!(
        fixture.tree(),
        before,
        "opening and reading the status changed the project"
    );
    assert!(!root.join("AGENTS.md").exists() && !root.join("CLAUDE.md").exists());
    assert!(!root.join(".oah").exists(), "no settings folder is created");
    let wiki = tool(&tools, ProjectToolIdDto::Wiki);
    assert_eq!(wiki.state, "found");
    assert_eq!(wiki.paths[0].path, "docs/wiki");
    assert!(wiki.notes[0].contains("log.md") && !wiki.notes[0].contains("index.md"));
    assert_eq!(tool(&tools, ProjectToolIdDto::ImpactMap).state, "no-commit");
    assert_eq!(tool(&tools, ProjectToolIdDto::Jev).state, "off");
}

#[test]
fn a_committed_repository_is_ready_and_an_existing_map_file_is_named_without_a_full_path() {
    use sha2::{Digest, Sha256};

    let fixture = Fixture::new();
    let root = fixture.folder("repo", &[("src/main.rs", "fn main() {}\n")]);
    fixture.git(&root, &["init", "-q"]);
    fixture.git(&root, &["add", "."]);
    let id = fixture.open(&root);
    assert_eq!(
        tool(&fixture.status(&id).unwrap(), ProjectToolIdDto::ImpactMap).state,
        "no-commit",
        "staged files are not a commit"
    );
    fixture.git(&root, &["commit", "-q", "-m", "first"]);
    let before = fixture.tree();
    assert_eq!(
        tool(&fixture.status(&id).unwrap(), ProjectToolIdDto::ImpactMap).state,
        "ready"
    );
    fixture.git(&root, &["pack-refs", "--all"]);
    assert_eq!(
        tool(&fixture.status(&id).unwrap(), ProjectToolIdDto::ImpactMap).state,
        "ready",
        "a packed branch is still a commit"
    );

    // Where the source CLI stores this checkout's map: <common dir>/oah/impact/maps/<sha256 of the root>.json.
    let opened = fixture.session.project().unwrap();
    let key: String = Sha256::digest(opened.project.root.to_str().unwrap().as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let maps = root.join(".git").join("oah").join("impact").join("maps");
    fs::create_dir_all(&maps).unwrap();
    fs::write(maps.join(format!("{key}.json")), "{}").unwrap();
    fs::create_dir_all(root.join("graphify-out")).unwrap();
    fs::write(root.join("graphify-out/graph.json"), "{}").unwrap();

    let tools = fixture.status(&id).unwrap();
    let impact = tool(&tools, ProjectToolIdDto::ImpactMap);
    assert_eq!(impact.state, "map-found");
    assert!(
        impact
            .status
            .contains("has not checked whether it is current")
    );
    assert_eq!(impact.paths[0].base, ToolPathBaseDto::GitCommonDir);
    assert_eq!(impact.paths[0].path, format!("oah/impact/maps/{key}.json"));
    assert!(
        impact
            .notes
            .iter()
            .any(|note| note.contains("has not verified it"))
    );
    let json = serde_json::to_string(&tools).unwrap();
    for whole in [&fixture.base, &root] {
        let whole = serde_json::to_string(&whole.display().to_string()).unwrap();
        assert!(
            !json.contains(whole.trim_matches('"')),
            "a full path is in {json}"
        );
    }
    drop(before);
}

#[test]
fn a_request_for_the_previous_project_is_refused_after_a_swap() {
    let fixture = Fixture::new();
    let (a, b) = (
        fixture.folder("a", &[("docs/wiki/index.md", "a\n")]),
        fixture.folder("b", &[]),
    );
    assert_eq!(
        fixture.status("anything").unwrap_err().code(),
        "OAH_PROJECT_NOT_OPEN"
    );
    let first = fixture.open(&a);
    assert_eq!(
        tool(&fixture.status(&first).unwrap(), ProjectToolIdDto::Wiki).state,
        "found"
    );

    let second = fixture.open(&b);
    assert_ne!(first, second);
    assert_eq!(
        fixture.status(&first).unwrap_err().code(),
        "OAH_PROJECT_CHANGED"
    );
    let tools = fixture.status(&second).unwrap();
    assert_eq!(tools.project_id, second);
    assert_eq!(tool(&tools, ProjectToolIdDto::Wiki).state, "none");
}

#[test]
fn wiki_roots_are_found_detected_ambiguous_or_refused() {
    assert_eq!(wiki_state(&[]), (String::from("none"), vec![]));
    assert_eq!(
        wiki_state(&[("docs/wiki/index.md", "x")]),
        (String::from("found"), vec![String::from("docs/wiki")])
    );
    assert_eq!(
        wiki_state(&[("knowledge/wiki/index.md", "x")]),
        (
            String::from("detected"),
            vec![String::from("knowledge/wiki")]
        )
    );
    assert_eq!(
        wiki_state(&[("docs/wiki/index.md", "x"), ("wiki/index.md", "x")]),
        (
            String::from("ambiguous"),
            vec![String::from("docs/wiki"), String::from("wiki")]
        )
    );
    // An Obsidian vault in a top-level folder is a candidate; a marker above a standard folder is that same wiki.
    assert_eq!(
        wiki_state(&[("notes/.obsidian/app.json", "{}"), ("notes/a.md", "x")]),
        (String::from("detected"), vec![String::from("notes")])
    );
    assert_eq!(
        wiki_state(&[
            (".obsidian/app.json", "{}"),
            ("knowledge/wiki/index.md", "x")
        ]),
        (
            String::from("detected"),
            vec![String::from("knowledge/wiki")]
        )
    );
    assert_eq!(
        wiki_state(&[
            ("notes/.obsidian/app.json", "{}"),
            ("docs/wiki/index.md", "x")
        ]),
        (
            String::from("ambiguous"),
            vec![String::from("docs/wiki"), String::from("notes")]
        )
    );
}

#[test]
fn a_configured_wiki_folder_is_used_as_set_and_its_problems_are_told_apart() {
    let set = |dir: &str| format!("[wiki]\ndir = {dir}\n");
    let state = |config: String, files: &[(&str, &str)]| {
        let mut all = vec![(".oah/config.toml", config.as_str())];
        all.extend_from_slice(files);
        wiki_state(&all)
    };
    assert_eq!(
        state(
            set("\"notes\""),
            &[("notes/a.md", "x"), ("docs/wiki/index.md", "x")]
        ),
        (String::from("found"), vec![String::from("notes")]),
        "a set folder wins over the standard places"
    );
    assert_eq!(
        state(set("\"notes\""), &[("docs/wiki/index.md", "x")]),
        (
            String::from("configured-missing"),
            vec![String::from("notes")]
        )
    );
    assert_eq!(state(set("\"../outside\""), &[]).0, "invalid");
    assert_eq!(state(set("7"), &[]).0, "invalid");
    assert_eq!(state(String::from("wiki = 1\n"), &[]).0, "invalid");
    assert_eq!(
        state(set("\"notes\""), &[("notes", "a file")]).0,
        "not-folder"
    );
    assert_eq!(
        state(String::from("[wiki\n"), &[("docs/wiki/index.md", "x")]).0,
        "settings-unreadable"
    );
}

#[cfg(unix)]
#[test]
fn a_linked_or_unreadable_wiki_folder_is_refused_with_its_own_state() {
    use std::os::unix::fs::{PermissionsExt, symlink};

    let dir = tempfile::tempdir().unwrap();
    let root = paths::canonical_folder(dir.path()).unwrap();
    write_files(&root, &[("elsewhere/index.md", "x")]);
    fs::create_dir_all(root.join("docs")).unwrap();
    symlink(root.join("elsewhere"), root.join("docs/wiki")).unwrap();
    let wiki = tool(&cards(&root), ProjectToolIdDto::Wiki).clone();
    assert_eq!(wiki.state, "link");
    assert!(
        !serde_json::to_string(&wiki).unwrap().contains("elsewhere"),
        "the link target is not shown"
    );

    fs::remove_file(root.join("docs/wiki")).unwrap();
    write_files(&root, &[("docs/wiki/index.md", "x")]);
    let locked = root.join("docs/wiki");
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
    // A privileged test user can read everything; then there is no refusal to observe.
    let enforced = fs::read_dir(&locked).is_err();
    let state = tool(&cards(&root), ProjectToolIdDto::Wiki).state.clone();
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
    if enforced {
        assert_eq!(state, "permission-denied");
    }
}

#[test]
fn jev_shows_the_effective_project_setting_only() {
    assert_eq!(jev_state(""), "off");
    assert_eq!(jev_state("[rerank]\nprovider = \"typesafe\"\n"), "off");
    assert_eq!(
        jev_state("[rerank]\nenabled = false\nprovider = \"typesafe\"\n"),
        "off"
    );
    assert_eq!(jev_state("[rerank]\nenabled = true\n"), "no-provider");
    assert_eq!(
        jev_state("[rerank]\nenabled = true\nprovider = \"other\"\n"),
        "unknown-provider"
    );
    assert_eq!(jev_state("[rerank]\nenabled = \"yes\"\n"), "invalid");
    assert_eq!(jev_state("rerank = true\n"), "invalid");
    assert_eq!(jev_state("[rerank\n"), "settings-unreadable");

    let on = "[rerank]\nenabled = true\nprovider = \"typesafe\"\n\n[impact]\nrerank = true\n";
    let (_dir, _root, tools) = plain(&[(".oah/config.toml", on)]);
    let jev = tool(&tools, ProjectToolIdDto::Jev);
    assert_eq!(jev.state, "on");
    assert!(jev.notes[0].contains("did not look for a key"));
    assert!(jev.notes[1].contains("switched on"));
    // An unreadable settings file leaves the Impact Map card, which does not depend on it, as it is.
    let (_dir, _root, broken) = plain(&[(".oah/config.toml", "[rerank\n")]);
    assert_eq!(tool(&broken, ProjectToolIdDto::ImpactMap).state, "no-git");
    assert!(
        tool(&broken, ProjectToolIdDto::Jev)
            .reason
            .as_deref()
            .unwrap()
            .contains("OAH_CONFIG")
    );
}

#[test]
fn the_shared_sample_is_what_a_set_up_folder_returns() {
    let config =
        "[wiki]\ndir = \"docs/wiki\"\n\n[rerank]\nenabled = true\nprovider = \"typesafe\"\n";
    let (_dir, _root, tools) = plain(&[
        (".oah/config.toml", config),
        ("docs/wiki/index.md", "# Wiki\n"),
        ("docs/wiki/log.md", "# Log\n"),
        ("src/main.rs", "fn main() {}\n"),
    ]);
    let text = serde_json::to_string_pretty(&tools).unwrap() + "\n";
    let sample = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/dto_samples/workflow/project_tools_status.json");
    if std::env::var_os("OAH_WRITE_DTO_SAMPLES").is_some() {
        fs::write(&sample, &text).unwrap();
    }
    assert_eq!(fs::read_to_string(&sample).unwrap(), text);
}
