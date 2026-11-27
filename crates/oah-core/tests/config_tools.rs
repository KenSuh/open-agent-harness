// 프로젝트 도구 설정 시험: Python 과 공유하는 표본으로 읽기를 맞추고, 계획·적용의 결속과 거부·복구 경로가 파일을 지키는지 확인한다.
// The save itself is bound to open folder handles, which only macOS and Windows offer here; elsewhere it refuses.
#![cfg_attr(
    not(any(target_os = "macos", windows)),
    allow(dead_code, unused_imports)
)]
use oah_core::config::{
    Backup, BackupPlan, ConfigFile, Format, KeptReason, ProjectConfig, ProjectTools, Setting,
    TOOL_KEYS, ToolsApproval, ToolsBinding, ToolsChange, ToolsPlan, Value, apply_tools_save,
    backup_path, codes, notice_codes, plan_tools_save,
};
use oah_core::fsx::{FileLock, Fingerprint, LockOptions};
use oah_core::paths;
use serde_json::{Value as Json, json};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

const SAMPLES: &str = include_str!("config_samples/project_tools.json");

fn setting<T: serde::Serialize>(setting: &Setting<T>) -> Json {
    json!({"value": setting.value, "explicit": setting.explicit})
}

/// The plain form the shared samples give; the Python reader's `ProjectTools.view()` builds the same one.
fn view(tools: &ProjectTools) -> Json {
    let mut notices: Vec<(String, String)> = tools
        .notices
        .iter()
        .flat_map(|notice| {
            notice
                .keys()
                .iter()
                .map(|key| (notice.code().to_string(), key.clone()))
        })
        .collect();
    notices.sort();
    json!({
        "wiki": {"dir": setting(&tools.wiki.dir)},
        "impact": {
            "wiki_dirs": setting(&tools.impact.wiki_dirs),
            "graph_file": setting(&tools.impact.graph_file),
            "rerank": setting(&tools.impact.rerank),
        },
        "jev": {
            "enabled": setting(&tools.jev.enabled),
            "provider": setting(&tools.jev.provider),
        },
        "managed": {
            "wiki_index": {
                "managed": setting(&tools.managed.wiki_index.managed),
                "exclude": setting(&tools.managed.wiki_index.exclude),
                "embedding_model": setting(&tools.managed.wiki_index.embedding_model),
            },
            "graphify": {"managed": setting(&tools.managed.graphify.managed)},
        },
        "notices": notices.iter().map(|(code, key)| json!([code, key])).collect::<Vec<_>>(),
    })
}

#[test]
fn every_shared_sample_reads_as_the_contract_says() {
    let samples: Json = serde_json::from_str(SAMPLES).unwrap();
    assert_eq!(samples["schema"], 1);
    let cases = samples["cases"].as_array().unwrap();
    let (mut read, mut refused) = (0, 0);
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let bytes = case["toml"].as_str().map(|text| text.as_bytes().to_vec());
        let file = ConfigFile::from_bytes(Path::new(".oah/config.toml"), bytes).unwrap();
        match (case.get("error"), ProjectTools::read(&file)) {
            (Some(code), Err(error)) => {
                assert_eq!(error.code(), code.as_str().unwrap(), "{name}");
                refused += 1;
            }
            (None, Ok(tools)) => {
                assert_eq!(view(&tools), case["expect"], "{name}");
                read += 1;
            }
            (expected, got) => panic!("{name}: expected error {expected:?}, got {got:?}"),
        }
    }
    assert_eq!((read, refused), (20, 19), "the sample counts changed");
}

#[test]
fn a_broken_tools_table_does_not_stop_the_stages_from_being_read() {
    let text = "[stages]\nplan = \"codex\"\n[wiki]\ndir = 5\n";
    let file = ConfigFile::from_bytes(Path::new("c.toml"), Some(text.as_bytes().to_vec())).unwrap();
    let config = ProjectConfig::from_file(file).unwrap();
    assert_eq!(
        config.effective().agent_for("plan").unwrap().as_str(),
        "codex"
    );
    assert_eq!(config.tools().unwrap_err().code(), codes::INVALID);
}

struct Project {
    dir: tempfile::TempDir,
    root: PathBuf,
}

impl Project {
    fn empty() -> Project {
        let dir = tempfile::tempdir().unwrap();
        let root = paths::canonical_folder(dir.path()).unwrap();
        Project { dir, root }
    }

