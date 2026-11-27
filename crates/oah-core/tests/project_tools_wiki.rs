// Project Wiki 로컬 작업 검사: 실제 폴더에서 미리보기·적용·재실행, 기존 위키 보존, 링크·권한·경합 거부, 텍스트 검색을 본다.
#![cfg(any(target_os = "macos", windows))]

use oah_core::Coded;
use oah_core::fsx::Fingerprint;
use oah_core::fsx::skill_publish::{ProjectRoot, PublishError};
use oah_core::project_tools::wiki::{
    CopyAction, CopyBlock, DEFAULT_DIR, FileAction, ItemStatus, STANDARD_FILES, SetupMode,
    SkipReason, WikiCopyRequest, WikiError, WikiSearchRequest, WikiSetupPlan, WikiSetupRequest,
    apply_copy, apply_setup, discover, plan_copy, plan_setup, search, standard_files,
};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use tempfile::TempDir;
use unicode_normalization::UnicodeNormalization;

const TODAY: &str = "2026-01-02";

struct Fixture {
    root: ProjectRoot,
    project: PathBuf,
    outside: PathBuf,
    _temp: TempDir,
}

impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let base = temp.path().canonicalize().unwrap();
        let (project, home, outside) = (
            base.join("project"),
            base.join("home"),
            base.join("outside"),
        );
        for dir in [&project, &home, &outside] {
            fs::create_dir(dir).unwrap();
        }
        Self {
            root: ProjectRoot::open(&project, &home).unwrap(),
            project,
            outside,
            _temp: temp,
        }
    }

    /// A project path from a name written with `/` (a verbatim Windows path does not accept `/`).
    fn at(&self, rel: &str) -> PathBuf {
        rel.split('/')
            .fold(self.project.clone(), |path, part| path.join(part))
    }

    fn write(&self, rel: &str, bytes: &[u8]) {
        let path = self.at(rel);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }

    fn snapshot(&self) -> BTreeMap<String, Vec<u8>> {
        tree(&self.project)
    }
}