    fn with_bytes(bytes: &[u8]) -> Project {
        let project = Project::empty();
        fs::create_dir_all(project.root.join(".oah")).unwrap();
        fs::write(project.config(), bytes).unwrap();
        project
    }

    fn with(text: &str) -> Project {
        Project::with_bytes(text.as_bytes())
    }

    fn config(&self) -> PathBuf {
        self.root.join(".oah").join("config.toml")
    }

    fn id(&self) -> String {
        paths::folder_id(&self.root)
    }

    fn binding(&self) -> ToolsBinding {
        ToolsBinding::new(self.id(), self.root.clone())
    }

    fn bytes(&self) -> Vec<u8> {
        fs::read(self.config()).unwrap()
    }

    fn text(&self) -> String {
        String::from_utf8(self.bytes()).unwrap()
    }

    fn plan(&self, changes: &[ToolsChange]) -> ToolsPlan {
        plan_tools_save(&self.binding(), changes).unwrap()
    }

    fn tools(&self) -> ProjectTools {
        ProjectConfig::read(&self.config())
            .unwrap()
            .tools()
            .unwrap()
    }

    /// Every entry below the root with its bytes (folders and links by kind), sorted: what "nothing changed" means.
    fn tree(&self) -> Vec<(String, String)> {
        fn walk(root: &Path, folder: &Path, out: &mut Vec<(String, String)>) {
            for entry in fs::read_dir(folder).unwrap() {
                let path = entry.unwrap().path();
                let name = path
                    .strip_prefix(root)
                    .unwrap()
                    .to_string_lossy()
                    .replace('\\', "/");
                let metadata = fs::symlink_metadata(&path).unwrap();
                if metadata.file_type().is_symlink() {
                    out.push((name, String::from("<link>")));
                } else if metadata.is_dir() {
                    out.push((name, String::from("<dir>")));
                    walk(root, &path, out);
                } else {
                    out.push((name, Fingerprint::of(&fs::read(&path).unwrap()).to_hex()));
                }
            }
        }
        let mut out = Vec::new();
        walk(&self.root, &self.root, &mut out);
        out.sort();
        out
    }
}

fn quick() -> LockOptions {
    LockOptions {
        wait: Duration::from_millis(200),
        poll: Duration::from_millis(10),
    }
}

fn wiki(dir: &str) -> Vec<ToolsChange> {
    vec![ToolsChange::WikiDir(dir.to_string())]
}

fn apply(
    project: &Project,
    changes: &[ToolsChange],
    approval: &ToolsApproval,
) -> Result<oah_core::config::SaveOutcome, oah_core::config::ConfigError> {
    apply_tools_save(&project.binding(), changes, approval, quick())
}

const V04: &str = "# written by oah init\n[stages]\ninitial = \"claude\"\nreview = \"codex\"\ncorrection = \"codex\"\n\n\
                   [engines.claude]\ncommand = [\"claude\"]\n\n# Jev: you consented on 2026-05-01\n[rerank]\nenabled = true\n\
                   provider = \"typesafe\"\n\n[impact]\nrerank = true\n";