/// Every name under `root`: a file with its bytes, a folder with a trailing `/`, a link as `@`. Links are not followed.
fn tree(root: &Path) -> BTreeMap<String, Vec<u8>> {
    fn visit(folder: &Path, prefix: &str, out: &mut BTreeMap<String, Vec<u8>>) {
        for entry in fs::read_dir(folder).unwrap() {
            let entry = entry.unwrap();
            let name = format!("{prefix}{}", entry.file_name().to_string_lossy());
            let meta = fs::symlink_metadata(entry.path()).unwrap();
            if meta.file_type().is_symlink() || is_reparse(&meta) {
                out.insert(format!("{name}@"), Vec::new());
            } else if meta.is_dir() {
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

#[cfg(windows)]
fn is_reparse(meta: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    meta.file_attributes() & 0x400 != 0
}

#[cfg(not(windows))]
fn is_reparse(_: &fs::Metadata) -> bool {
    false
}

/// A link to a folder that a non-elevated process can make: a symbolic link, or a junction on Windows.
#[cfg(unix)]
fn link_folder(target: &Path, link: &Path) {
    std::os::unix::fs::symlink(target, link).unwrap();
}

#[cfg(windows)]
fn link_folder(target: &Path, link: &Path) {
    junction::create(target, link).unwrap();
}

fn request(configured: Option<&str>, selected: Option<&str>) -> WikiSetupRequest {
    WikiSetupRequest {
        configured: configured.map(str::to_string),
        selected: selected.map(str::to_string),
        today: TODAY.to_string(),
    }
}

fn actions(plan: &WikiSetupPlan) -> Vec<(&str, FileAction)> {
    plan.files
        .iter()
        .map(|file| (file.path.as_str(), file.action))
        .collect()
}

fn find(dir: &str, query: &str) -> WikiSearchRequest {
    WikiSearchRequest {
        dir: dir.to_string(),
        query: query.to_string(),
        max_hits: None,
    }
}

#[test]
fn standard_files_are_the_bytes_the_source_cli_writes() {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("project_tools_wiki/standard_files.json")).unwrap();
    let files = standard_files(fixture["today"].as_str().unwrap()).unwrap();
    let got: BTreeMap<String, String> = files
        .iter()
        .map(|(path, data)| (path.to_string(), Fingerprint::of(data).to_hex()))
        .collect();
    let want: BTreeMap<String, String> = fixture["files"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(path, digest)| (path.clone(), digest.as_str().unwrap().to_string()))
        .collect();
    assert_eq!(got, want);
}

#[test]
fn a_fresh_project_previews_docs_wiki_then_creates_it_and_a_second_run_changes_nothing() {
    let f = Fixture::new();
    let req = request(None, None);
    let plan = plan_setup(&f.root, &req).unwrap();
    assert!(f.snapshot().is_empty(), "a preview writes nothing");
    assert_eq!(plan.dir, DEFAULT_DIR);
    assert_eq!(plan.mode, SetupMode::Create);
    assert_eq!(plan.folders, ["docs", "docs/wiki", "docs/wiki/templates"]);
    let expected = standard_files(TODAY).unwrap();
    for ((path, data), file) in expected.iter().zip(&plan.files) {
        assert_eq!(
            (file.path.as_str(), file.action),
            (*path, FileAction::Create)
        );
        assert_eq!(file.sha256, Some(Fingerprint::of(data).to_hex()));
    }
    assert_eq!(
        plan.config_dir, None,
        "docs/wiki is already what a config without the key means"
    );
    assert_eq!(plan.changes(), 8);
    assert!(plan.id.starts_with("sha256:") && plan.id.len() == 71);
    assert_eq!(
        plan_setup(&f.root, &req).unwrap(),
        plan,
        "the same disk gives the same plan"
    );

    let report = apply_setup(&f.root, &req, &plan.id).unwrap();
    assert_eq!(report.stop, None);
    assert_eq!(report.folders_created, plan.folders);
    assert!(
        report
            .items
            .iter()
            .all(|item| item.status == ItemStatus::Created)
    );
    let mut want: BTreeMap<String, Vec<u8>> = plan
        .folders
        .iter()
        .map(|folder| (format!("{folder}/"), Vec::new()))
        .collect();
    want.extend(
        expected
            .iter()
            .map(|(path, data)| (format!("docs/wiki/{path}"), data.clone())),
    );
    assert_eq!(
        f.snapshot(),
        want,
        "exactly the planned folders and files, and no temporary file"
    );

    let again = plan_setup(&f.root, &req).unwrap();
    assert_eq!(again.mode, SetupMode::Adopt);
    assert_eq!(again.changes(), 0);
    assert!(
        again
            .files
            .iter()
            .all(|file| file.action == FileAction::Keep && file.sha256.is_none())
    );
    assert_eq!(
        apply_setup(&f.root, &req, &plan.id),
        Err(WikiError::Stale),
        "the first plan is spent"
    );
    let second = apply_setup(&f.root, &req, &again.id).unwrap();
    assert_eq!(second.stop, None);
    assert!(second.folders_created.is_empty());
    assert!(
        second
            .items
            .iter()
            .all(|item| item.status == ItemStatus::Kept)
    );
    assert_eq!(f.snapshot(), want);
}

#[test]
fn an_existing_wiki_is_adopted_in_place_and_every_user_file_stays_byte_identical() {
    let f = Fixture::new();
    f.write("wiki/Index.md", b"# My own index\r\n\r\n- [[retry]]\r\n");
    f.write("wiki/log.md", b"my log, no markers\n");
    f.write("wiki/decisions/2026/retry.md", "# 재시도 정책\n".as_bytes());
    f.write(
        "wiki/attachments/diagram.png",
        &[0x89, b'P', b'N', b'G', 0, 255, 13, 10],
    );
    f.write("wiki/.obsidian/app.json", b"{\"vimMode\":true}");
    f.write("wiki/.obsidian/workspace.json", b"{\"main\":{}}");
    f.write("wiki/templates/note.md", b"my template\n");
    f.write("src/main.rs", b"fn main() {}\n");
    let before = f.snapshot();

    let found = discover(&f.root).unwrap();
    assert_eq!(found.candidates.len(), 1);
    let only = &found.candidates[0];
    assert_eq!(
        (
            only.dir.as_str(),
            only.standard,
            only.obsidian,
            only.index,
            only.log
        ),
        ("wiki", true, true, true, true)
    );
    assert!(found.problems.is_empty() && !found.truncated);

    let req = request(None, None);
    let plan = plan_setup(&f.root, &req).unwrap();
    assert_eq!((plan.dir.as_str(), plan.mode), ("wiki", SetupMode::Adopt));
    assert!(plan.folders.is_empty());
    assert_eq!(
        actions(&plan),
        [
            ("Index.md", FileAction::Keep),
            ("log.md", FileAction::Keep),
            ("wiki-guide.md", FileAction::Create),
            ("templates/note.md", FileAction::Keep),
            ("templates/decision.md", FileAction::Create),
        ]
    );
    assert_eq!(
        plan.config_dir.as_deref(),
        Some("wiki"),
        "the config writer is told, nothing is configured here"
    );
    assert_eq!(f.snapshot(), before);

    let report = apply_setup(&f.root, &req, &plan.id).unwrap();
    assert_eq!(report.stop, None);
    assert_eq!(report.config_dir.as_deref(), Some("wiki"));
    let after = f.snapshot();
    for (path, bytes) in &before {
        assert_eq!(after.get(path), Some(bytes), "{path} changed");
    }
    let added: Vec<&str> = after
        .keys()
        .filter(|path| !before.contains_key(*path))
        .map(String::as_str)
        .collect();
    assert_eq!(added, ["wiki/templates/decision.md", "wiki/wiki-guide.md"]);
    assert!(
        !f.at(".oah").exists(),
        "the configuration is not this module's to write"
    );

    // With the folder configured, the same wiki needs no config change.
    let configured = plan_setup(&f.root, &request(Some("wiki"), None)).unwrap();
    assert_eq!((configured.changes(), configured.config_dir), (0, None));
}

#[test]
fn an_ordinary_folder_the_user_chooses_gets_the_standard_files_beside_its_own() {
    let f = Fixture::new();
    f.write("notes/todo.txt", b"buy milk");
    f.write("notes/ideas/one.md", b"# One\n");
    let before = f.snapshot();
    assert!(discover(&f.root).unwrap().candidates.is_empty());
    let req = request(None, Some("./notes/"));
    let plan = plan_setup(&f.root, &req).unwrap();
    assert_eq!((plan.dir.as_str(), plan.mode), ("notes", SetupMode::Adopt));
    assert_eq!(plan.folders, ["notes/templates"]);
    assert_eq!(plan.config_dir.as_deref(), Some("notes"));
    let report = apply_setup(&f.root, &req, &plan.id).unwrap();
    assert_eq!(report.stop, None);
    let after = f.snapshot();
    assert!(
        before
            .iter()
            .all(|(path, bytes)| after.get(path) == Some(bytes))
    );
    assert_eq!(after.len(), before.len() + 6);
}

#[test]
fn a_configured_folder_that_is_broken_is_reported_and_no_other_folder_is_set_up() {
    let f = Fixture::new();
    f.write("README.md", b"# Project\n");
    f.write("docs/file-not-folder", b"x");
    link_folder(&f.outside, &f.at("linked"));
    let before = f.snapshot();
    let plan = |configured: &str| plan_setup(&f.root, &request(Some(configured), None));

    assert_eq!(
        plan("notes/wiki"),
        Err(WikiError::Missing {
            dir: "notes/wiki".into()
        })
    );
    assert_eq!(
        plan("docs/wiki"),
        Err(WikiError::Missing {
            dir: "docs/wiki".into()
        })
    );
    assert_eq!(
        plan("linked"),
        Err(WikiError::Unsafe {
            path: "linked".into()
        })
    );
    assert_eq!(
        plan("linked/wiki"),
        Err(WikiError::Unsafe {
            path: "linked".into()
        })
    );
    assert_eq!(
        plan("docs/file-not-folder"),
        Err(WikiError::Unsafe {
            path: "docs/file-not-folder".into()
        })
    );
    for escaping in ["../outside", "/tmp/wiki", "C:\\wiki", ".git/wiki", ""] {
        let err = plan(escaping).unwrap_err();
        assert_eq!(err.code(), "OAH_WIKI_DIR_INVALID", "{escaping:?}");
    }
    assert_eq!(
        apply_setup(
            &f.root,
            &request(Some("notes/wiki"), None),
            "sha256:anything"
        ),
        Err(WikiError::Missing {
            dir: "notes/wiki".into()
        })
    );
    assert_eq!(
        f.snapshot(),
        before,
        "a broken configured folder writes nothing, anywhere"
    );
    assert!(tree(&f.outside).is_empty());

    // Only the user's explicit choice creates the folder the configuration names.
    let chosen = request(Some("notes/wiki"), Some("notes/wiki"));
    let plan = plan_setup(&f.root, &chosen).unwrap();
    assert_eq!(
        (plan.mode, plan.config_dir.clone()),
        (SetupMode::Create, None)
    );
    assert_eq!(apply_setup(&f.root, &chosen, &plan.id).unwrap().stop, None);
    assert!(f.at("notes/wiki/index.md").is_file());
    // And a choice that differs from a broken configuration is told to the config writer.
    let elsewhere = plan_setup(&f.root, &request(Some("../broken"), Some("notes/wiki"))).unwrap();
    assert_eq!(elsewhere.config_dir.as_deref(), Some("notes/wiki"));
}

#[test]
fn two_wikis_need_a_choice_and_nothing_is_written_until_one_is_made() {
    let f = Fixture::new();
    f.write("docs/wiki/index.md", b"standard\n");
    f.write("vault/.obsidian/app.json", b"{}");
    f.write("vault/decisions/a.md", b"# A\n");
    f.write("node_modules/pkg/.obsidian/app.json", b"{}");
    f.write(".hidden/vault/.obsidian/app.json", b"{}");
    let before = f.snapshot();
    let found = discover(&f.root).unwrap();
    assert_eq!(
        found
            .candidates
            .iter()
            .map(|c| c.dir.as_str())
            .collect::<Vec<_>>(),
        ["docs/wiki", "vault"]
    );
    let ambiguous = WikiError::Ambiguous {
        candidates: vec!["docs/wiki".into(), "vault".into()],
    };
    let req = request(None, None);
    assert_eq!(plan_setup(&f.root, &req), Err(ambiguous.clone()));
    assert_eq!(apply_setup(&f.root, &req, "sha256:guess"), Err(ambiguous));
    assert_eq!(f.snapshot(), before);

    // An Obsidian vault above a standard wiki folder marks that same wiki, as on the status card.
    let nested = Fixture::new();
    nested.write("knowledge/.obsidian/app.json", b"{}");
    nested.write("knowledge/wiki/index.md", b"standard\n");
    let found = discover(&nested.root).unwrap();
    assert_eq!(
        found
            .candidates
            .iter()
            .map(|c| c.dir.as_str())
            .collect::<Vec<_>>(),
        ["knowledge/wiki"]
    );
    let plan = plan_setup(&nested.root, &request(None, None)).unwrap();
    assert_eq!(
        (plan.dir.as_str(), plan.config_dir.as_deref()),
        ("knowledge/wiki", Some("knowledge/wiki"))
    );

    let chosen = request(None, Some("vault"));
    let plan = plan_setup(&f.root, &chosen).unwrap();
    assert_eq!((plan.dir.as_str(), plan.mode), ("vault", SetupMode::Adopt));
    assert_eq!(plan.config_dir.as_deref(), Some("vault"));
    assert_eq!(apply_setup(&f.root, &chosen, &plan.id).unwrap().stop, None);
    let after = f.snapshot();
    assert_eq!(
        after.get("docs/wiki/index.md").map(Vec::as_slice),
        Some(&b"standard\n"[..])
    );
    assert!(
        !after.contains_key("docs/wiki/log.md"),
        "the folder that was not chosen is untouched"
    );
}

#[test]
fn localized_and_long_utf8_folder_names_are_used_as_spelled_on_disk() {
    let f = Fixture::new();
    let long = "위키".repeat(40);
    let on_disk: String = "문서".nfd().collect();
    assert_ne!(on_disk, "문서");
    fs::create_dir(f.project.join(&on_disk)).unwrap();
    let asked = format!("문서/{long}/{long}/결정 기록");
    let req = request(None, Some(&asked));
    let plan = plan_setup(&f.root, &req).unwrap();
    let dir = format!("{on_disk}/{long}/{long}/결정 기록");
    assert_eq!(plan.dir, dir);
    assert_eq!(plan.folders.len(), 4);
    assert_eq!(plan.config_dir.as_deref(), Some(dir.as_str()));
    assert_eq!(apply_setup(&f.root, &req, &plan.id).unwrap().stop, None);
    assert_eq!(
        fs::read_dir(&f.project).unwrap().count(),
        1,
        "no second folder beside the one on disk"
    );
    let wiki = f
        .project
        .join(&on_disk)
        .join(&long)
        .join(&long)
        .join("결정 기록");
    assert_eq!(
        fs::read(wiki.join("templates").join("decision.md")).unwrap(),
        standard_files(TODAY).unwrap()[4].1
    );

    let note: String = "# 재시도 정책\n\n지수 백오프를 쓴다.\n".nfd().collect();
    fs::create_dir(wiki.join("결정")).unwrap();
    fs::write(wiki.join("결정").join("재시도.md"), note).unwrap();
    let result = search(&f.root, &find(&asked, "백오프")).unwrap();
    assert_eq!(result.dir, dir);
    assert_eq!(result.hits.len(), 1);
    let hit = &result.hits[0];
    assert_eq!((hit.file.as_str(), hit.line), ("결정/재시도.md", 3));
    assert_eq!(
        hit.heading.as_deref().map(|h| h.nfc().collect::<String>()),
        Some("재시도 정책".into())
    );
}

#[test]
fn nested_decisions_are_found_with_file_heading_and_line_and_unsafe_content_is_left_out() {
    let f = Fixture::new();
    let decision = [
        "---",
        "title: \"Retry policy\"",
        "tags: [decision, retry-backoff]",
        "---",
        "# Retry policy",
        "",
        "## Decision",
        "",
        "We use exponential Backoff with jitter for every retry.",
        "",
        "```text",
        "# not a heading: retry backoff inside a fence",
        "```",
        "",
        "## Consequences",
        "",
        "Retry storms stop; backoff is capped.",
    ]
    .join("\n");
    f.write(
        "docs/wiki/decisions/2026/retry-policy.md",
        decision.as_bytes(),
    );
    f.write("docs/wiki/index.md", b"Notes:\n- [[retry-policy]]\n");
    f.write(
        "docs/wiki/crlf.md",
        "\u{feff}# Windows note\r\n\r\nretry with BACKOFF here\r\n".as_bytes(),
    );
    f.write("docs/wiki/.obsidian/plugins/note.md", b"retry backoff\n");
    f.write("docs/wiki/.trash/gone.md", b"retry backoff\n");
    f.write("docs/wiki/archive/old.md", b"retry backoff\n");
    f.write("docs/wiki/raw/Archives/older.md", b"retry backoff\n");
    f.write("docs/wiki/log_archive_01.md", b"retry backoff\n");
    f.write("docs/wiki/notes.txt", b"retry backoff\n");
    f.write(
        "docs/wiki/big.md",
        format!("retry backoff\n{}", "x".repeat(1 << 20)).as_bytes(),
    );
    f.write("docs/wiki/binary.md", b"retry backoff \xff\xfe\n");
    fs::write(f.outside.join("secret.md"), b"retry backoff outside\n").unwrap();
    fs::hard_link(f.outside.join("secret.md"), f.at("docs/wiki/hardlinked.md")).unwrap();
    link_folder(&f.outside, &f.at("docs/wiki/shared"));
    let before = f.snapshot();

    let result = search(&f.root, &find("docs/wiki", "  retry   BACKOFF ")).unwrap();
    let hits: Vec<(&str, usize, Option<&str>, &str)> = result
        .hits
        .iter()
        .map(|hit| {
            (
                hit.file.as_str(),
                hit.line,
                hit.heading.as_deref(),
                hit.context.as_str(),
            )
        })
        .collect();
    assert_eq!(
        hits,
        [
            (
                "crlf.md",
                3,
                Some("Windows note"),
                "retry with BACKOFF here"
            ),
            (
                "decisions/2026/retry-policy.md",
                3,
                None,
                "tags: [decision, retry-backoff]"
            ),
            (
                "decisions/2026/retry-policy.md",
                9,
                Some("Decision"),
                "We use exponential Backoff with jitter for every retry."
            ),
            (
                "decisions/2026/retry-policy.md",
                12,
                Some("Decision"),
                "# not a heading: retry backoff inside a fence"
            ),
            (
                "decisions/2026/retry-policy.md",
                17,
                Some("Consequences"),
                "Retry storms stop; backoff is capped."
            ),
        ]
    );
    let skipped: Vec<(&str, SkipReason)> = result
        .skipped
        .iter()
        .map(|skip| (skip.path.as_str(), skip.reason))
        .collect();
    assert_eq!(
        skipped,
        [
            ("big.md", SkipReason::Oversized),
            ("binary.md", SkipReason::NotText),
            ("hardlinked.md", SkipReason::Link),
            ("shared", SkipReason::Link),
        ]
    );
    assert_eq!((result.skipped_total, result.truncated), (4, false));
    assert_eq!(
        result.files_searched, 4,
        "index, crlf, the decision and the non-text file were read"
    );
    assert_eq!(f.snapshot(), before, "a search writes nothing");

    let one = search(
        &f.root,
        &WikiSearchRequest {
            max_hits: Some(1),
            ..find("docs/wiki", "retry")
        },
    )
    .unwrap();
    assert_eq!((one.hits.len(), one.truncated), (1, true));
    assert!(
        search(&f.root, &find("docs/wiki", "no-such-word"))
            .unwrap()
            .hits
            .is_empty()
    );
    assert_eq!(
        search(&f.root, &find("wiki", "retry")),
        Err(WikiError::Missing { dir: "wiki".into() })
    );
    assert_eq!(
        search(&f.root, &find("docs/wiki", " ")),
        Err(WikiError::QueryInvalid)
    );
    assert_eq!(
        search(&f.root, &find("docs/wiki/shared", "retry")),
        Err(WikiError::Unsafe {
            path: "docs/wiki/shared".into()
        })
    );
    assert_eq!(
        search(&f.root, &find("../outside", "retry"))
            .unwrap_err()
            .code(),
        "OAH_WIKI_DIR_INVALID"
    );
}

#[test]
fn a_folder_swapped_for_a_link_after_the_preview_is_refused_and_nothing_lands_outside() {
    let f = Fixture::new();
    fs::create_dir(f.at("docs")).unwrap();
    let req = request(None, None);
    let plan = plan_setup(&f.root, &req).unwrap();
    assert_eq!(plan.folders, ["docs/wiki", "docs/wiki/templates"]);
    fs::remove_dir(f.at("docs")).unwrap();
    link_folder(&f.outside, &f.at("docs"));
    let unsafe_docs = WikiError::Unsafe {
        path: "docs".into(),
    };
    assert_eq!(
        apply_setup(&f.root, &req, &plan.id),
        Err(unsafe_docs.clone())
    );
    assert_eq!(plan_setup(&f.root, &req), Err(unsafe_docs.clone()));
    let found = discover(&f.root).unwrap();
    assert!(found.candidates.is_empty());
    assert_eq!(found.problems.len(), 1);
    assert_eq!(
        (found.problems[0].dir.as_str(), &found.problems[0].error),
        (DEFAULT_DIR, &unsafe_docs)
    );
    assert!(tree(&f.outside).is_empty());
}

#[test]
fn a_wiki_or_templates_folder_replaced_after_the_preview_makes_the_plan_stale() {
    let f = Fixture::new();
    f.write("wiki/index.md", b"mine\n");
    fs::create_dir(f.at("wiki/templates")).unwrap();
    let req = request(None, Some("wiki"));

    // Another real folder under the same name, with the same content: still not the folder that was previewed.
    let plan = plan_setup(&f.root, &req).unwrap();
    fs::rename(f.at("wiki"), f.at("wiki-moved")).unwrap();
    f.write("wiki/index.md", b"mine\n");
    fs::create_dir(f.at("wiki/templates")).unwrap();
    let before = f.snapshot();
    assert_eq!(apply_setup(&f.root, &req, &plan.id), Err(WikiError::Stale));
    assert_eq!(f.snapshot(), before);

    // The templates folder swapped for a link: the new survey blocks it, so the confirmed plan is not what is on disk.
    let plan = plan_setup(&f.root, &req).unwrap();
    fs::remove_dir(f.at("wiki/templates")).unwrap();
    link_folder(&f.outside, &f.at("wiki/templates"));
    let before = f.snapshot();
    assert_eq!(apply_setup(&f.root, &req, &plan.id), Err(WikiError::Stale));
    let blocked = plan_setup(&f.root, &req).unwrap();
    assert_eq!(
        actions(&blocked)[3..],
        [
            ("templates/note.md", FileAction::Blocked),
            ("templates/decision.md", FileAction::Blocked)
        ]
    );
    assert_eq!(
        apply_setup(&f.root, &req, &blocked.id),
        Err(WikiError::Blocked {
            paths: vec![
                "wiki/templates/note.md".into(),
                "wiki/templates/decision.md".into()
            ]
        })
    );
    assert_eq!(f.snapshot(), before);
    assert!(tree(&f.outside).is_empty());
}

#[test]
fn a_file_that_appears_after_the_preview_is_never_overwritten() {
    let f = Fixture::new();
    let req = request(None, None);
    let plan = plan_setup(&f.root, &req).unwrap();
    f.write(
        "docs/wiki/index.md",
        b"written by the user after the preview\n",
    );
    let before = f.snapshot();
    assert_eq!(apply_setup(&f.root, &req, &plan.id), Err(WikiError::Stale));
    assert_eq!(f.snapshot(), before);

    // The writer itself refuses a taken name, whatever the plan said, and leaves no temporary file.
    let wiki = f
        .root
        .folder()
        .unwrap()
        .folder("docs")
        .unwrap()
        .folder("wiki")
        .unwrap();
    assert!(matches!(
        wiki.publish_new("index.md", b"replacement"),
        Err(PublishError::Occupied)
    ));
    assert!(matches!(
        wiki.create_folder("index.md"),
        Err(PublishError::Occupied)
    ));
    for unsafe_name in [
        "../escape.md",
        "sub/note.md",
        "..",
        "con",
        "note.md.",
        ".git",
    ] {
        assert!(
            matches!(
                wiki.publish_new(unsafe_name, b"x"),
                Err(PublishError::UnsafePath)
            ),
            "{unsafe_name}"
        );
    }
    assert_eq!(f.snapshot(), before);

    // A standard name taken by a folder blocks the plan instead of being worked around.
    fs::create_dir(f.at("docs/wiki/log.md")).unwrap();
    let blocked = plan_setup(&f.root, &req).unwrap();
    assert_eq!(actions(&blocked)[1], ("log.md", FileAction::Blocked));
    assert_eq!(
        apply_setup(&f.root, &req, &blocked.id),
        Err(WikiError::Blocked {
            paths: vec!["docs/wiki/log.md".into()]
        })
    );
}

#[test]
fn a_held_folder_that_is_moved_away_is_not_written_through() {
    let f = Fixture::new();
    let docs = f.root.folder().unwrap().create_folder("docs").unwrap();
    docs.publish_new("first.md", b"one").unwrap();
    // Windows does not let a folder that is held open be renamed; where the swap is possible it must be noticed.
    match fs::rename(f.at("docs"), f.at("docs-moved")) {
        Ok(()) => {
            link_folder(&f.outside, &f.at("docs"));
            assert!(matches!(
                docs.publish_new("second.md", b"two"),
                Err(PublishError::UnsafePath)
            ));
            assert!(matches!(
                docs.create_folder("sub"),
                Err(PublishError::UnsafePath)
            ));
            assert_eq!(
                tree(&f.at("docs-moved")).keys().collect::<Vec<_>>(),
                ["first.md"]
            );
        }
        Err(_) if cfg!(windows) => docs.publish_new("second.md", b"two").unwrap(),
        Err(err) => panic!("the held folder could not be renamed: {err}"),
    }
    assert!(tree(&f.outside).is_empty());
}

#[cfg(unix)]
#[test]
fn permission_problems_are_reported_item_by_item_and_a_new_plan_finishes_the_rest() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    f.write("wiki/templates/mine.md", b"mine\n");
    let chmod = |rel: &str, mode: u32| {
        fs::set_permissions(f.at(rel), fs::Permissions::from_mode(mode)).unwrap();
    };
    let statuses = |report: &oah_core::project_tools::wiki::WikiApplyReport| -> Vec<ItemStatus> {
        report.items.iter().map(|item| item.status).collect()
    };
    let req = request(None, Some("wiki"));
    use ItemStatus::{Created, Failed, Kept, Pending};

    // A wiki folder that cannot be written: the preview still works, the apply stops at the first file.
    chmod("wiki", 0o555);
    let plan = plan_setup(&f.root, &req).unwrap();
    let before = f.snapshot();
    let report = apply_setup(&f.root, &req, &plan.id).unwrap();
    assert_eq!(
        report.stop,
        Some(WikiError::Denied {
            path: "wiki/index.md".into()
        })
    );
    assert_eq!(
        statuses(&report),
        [Failed, Pending, Pending, Pending, Pending]
    );
    assert_eq!(report.config_dir, None);
    assert_eq!(f.snapshot(), before);
    chmod("wiki", 0o755);

    // Only the templates folder cannot be written: three files land, the report says where it stopped.
    chmod("wiki/templates", 0o555);
    let plan = plan_setup(&f.root, &req).unwrap();
    let report = apply_setup(&f.root, &req, &plan.id).unwrap();
    assert_eq!(
        report.stop,
        Some(WikiError::Denied {
            path: "wiki/templates/note.md".into()
        })
    );
    assert_eq!(
        statuses(&report),
        [Created, Created, Created, Failed, Pending]
    );
    assert_eq!(apply_setup(&f.root, &req, &plan.id), Err(WikiError::Stale));
    chmod("wiki/templates", 0o755);

    let rest = plan_setup(&f.root, &req).unwrap();
    assert_eq!(rest.changes(), 2);
    let report = apply_setup(&f.root, &req, &rest.id).unwrap();
    assert_eq!(report.stop, None);
    assert_eq!(statuses(&report), [Kept, Kept, Kept, Created, Created]);
    assert_eq!(report.config_dir.as_deref(), Some("wiki"));
    assert_eq!(fs::read(f.at("wiki/templates/mine.md")).unwrap(), b"mine\n");
    let mode = fs::metadata(f.at("wiki/index.md"))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o111, 0, "a new note is not executable");
    assert_ne!(mode & 0o400, 0);

    // A folder that cannot be read is a diagnostic for the configured wiki and a skipped folder for a search.
    f.write("wiki/private/secret.md", b"mine too\n");
    chmod("wiki/private", 0o000);
    let result = search(&f.root, &find("wiki", "mine")).unwrap();
    assert_eq!(
        result
            .hits
            .iter()
            .map(|hit| hit.file.as_str())
            .collect::<Vec<_>>(),
        ["templates/mine.md"]
    );
    assert_eq!(
        result
            .skipped
            .iter()
            .map(|skip| (skip.path.as_str(), skip.reason))
            .collect::<Vec<_>>(),
        [("private", SkipReason::Unreadable)]
    );
    chmod("wiki/private", 0o755);
    chmod("wiki", 0o000);
    assert_eq!(
        plan_setup(&f.root, &request(Some("wiki"), None)),
        Err(WikiError::Denied {
            path: "wiki".into()
        })
    );
    chmod("wiki", 0o755);
}

#[test]
fn discovery_is_bounded_and_reads_only_names() {
    let f = Fixture::new();
    f.write("knowledge/wiki/log.md", b"log\n");
    f.write("a/b/deep/.obsidian/app.json", b"{}");
    f.write("a/b/c/too-deep/.obsidian/app.json", b"{}");
    f.write("Wiki-Notes/.obsidian/app.json", b"{}");
    f.write("docs/wiki", b"a file where the standard folder would be");
    let before = f.snapshot();
    let found = discover(&f.root).unwrap();
    assert_eq!(
        found
            .candidates
            .iter()
            .map(|c| (c.dir.as_str(), c.standard, c.obsidian, c.index, c.log))
            .collect::<Vec<_>>(),
        [
            ("knowledge/wiki", true, false, false, true),
            ("Wiki-Notes", false, true, false, false),
            ("a/b/deep", false, true, false, false),
        ]
    );
    assert!(
        !found.truncated,
        "three levels is the scope, not a bound that was hit"
    );
    assert_eq!(found.problems.len(), 1);
    assert_eq!(
        found.problems[0].error,
        WikiError::Unsafe {
            path: "docs/wiki".into()
        }
    );
    assert_eq!(f.snapshot(), before);
    assert_eq!(
        plan_setup(&f.root, &request(None, Some("docs/wiki"))),
        Err(WikiError::Unsafe {
            path: "docs/wiki".into()
        })
    );
    assert_eq!(
        plan_setup(&f.root, &request(None, Some("wiki")))
            .unwrap()
            .mode,
        SetupMode::Create
    );
    let bad_date = WikiSetupRequest {
        today: "today".into(),
        ..request(None, Some("wiki"))
    };
    assert_eq!(plan_setup(&f.root, &bad_date), Err(WikiError::DateInvalid));
    assert_eq!(STANDARD_FILES.len(), 5);
}

fn copy_of(source: &str) -> WikiCopyRequest {
    WikiCopyRequest {
        source: source.to_string(),
    }
}

#[test]
fn a_wiki_is_copied_to_the_standard_folder_and_the_source_is_kept() {
    let f = Fixture::new();
    f.write(
        "wiki/index.md",
        b"- [[retry]]\n- ![diagram](attachments/d.png)\n",
    );
    f.write(
        "wiki/decisions/2026/retry.md",
        b"# Retry\n\nBack to [[index]] and [the picture](../../attachments/d.png).\n[gone](gone.md) [[nowhere]]\n",
    );
    f.write("wiki/attachments/d.png", &[0x89, b'P', b'N', b'G', 0, 255]);
    f.write("wiki/.obsidian/app.json", b"{\"vimMode\":true}");
    f.write("wiki/.obsidian/workspace.json", b"{\"main\":{}}");
    f.write("wiki/.obsidian/workspace-mobile.json", b"{}");
    fs::create_dir(f.at("wiki/empty")).unwrap();
    let before = f.snapshot();

    let req = copy_of("wiki");
    let plan = plan_copy(&f.root, &req).unwrap();
    assert_eq!(
        (plan.source.as_str(), plan.destination.as_str()),
        ("wiki", DEFAULT_DIR)
    );
    assert_eq!(
        plan.folders,
        [
            "docs",
            "docs/wiki",
            "docs/wiki/.obsidian",
            "docs/wiki/attachments",
            "docs/wiki/decisions",
            "docs/wiki/decisions/2026",
        ]
    );
    assert_eq!(
        plan.files
            .iter()
            .map(|file| (file.path.as_str(), file.action))
            .collect::<Vec<_>>(),
        [
            (".obsidian/app.json", CopyAction::Copy),
            ("attachments/d.png", CopyAction::Copy),
            ("decisions/2026/retry.md", CopyAction::Copy),
            ("index.md", CopyAction::Copy),
        ]
    );
    assert_eq!(
        plan.left,
        [
            ".obsidian/workspace-mobile.json",
            ".obsidian/workspace.json"
        ]
    );
    assert_eq!((plan.blocked_total, plan.unresolved_total), (0, 2));
    assert_eq!(
        plan.unresolved_links
            .iter()
            .map(|link| (link.file.as_str(), link.line, link.target.as_str()))
            .collect::<Vec<_>>(),
        [
            ("decisions/2026/retry.md", 4, "nowhere"),
            ("decisions/2026/retry.md", 4, "gone.md")
        ]
    );
    assert_eq!(f.snapshot(), before, "a preview writes nothing");
    assert_eq!(plan_copy(&f.root, &req).unwrap(), plan);

    let report = apply_copy(&f.root, &req, &plan.id).unwrap();
    assert_eq!(report.stop, None);
    assert_eq!(report.folders_created, plan.folders);
    assert!(
        report
            .items
            .iter()
            .all(|item| item.status == ItemStatus::Created)
    );
    assert_eq!(report.config_dir.as_deref(), Some(DEFAULT_DIR));
    let after = f.snapshot();
    for (path, bytes) in &before {
        assert_eq!(after.get(path), Some(bytes), "the source keeps {path}");
    }
    for file in &plan.files {
        assert_eq!(
            after.get(&format!("docs/wiki/{}", file.path)),
            before.get(&format!("wiki/{}", file.path)),
            "{}",
            file.path
        );
    }
    assert_eq!(
        after.len(),
        before.len() + plan.folders.len() + plan.files.len()
    );
    assert!(!after.contains_key("docs/wiki/.obsidian/workspace.json"));

    // Run again: everything is already there, nothing is written, and the old confirmation is spent.
    let again = plan_copy(&f.root, &req).unwrap();
    assert_eq!((again.copies(), again.folders.len()), (0, 0));
    assert!(
        again
            .files
            .iter()
            .all(|file| file.action == CopyAction::Same)
    );
    assert_eq!(apply_copy(&f.root, &req, &plan.id), Err(WikiError::Stale));
    let second = apply_copy(&f.root, &req, &again.id).unwrap();
    assert!(
        second
            .items
            .iter()
            .all(|item| item.status == ItemStatus::Kept)
    );
    assert_eq!(second.config_dir.as_deref(), Some(DEFAULT_DIR));
    assert_eq!(f.snapshot(), after);

    // The copy can then be set up like any other wiki: its own index is kept.
    let setup = plan_setup(&f.root, &request(Some(DEFAULT_DIR), None)).unwrap();
    assert_eq!(actions(&setup)[0], ("index.md", FileAction::Keep));
    assert_eq!(setup.changes(), 5);
}

#[test]
fn a_copy_is_refused_for_overlap_and_blocked_by_links_conflicts_and_colliding_names() {
    let f = Fixture::new();
    f.write("docs/wiki/sub/inner.md", b"inner\n");
    f.write("docs/wiki/conflict.md", b"different\n");
    f.write("docs/wiki/case.md", b"lower\n");
    f.write("docs/wiki/taken", b"a file where the source has a folder\n");
    f.write("docs/wiki/same.md", b"same\n");
    for (source, shown) in [
        ("docs/wiki", "docs/wiki"),
        ("Docs/Wiki", "docs/wiki"),
        ("docs", "docs"),
        ("docs/wiki/sub", "docs/wiki/sub"),
    ] {
        assert_eq!(
            plan_copy(&f.root, &copy_of(source)),
            Err(WikiError::Overlap {
                source: shown.into(),
                destination: DEFAULT_DIR.into()
            }),
            "{source}"
        );
    }
    assert_eq!(
        plan_copy(&f.root, &copy_of("nowhere")),
        Err(WikiError::Missing {
            dir: "nowhere".into()
        })
    );

    f.write("src/main.rs", b"fn main() {}\n");
    f.write(
        "notes/a.md",
        b"# A\n\nSee [the code](../src/main.rs) and [[same]].\n",
    );
    f.write("notes/conflict.md", b"source\n");
    f.write("notes/Case.md", b"upper\n");
    f.write("notes/taken/x.md", b"x\n");
    f.write("notes/same.md", b"same\n");
    fs::write(f.outside.join("secret.md"), b"outside\n").unwrap();
    fs::hard_link(f.outside.join("secret.md"), f.at("notes/hard.md")).unwrap();
    link_folder(&f.outside, &f.at("notes/shared"));
    let before = f.snapshot();
    let req = copy_of("notes");
    let plan = plan_copy(&f.root, &req).unwrap();
    let mut blocked: Vec<(&str, CopyBlock)> = plan
        .blocked
        .iter()
        .map(|entry| (entry.path.as_str(), entry.reason))
        .collect();
    blocked.sort_by_key(|(path, _)| *path);
    assert_eq!(
        blocked,
        [
            ("Case.md", CopyBlock::CaseCollision),
            ("a.md", CopyBlock::LinkLeavesWiki),
            ("conflict.md", CopyBlock::Conflict),
            ("hard.md", CopyBlock::Link),
            ("shared", CopyBlock::Link),
            ("taken", CopyBlock::Conflict),
        ]
    );
    let leaving = plan
        .blocked
        .iter()
        .find(|entry| entry.path == "a.md")
        .unwrap();
    assert_eq!(
        (leaving.line, leaving.target.as_deref()),
        (Some(3), Some("../src/main.rs"))
    );
    assert_eq!(
        plan.files
            .iter()
            .map(|file| (file.path.as_str(), file.action))
            .collect::<Vec<_>>(),
        [("a.md", CopyAction::Copy), ("same.md", CopyAction::Same)]
    );
    let refused = apply_copy(&f.root, &req, &plan.id).unwrap_err();
    assert_eq!(refused.code(), "OAH_WIKI_PLAN_BLOCKED");
    assert_eq!(
        f.snapshot(),
        before,
        "a blocked copy writes nothing and replaces nothing"
    );
    assert!(tree(&f.outside).len() == 1);
}

#[test]
fn a_copy_stops_when_the_source_or_the_destination_changed_after_the_preview() {
    let f = Fixture::new();
    f.write("notes/a.md", b"one\n");
    f.write("notes/sub/b.md", b"two\n");
    fs::create_dir(f.at("docs")).unwrap();
    let req = copy_of("notes");

    let plan = plan_copy(&f.root, &req).unwrap();
    f.write("notes/a.md", b"one, edited after the preview\n");
    let before = f.snapshot();
    assert_eq!(apply_copy(&f.root, &req, &plan.id), Err(WikiError::Stale));
    assert_eq!(f.snapshot(), before);

    let plan = plan_copy(&f.root, &req).unwrap();
    f.write("notes/new.md", b"added after the preview\n");
    assert_eq!(apply_copy(&f.root, &req, &plan.id), Err(WikiError::Stale));

    // A destination file that appears after the preview is kept, and the copy waits for a new plan.
    let plan = plan_copy(&f.root, &req).unwrap();
    f.write("docs/wiki/a.md", b"the user's own file\n");
    assert_eq!(apply_copy(&f.root, &req, &plan.id), Err(WikiError::Stale));
    assert_eq!(
        fs::read(f.at("docs/wiki/a.md")).unwrap(),
        b"the user's own file\n"
    );
    let conflict = plan_copy(&f.root, &req).unwrap();
    assert_eq!(
        conflict
            .blocked
            .iter()
            .map(|entry| (entry.path.as_str(), entry.reason))
            .collect::<Vec<_>>(),
        [("a.md", CopyBlock::Conflict)]
    );
    fs::remove_file(f.at("docs/wiki/a.md")).unwrap();
    fs::remove_dir(f.at("docs/wiki")).unwrap();

    // A source without files has nothing to switch to.
    fs::create_dir(f.at("empty")).unwrap();
    let nothing = plan_copy(&f.root, &copy_of("empty")).unwrap();
    assert_eq!((nothing.files.len(), nothing.folders.len()), (0, 0));
    let report = apply_copy(&f.root, &copy_of("empty"), &nothing.id).unwrap();
    assert_eq!((report.stop, report.config_dir), (None, None));
    assert!(!f.at("docs/wiki").exists());

    // The destination's parent swapped for a link after the preview: refused, and nothing lands outside.
    let plan = plan_copy(&f.root, &req).unwrap();
    fs::remove_dir(f.at("docs")).unwrap();
    link_folder(&f.outside, &f.at("docs"));
    assert_eq!(
        apply_copy(&f.root, &req, &plan.id),
        Err(WikiError::Unsafe {
            path: "docs".into()
        })
    );
    assert!(tree(&f.outside).is_empty());
}

#[cfg(unix)]
#[test]
fn a_copy_that_stopped_part_way_is_finished_by_a_new_plan() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    f.write("notes/a.md", b"one\n");
    f.write("notes/sub/b.md", b"two\n");
    f.write("notes/z.md", b"three\n");
    fs::create_dir_all(f.at("docs/wiki/sub")).unwrap();
    fs::set_permissions(f.at("docs/wiki/sub"), fs::Permissions::from_mode(0o555)).unwrap();
    let req = copy_of("notes");
    let plan = plan_copy(&f.root, &req).unwrap();
    let report = apply_copy(&f.root, &req, &plan.id).unwrap();
    assert_eq!(
        report.stop,
        Some(WikiError::Denied {
            path: "docs/wiki/sub/b.md".into()
        })
    );
    assert_eq!(
        report
            .items
            .iter()
            .map(|item| item.status)
            .collect::<Vec<_>>(),
        [ItemStatus::Created, ItemStatus::Failed, ItemStatus::Pending]
    );
    assert_eq!(
        report.config_dir, None,
        "the configuration is not switched to a partial copy"
    );
    fs::set_permissions(f.at("docs/wiki/sub"), fs::Permissions::from_mode(0o755)).unwrap();

    let rest = plan_copy(&f.root, &req).unwrap();
    assert_eq!(
        rest.files
            .iter()
            .map(|file| file.action)
            .collect::<Vec<_>>(),
        [CopyAction::Same, CopyAction::Copy, CopyAction::Copy]
    );
    let report = apply_copy(&f.root, &req, &rest.id).unwrap();
    assert_eq!(report.stop, None);
    assert_eq!(report.config_dir.as_deref(), Some(DEFAULT_DIR));
    assert_eq!(tree(&f.at("docs/wiki")), tree(&f.at("notes")));
}