#[test]
fn a_plan_reads_only_and_names_what_would_change() {
    let project = Project::empty();
    let before = project.tree();
    let plan = project.plan(&wiki("knowledge/wiki"));
    assert_eq!(
        project.tree(),
        before,
        "planning created or changed something"
    );
    assert_eq!(plan.project_id(), project.id());
    assert_eq!(plan.root(), project.root);
    assert_eq!(plan.config_path(), project.config());
    assert_eq!(plan.before(), None);
    assert_eq!(plan.id().len(), 64);
    let changed: Vec<(&str, Option<String>)> = plan
        .changes()
        .iter()
        .map(|change| {
            (
                change.key.as_str(),
                change.after.as_ref().map(Value::to_toml),
            )
        })
        .collect();
    assert_eq!(
        changed,
        [("wiki.dir", Some(String::from("\"knowledge/wiki\"")))]
    );
    assert!(plan.changes().iter().all(|change| change.before.is_none()));
    assert_eq!(plan.tools().wiki.dir.value, "docs/wiki");
    assert_eq!(plan.tools_after().wiki.dir.value, "knowledge/wiki");
    // The map follows by reading the selected folder; no key is written for it.
    assert_eq!(
        plan.tools_after().impact.wiki_dirs,
        Setting {
            value: vec![String::from("knowledge/wiki")],
            explicit: false
        }
    );
    assert!(plan.upgrade_warning().is_none() && plan.backup().is_none());
    // The same project, file and change give the same plan; another change gives another one.
    assert_eq!(project.plan(&wiki("knowledge/wiki")).id(), plan.id());
    assert_ne!(project.plan(&wiki("notes")).id(), plan.id());
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn an_approved_plan_creates_the_file_and_a_second_run_changes_nothing() {
    let project = Project::empty();
    let changes = vec![
        ToolsChange::WikiDir("knowledge/wiki".into()),
        ToolsChange::ImpactGraphFile("graphs/code.json".into()),
        ToolsChange::JevEnabled(true),
        ToolsChange::WikiIndexManaged(true),
        ToolsChange::WikiIndexExclude(vec!["raw/**".into()]),
        ToolsChange::GraphifyManaged(true),
    ];
    let plan = project.plan(&changes);
    let outcome = apply(&project, &changes, &plan.approve(false)).unwrap();
    let receipt = outcome.written.expect("written");
    assert_eq!(receipt.path(), project.config());
    assert!(!outcome.upgraded && outcome.backup.is_none());
    assert_eq!(Some(project.bytes().as_slice()), plan.after_bytes());
    let mut names: Vec<String> = fs::read_dir(project.root.join(".oah"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    assert_eq!(names, ["config.toml"], "a lock or temporary file was left");
    let tools = project.tools();
    assert_eq!(&tools, plan.tools_after());
    assert_eq!(
        tools.wiki.dir,
        Setting {
            value: String::from("knowledge/wiki"),
            explicit: true
        }
    );
    assert_eq!(tools.impact.graph_file.value, "graphs/code.json");
    assert_eq!(
        (tools.jev.enabled.value, tools.jev.provider.value.as_str()),
        (true, "typesafe")
    );
    assert!(
        !tools.impact.rerank.explicit,
        "Jev on must not turn the map's re-ranking on"
    );
    assert!(tools.managed.wiki_index.managed.value && tools.managed.graphify.managed.value);
    assert!(tools.notices.is_empty());
    assert_eq!(
        ProjectConfig::read(&project.config()).unwrap().format(),
        Format::Current
    );
    // Same input again: nothing to write, the file keeps its bytes.
    let before = project.tree();
    let again = project.plan(&changes);
    assert!(again.after_bytes().is_none() && again.changes().is_empty());
    let outcome = apply(&project, &changes, &again.approve(false)).unwrap();
    assert!(outcome.written.is_none());
    assert_eq!(project.tree(), before);
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn a_save_keeps_comments_unknown_keys_the_bom_and_crlf() {
    let text = "\u{feff}# team notes\r\n[stages]\r\nplan = \"claude\"   # lead\r\nbuild = \"claude\"\r\n\r\n[wiki]\r\n\
                # where notes live\r\ndir = \"docs/wiki\"  # default\r\nstale_days = 90\r\n\r\n[custom]\r\nkeep = [1, 2]\r\n\r\n\
                [rerank]\r\nenabled = true\r\nprovider = \"typesafe\"\r\n";
    let project = Project::with(text);
    let changes = wiki("knowledge/wiki");
    let plan = project.plan(&changes);
    apply(&project, &changes, &plan.approve(false)).unwrap();
    let after = project.text();
    let expected = text.replace(
        "dir = \"docs/wiki\"  # default",
        "dir = \"knowledge/wiki\"  # default",
    );
    assert_eq!(after, expected);
    assert!(
        !after.replace("\r\n", "").contains('\n'),
        "a bare LF was written"
    );
    let tools = project.tools();
    assert!(tools.jev.enabled.value, "the existing opt-in was changed");
    assert_eq!(tools.impact.wiki_dirs.value, ["knowledge/wiki"]);
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn an_explicit_list_of_wiki_folders_is_kept_and_reported() {
    let text = "[sections]\norder = [\"plan\", \"build\", \"review\", \"fix\"]\n[impact]\nwiki_dirs = [\"docs/wiki\", \"adr\"]\n";
    let project = Project::with(text);
    let changes = wiki("notes");
    let plan = project.plan(&changes);
    assert_eq!(plan.kept().len(), 1);
    assert_eq!(plan.kept()[0].key, "impact.wiki_dirs");
    assert_eq!(plan.kept()[0].reason, KeptReason::ExplicitWikiDirs);
    assert_eq!(plan.kept()[0].reason.as_str(), "explicit-wiki-dirs");
    apply(&project, &changes, &plan.approve(false)).unwrap();
    assert_eq!(project.text(), format!("{text}\n[wiki]\ndir = \"notes\"\n"));
    assert_eq!(project.tools().impact.wiki_dirs.value, ["docs/wiki", "adr"]);
    // The override is removed on request, which puts the map back on the selected wiki folder.
    let reset = vec![ToolsChange::ImpactWikiDirs(None)];
    let plan = project.plan(&reset);
    apply(&project, &reset, &plan.approve(false)).unwrap();
    assert!(!project.text().contains("wiki_dirs"), "{}", project.text());
    let tools = project.tools();
    assert_eq!(
        tools.impact.wiki_dirs,
        Setting {
            value: vec![String::from("notes")],
            explicit: false
        }
    );
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn a_plan_for_other_bytes_another_change_or_another_project_is_stale() {
    let project = Project::with("[stages]\nplan = \"claude\"\n");
    let changes = wiki("notes");
    let approval = project.plan(&changes).approve(false);
    // 1. An editor saved the file after the preview.
    fs::write(project.config(), "[stages]\nplan = \"codex\"\n").unwrap();
    let before = project.tree();
    let error = apply(&project, &changes, &approval).unwrap_err();
    assert_eq!(error.code(), codes::PLAN_STALE);
    assert_eq!(error.code(), "OAH_PLAN_STALE");
    assert_eq!(project.tree(), before);
    // 2. The approved plan cannot carry another change.
    let approval = project.plan(&changes).approve(false);
    let error = apply(&project, &wiki("elsewhere"), &approval).unwrap_err();
    assert_eq!(error.code(), codes::PLAN_STALE);
    assert_eq!(project.tree(), before);
    // 3. Nor be used in another project, even one with the same bytes.
    let other = Project::with("[stages]\nplan = \"codex\"\n");
    let other_before = other.tree();
    let error = apply(&other, &changes, &approval).unwrap_err();
    assert_eq!(error.code(), codes::PLAN_STALE);
    assert_eq!(other.tree(), other_before);
    // 4. A root that is not the project the caller holds is refused before any plan is made.
    let wrong = ToolsBinding::new(project.id(), other.root.clone());
    let error = plan_tools_save(&wrong, &changes).unwrap_err();
    assert_eq!(error.code(), codes::PROJECT_CHANGED);
    assert_eq!(error.code(), "OAH_PROJECT_CHANGED");
    let error = apply_tools_save(&wrong, &changes, &approval, quick()).unwrap_err();
    assert_eq!(error.code(), codes::PROJECT_CHANGED);
    assert_eq!(other.tree(), other_before);
    let gone = ToolsBinding::new(project.id(), project.root.join("missing"));
    assert_eq!(
        plan_tools_save(&gone, &changes).unwrap_err().code(),
        codes::PROJECT_CHANGED
    );
    // The fresh plan still works.
    apply(&project, &changes, &approval).unwrap();
    assert_eq!(project.tools().wiki.dir.value, "notes");
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn a_file_oah_cannot_read_is_never_edited() {
    for broken in [
        "[wiki\ndir = \n",
        "[wiki]\ndir = 5\n",
        "[impact]\nwiki_dirs = \"docs/wiki\"\n",
        "[rerank]\nenabled = \"yes\"\n",
        "[project_tools]\nwiki_index = 1\n",
        "[stages]\nplan = \"robot\"\n",
    ] {
        let project = Project::with(broken);
        let before = project.tree();
        let error = plan_tools_save(&project.binding(), &wiki("notes")).unwrap_err();
        assert_eq!(error.code(), codes::INVALID, "{broken:?}");
        let error = apply(
            &project,
            &wiki("notes"),
            &ToolsApproval::new("0".repeat(64), None),
        )
        .unwrap_err();
        assert_eq!(error.code(), codes::INVALID, "{broken:?}");
        assert_eq!(project.tree(), before, "{broken:?}");
    }
    let project = Project::with_bytes(b"[wiki]\ndir = \"\xc4\xda\"\n");
    let before = project.tree();
    assert_eq!(
        plan_tools_save(&project.binding(), &wiki("notes"))
            .unwrap_err()
            .code(),
        codes::INVALID
    );
    assert_eq!(project.tree(), before);
    // A key written in a layout the byte-preserving patch cannot edit is refused, not rewritten.
    let project = Project::with("wiki.stale_days = 30\n[sections]\norder = [\"plan\"]\n");
    let before = project.tree();
    let error = plan_tools_save(&project.binding(), &wiki("notes"))
        .map(|plan| plan.id().to_string())
        .unwrap_err();
    assert_eq!(error.code(), codes::LAYOUT_UNSUPPORTED);
    assert_eq!(project.tree(), before);
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn a_v04_file_needs_the_approval_of_its_exact_bytes_and_keeps_a_backup() {
    let project = Project::with(V04);
    let changes = wiki("knowledge/wiki");
    let plan = project.plan(&changes);
    let warning = plan.upgrade_warning().expect("an upgrade warning");
    assert!(warning.message().contains(".oah/config.toml.v0.4.bak"));
    assert_eq!(
        plan.backup(),
        Some(&BackupPlan::Write(backup_path(&project.config())))
    );
    // Without the approval, and with one for other bytes: refused, nothing written.
    let before = project.tree();
    for approval in [
        plan.approve(false),
        ToolsApproval::new(plan.id(), Some(Fingerprint::of(b"other bytes"))),
    ] {
        let error = apply(&project, &changes, &approval).unwrap_err();
        assert_eq!(error.code(), codes::UPGRADE_REQUIRED);
        assert_eq!(project.tree(), before);
    }
    let approval = plan.approve(true);
    assert_eq!(approval.upgrade(), Some(Fingerprint::of(V04.as_bytes())));
    assert_eq!(approval.plan_id(), plan.id());
    let outcome = apply(&project, &changes, &approval).unwrap();
    assert!(outcome.upgraded);
    assert!(matches!(outcome.backup, Some(Backup::Written(_))));
    assert_eq!(
        fs::read(backup_path(&project.config())).unwrap(),
        V04.as_bytes()
    );
    let after = ProjectConfig::read(&project.config()).unwrap();
    assert_eq!(after.format(), Format::Current);
    let text = project.text();
    assert!(
        text.contains("# written by oah init")
            && text.contains("# Jev: you consented on 2026-05-01")
    );
    assert!(
        text.contains(
            "plan = \"claude\"\nbuild = \"claude\"\nreview = \"codex\"\nfix = \"codex\"\n"
        ),
        "{text}"
    );
    let tools = after.tools().unwrap();
    assert_eq!(tools.wiki.dir.value, "knowledge/wiki");
    assert_eq!(tools.impact.wiki_dirs.value, ["knowledge/wiki"]);
    // The opt-in the 0.4 CLI recorded is exactly as it was.
    assert_eq!(tools.jev, plan.tools().jev);
    assert_eq!(
        tools.impact.rerank,
        Setting {
            value: true,
            explicit: true
        }
    );
    // A later change is an ordinary save: no upgrade, the backup stays.
    let more = vec![ToolsChange::ImpactRerank(false)];
    let plan = project.plan(&more);
    assert!(plan.upgrade_warning().is_none() && plan.backup().is_none());
    let outcome = apply(&project, &more, &plan.approve(false)).unwrap();
    assert!(!outcome.upgraded && outcome.backup.is_none());
    assert_eq!(
        fs::read(backup_path(&project.config())).unwrap(),
        V04.as_bytes()
    );
    assert!(project.tools().jev.enabled.value);
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn an_existing_backup_is_reused_only_when_identical() {
    let changes = wiki("notes");
    let conflict = Project::with(V04);
    fs::write(backup_path(&conflict.config()), b"another file").unwrap();
    let plan = conflict.plan(&changes);
    assert!(matches!(plan.backup(), Some(BackupPlan::Conflict(_))));
    let before = conflict.tree();
    let error = apply(&conflict, &changes, &plan.approve(true)).unwrap_err();
    assert_eq!(error.code(), codes::BACKUP_CONFLICT);
    assert_eq!(conflict.tree(), before);

    let same = Project::with(V04);
    fs::write(backup_path(&same.config()), V04).unwrap();
    let plan = same.plan(&changes);
    assert!(matches!(plan.backup(), Some(BackupPlan::Reuse(_))));
    let outcome = apply(&same, &changes, &plan.approve(true)).unwrap();
    assert!(matches!(outcome.backup, Some(Backup::Reused(_))));
    assert_eq!(
        fs::read(backup_path(&same.config())).unwrap(),
        V04.as_bytes()
    );
    assert_eq!(same.tools().wiki.dir.value, "notes");
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn a_held_lock_refuses_and_two_savers_never_both_win() {
    let project = Project::with("[stages]\nplan = \"claude\"\n");
    let changes = wiki("notes");
    let plan = project.plan(&changes);
    let before = project.tree();
    let held = FileLock::acquire(&project.config(), quick()).unwrap();
    let error = apply(&project, &changes, &plan.approve(false)).unwrap_err();
    assert_eq!(error.code(), "OAH_FILE_BUSY");
    held.release().unwrap();
    assert_eq!(project.tree(), before);

    // Both plans were made from the same bytes. Whatever the timing, one save lands and the other is refused.
    let (first, second) = (wiki("first"), wiki("second"));
    let approvals = [
        project.plan(&first).approve(false),
        project.plan(&second).approve(false),
    ];
    let binding = project.binding();
    let barrier = std::sync::Barrier::new(2);
    let results: Vec<_> = std::thread::scope(|scope| {
        let handles: Vec<_> = [(&first, &approvals[0]), (&second, &approvals[1])]
            .into_iter()
            .map(|(changes, approval)| {
                let (binding, barrier) = (&binding, &barrier);
                scope.spawn(move || {
                    barrier.wait();
                    apply_tools_save(binding, changes, approval, LockOptions::default())
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect()
    });
    let won: Vec<usize> = (0..2).filter(|index| results[*index].is_ok()).collect();
    assert_eq!(won.len(), 1, "{results:?}");
    let lost = results[1 - won[0]].as_ref().unwrap_err();
    assert!(
        [codes::PLAN_STALE, codes::CHANGED].contains(&lost.code()),
        "{lost}"
    );
    assert_eq!(project.tools().wiki.dir.value, ["first", "second"][won[0]]);
}

#[test]
fn a_non_regular_config_folder_or_file_is_refused() {
    let changes = wiki("notes");
    let file_dir = Project::empty();
    fs::write(file_dir.root.join(".oah"), b"not a folder").unwrap();
    let before = file_dir.tree();
    let error = plan_tools_save(&file_dir.binding(), &changes).unwrap_err();
    assert_eq!(error.code(), "OAH_FILE_IO");
    assert!(
        error.message().contains(".oah is not a regular folder"),
        "{error}"
    );
    assert_eq!(file_dir.tree(), before);

    let dir_file = Project::empty();
    fs::create_dir_all(dir_file.config()).unwrap();
    let before = dir_file.tree();
    let error = plan_tools_save(&dir_file.binding(), &changes).unwrap_err();
    assert_eq!(error.code(), "OAH_FILE_IO");
    assert!(
        error
            .message()
            .contains(".oah/config.toml is not a regular file"),
        "{error}"
    );
    assert_eq!(dir_file.tree(), before);

    let dir_lock = Project::with("[sections]\norder = [\"plan\"]\n");
    fs::create_dir(dir_lock.root.join(".oah/config.toml.lock")).unwrap();
    let before = dir_lock.tree();
    let error = plan_tools_save(&dir_lock.binding(), &changes).unwrap_err();
    assert_eq!(error.code(), "OAH_FILE_IO");
    assert!(error.message().contains(".oah/config.toml.lock"), "{error}");
    assert_eq!(dir_lock.tree(), before);
}

#[cfg(target_os = "macos")]
#[test]
fn a_link_on_the_way_never_lets_a_save_leave_the_project() {
    use std::os::unix::fs::symlink;
    let changes = wiki("notes");
    let outside = tempfile::tempdir().unwrap();
    let outside_file = outside.path().join("config.toml");
    fs::write(&outside_file, "[sections]\norder = [\"plan\"]\n").unwrap();
    let outside_before = fs::read(&outside_file).unwrap();
    let refused = |project: &Project, label: &str| {
        let before = project.tree();
        let error = plan_tools_save(&project.binding(), &changes).unwrap_err();
        assert_eq!(error.code(), "OAH_FILE_IO", "{label}: {error}");
        assert!(
            error.message().contains("is a link or reparse entry"),
            "{label}: {error}"
        );
        assert!(
            !error
                .message()
                .contains(&outside.path().display().to_string()),
            "the target leaked"
        );
        let approval = ToolsApproval::new("0".repeat(64), None);
        let error = apply(project, &changes, &approval).unwrap_err();
        assert_eq!(error.code(), "OAH_FILE_IO", "{label}: {error}");
        assert_eq!(project.tree(), before, "{label}");
        assert_eq!(fs::read(&outside_file).unwrap(), outside_before, "{label}");
        let mut names: Vec<_> = fs::read_dir(outside.path())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        names.sort();
        assert_eq!(
            names,
            ["config.toml"],
            "{label}: something was written outside"
        );
    };

    // 1. .oah itself is a link to a folder outside, live or dangling.
    let linked_dir = Project::empty();
    symlink(outside.path(), linked_dir.root.join(".oah")).unwrap();
    refused(&linked_dir, ".oah link");
    let dangling_dir = Project::empty();
    symlink(
        outside.path().join("absent"),
        dangling_dir.root.join(".oah"),
    )
    .unwrap();
    refused(&dangling_dir, ".oah dangling link");

    // 2. The file, its lock or its backup is a link inside a real .oah.
    let linked_file = Project::empty();
    fs::create_dir(linked_file.root.join(".oah")).unwrap();
    symlink(&outside_file, linked_file.config()).unwrap();
    refused(&linked_file, "config.toml link");
    let linked_lock = Project::with("[sections]\norder = [\"plan\"]\n");
    symlink(
        &outside_file,
        linked_lock.root.join(".oah/config.toml.lock"),
    )
    .unwrap();
    refused(&linked_lock, "lock link");
    let linked_backup = Project::with(V04);
    symlink(&outside_file, backup_path(&linked_backup.config())).unwrap();
    refused(&linked_backup, "backup link");

    // 3. A root reached through a link is the folder it resolves to: the save lands there and nowhere else.
    let real = Project::empty();
    let alias = outside.path().join("alias");
    symlink(&real.root, &alias).unwrap();
    let through = ToolsBinding::new(real.id(), alias.clone());
    let plan = plan_tools_save(&through, &changes).unwrap();
    assert_eq!(plan.root(), real.root);
    apply_tools_save(&through, &changes, &plan.approve(false), quick()).unwrap();
    assert_eq!(real.tools().wiki.dir.value, "notes");
    // The id of the link's own path is another project.
    let by_alias_id = ToolsBinding::new(paths::folder_id(&alias), alias);
    assert_eq!(
        plan_tools_save(&by_alias_id, &changes).unwrap_err().code(),
        codes::PROJECT_CHANGED
    );
}

#[cfg(windows)]
#[test]
fn a_junction_on_the_way_never_lets_a_save_leave_the_project() {
    let changes = wiki("notes");
    let outside = tempfile::tempdir().unwrap();
    let outside_file = outside.path().join("config.toml");
    fs::write(&outside_file, "[sections]\norder = [\"plan\"]\n").unwrap();
    let project = Project::empty();
    junction::create(outside.path(), project.root.join(".oah")).unwrap();
    let error = plan_tools_save(&project.binding(), &changes).unwrap_err();
    assert_eq!(error.code(), "OAH_FILE_IO", "{error}");
    assert!(
        error.message().contains("is a link or reparse entry"),
        "{error}"
    );
    let approval = ToolsApproval::new("0".repeat(64), None);
    let error = apply(&project, &changes, &approval).unwrap_err();
    assert_eq!(error.code(), "OAH_FILE_IO", "{error}");
    assert_eq!(
        fs::read(&outside_file).unwrap(),
        b"[sections]\norder = [\"plan\"]\n"
    );
    let names: Vec<_> = fs::read_dir(outside.path())
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    assert_eq!(
        names,
        ["config.toml"],
        "something was written through the junction"
    );
    junction::delete(project.root.join(".oah")).unwrap();
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn a_refusal_after_the_folder_was_made_leaves_no_folder() {
    // No .oah yet, a v0.4 upgrade is not involved, and the approved plan is for other changes: the stale check
    // comes first, so nothing is created at all.
    let project = Project::empty();
    let approval = project.plan(&wiki("notes")).approve(false);
    let error = apply(&project, &wiki("other"), &approval).unwrap_err();
    assert_eq!(error.code(), codes::PLAN_STALE);
    assert!(project.tree().is_empty(), "{:?}", project.tree());
    let _keep = &project.dir;
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn the_writer_owns_exactly_the_contract_keys_and_notices_have_codes() {
    let keys: Vec<String> = TOOL_KEYS.iter().map(|key| key.join(".")).collect();
    assert_eq!(
        keys,
        [
            "wiki.dir",
            "impact.wiki_dirs",
            "impact.graph_file",
            "impact.rerank",
            "rerank.enabled",
            "rerank.provider",
            "project_tools.wiki_index.managed",
            "project_tools.wiki_index.exclude",
            "project_tools.wiki_index.embedding_model",
            "project_tools.graphify.managed",
        ]
    );
    assert_eq!(notice_codes::NOT_PORTABLE, "OAH_CONFIG_NOT_PORTABLE");
    let project = Project::with("[sections]\norder = [\"plan\"]\n[wiki]\ndir = \"/srv/wiki\"\n");
    // A value that is not portable is still read, and a save may replace it with a portable one.
    assert_eq!(
        project.tools().notices[0].code(),
        notice_codes::NOT_PORTABLE
    );
    let changes = wiki("docs/wiki");
    let plan = project.plan(&changes);
    apply(&project, &changes, &plan.approve(false)).unwrap();
    assert!(project.tools().notices.is_empty());
    let error = plan_tools_save(&project.binding(), &wiki("/srv/wiki")).unwrap_err();
    assert_eq!(error.code(), codes::INVALID);
}

#[cfg(any(target_os = "macos", windows))]
#[test]
fn a_plan_is_stale_for_another_folder_put_at_the_same_path() {
    for config in [Some("[stages]\nplan = \"claude\"\n"), None] {
        let parent = tempfile::tempdir().unwrap();
        let parent = paths::canonical_folder(parent.path()).unwrap();
        let root = parent.join("project");
        let make = |root: &Path| {
            fs::create_dir(root).unwrap();
            if let Some(text) = config {
                fs::create_dir(root.join(".oah")).unwrap();
                fs::write(root.join(".oah").join("config.toml"), text).unwrap();
            }
        };
        let listing = |root: &Path| {
            let mut names: Vec<String> = fs::read_dir(root)
                .unwrap()
                .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
                .collect();
            names.sort();
            names
        };
        make(&root);
        // The id the caller holds is made from the path alone, so both folders have it.
        let binding = ToolsBinding::new(paths::folder_id(&root), root.clone());
        let changes = wiki("notes");
        let plan = plan_tools_save(&binding, &changes).unwrap();
        let approval = plan.approve(false);
        // The approved project moves away and another real folder, with the same bytes, takes its path.
        let moved = parent.join("moved");
        fs::rename(&root, &moved).unwrap();
        make(&root);
        let before = listing(&root);
        assert_ne!(plan_tools_save(&binding, &changes).unwrap().id(), plan.id());
        let error = apply_tools_save(&binding, &changes, &approval, quick()).unwrap_err();
        assert_eq!(error.code(), codes::PLAN_STALE, "{error}");
        assert_eq!(listing(&root), before, "the other folder was changed");
        if let Some(text) = config {
            assert_eq!(
                fs::read_to_string(root.join(".oah/config.toml")).unwrap(),
                text
            );
            assert_eq!(
                fs::read_to_string(moved.join(".oah/config.toml")).unwrap(),
                text
            );
        } else {
            assert!(before.is_empty() && listing(&moved).is_empty());
        }
        // Back at its path, the approved folder takes the approved plan.
        fs::remove_dir_all(&root).unwrap();
        fs::rename(&moved, &root).unwrap();
        assert_eq!(plan_tools_save(&binding, &changes).unwrap().id(), plan.id());
        apply_tools_save(&binding, &changes, &approval, quick()).unwrap();
        let saved = fs::read_to_string(root.join(".oah/config.toml")).unwrap();
        assert!(saved.contains("dir = \"notes\""), "{saved}");
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
#[test]
fn where_the_folder_cannot_be_bound_a_plan_works_and_a_save_is_refused() {
    let project = Project::with("[stages]\nplan = \"claude\"\n");
    let changes = wiki("notes");
    let plan = project.plan(&changes);
    assert_eq!(plan.tools_after().wiki.dir.value, "notes");
    let before = project.tree();
    let error = apply(&project, &changes, &plan.approve(false)).unwrap_err();
    assert_eq!(error.code(), "OAH_FILE_IO");
    assert!(error.message().contains("nothing was written"), "{error}");
    assert_eq!(project.tree(), before);
}
