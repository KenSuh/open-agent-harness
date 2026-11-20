// 명령 찾기 시험: 가짜 PATH 폴더에 만든 파일로 절대 폴더만 찾는지, Windows 규칙의 확장자 순서·npm shim·거절을 확인한다.
use super::{Resolution, Resolved, Rule, resolve, resolve_in};
use crate::paths::Env;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

fn folder() -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    (dir, root)
}

fn env_with_path(entries: &[&Path]) -> Env {
    let joined: Vec<String> = entries
        .iter()
        .map(|entry| entry.display().to_string())
        .collect();
    Env::new(std::env::temp_dir())
        .unwrap()
        .with("PATH", joined.join(";"))
}

fn write(path: &Path, text: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn found(argv: &[&Path], source: &Path) -> Resolution {
    Resolution::Found(Resolved {
        argv: argv
            .iter()
            .map(|part| OsString::from(part.as_os_str()))
            .collect(),
        source: source.to_path_buf(),
    })
}

fn unsupported(path: &Path, reason: &str) -> Resolution {
    Resolution::UnsupportedShim {
        path: path.to_path_buf(),
        reason: reason.to_string(),
    }
}

#[cfg(unix)]
mod unix {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn program(path: &Path, mode: u32) {
        write(path, "#!/bin/sh\n");
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
    }

    fn colon_path(entries: &[PathBuf]) -> Env {
        Env::new(std::env::temp_dir())
            .unwrap()
            .with("PATH", std::env::join_paths(entries).unwrap())
    }

    #[test]
    fn the_first_executable_file_on_path_wins() {
        let (_dir, root) = folder();
        program(&root.join("a/tool"), 0o644);
        program(&root.join("b/tool"), 0o755);
        program(&root.join("c/tool"), 0o755);
        let env = colon_path(&[root.join("a"), root.join("b"), root.join("c")]);
        let tool = root.join("b/tool");
        assert_eq!(resolve(&env, "tool", Rule::Unix), found(&[&tool], &tool));
    }

    /// `folder` as a path relative to the test's working folder.
    fn relative_to_cwd(folder: &Path) -> PathBuf {
        let cwd = std::env::current_dir().unwrap();
        let up: PathBuf = cwd.components().skip(1).map(|_| "..").collect();
        up.join(folder.strip_prefix("/").unwrap())
    }

    #[test]
    fn a_relative_path_folder_is_never_searched() {
        let (_dir, root) = folder();
        program(&root.join("bin/tool"), 0o755);
        let relative = relative_to_cwd(&root.join("bin"));
        assert!(
            relative.join("tool").is_file(),
            "the relative entry must reach the program"
        );
        let env = colon_path(&[relative]);
        assert_eq!(resolve(&env, "tool", Rule::Unix), Resolution::Missing);
    }

    #[test]
    fn a_folder_lookup_finds_an_executable_in_that_absolute_folder_only() {
        let (_dir, root) = folder();
        program(&root.join("own/tool"), 0o755);
        program(&root.join("plain/tool"), 0o644);
        let (env, tool) = (colon_path(&[root.join("elsewhere")]), root.join("own/tool"));
        assert_eq!(
            resolve_in(&env, &root.join("own"), "tool", Rule::Unix),
            found(&[&tool], &tool)
        );
        assert_eq!(
            resolve_in(&env, &root.join("plain"), "tool", Rule::Unix),
            Resolution::Missing
        );
        let relative = relative_to_cwd(&root.join("own"));
        assert_eq!(
            resolve_in(&env, &relative, "tool", Rule::Unix),
            Resolution::Missing
        );
    }

    #[test]
    fn a_relative_path_folder_is_never_searched_under_the_windows_rule() {
        let (_dir, root) = folder();
        write(&root.join("bin/tool.exe"), "MZ");
        let relative = relative_to_cwd(&root.join("bin"));
        assert!(
            relative.join("tool.exe").is_file(),
            "the relative entry must reach the program"
        );
        let env = env_with_path(&[&relative]);
        assert_eq!(resolve(&env, "tool", Rule::Windows), Resolution::Missing);
    }
}

#[test]
fn com_and_exe_run_directly_in_pathext_order() {
    let (_dir, root) = folder();
    write(&root.join("tool.cmd"), "@echo off\r\n");
    write(&root.join("tool.exe"), "MZ");
    write(&root.join("tool.com"), "MZ");
    let env = env_with_path(&[&root]).with("PATHEXT", ".EXE;.COM;.BAT;.CMD");
    let exe = root.join("tool.exe");
    assert_eq!(resolve(&env, "tool", Rule::Windows), found(&[&exe], &exe));
}

#[test]
fn names_match_without_regard_to_case_and_report_the_stored_name() {
    let (_dir, root) = folder();
    write(&root.join("Tool.EXE"), "MZ");
    let env = env_with_path(&[Path::new("relative"), &root]);
    let exe = root.join("Tool.EXE");
    assert_eq!(resolve(&env, "tool", Rule::Windows), found(&[&exe], &exe));
}

#[test]
fn a_quoted_path_entry_is_searched_without_its_quotes() {
    let (_dir, root) = folder();
    write(&root.join("tool.exe"), "MZ");
    let env = Env::new(std::env::temp_dir())
        .unwrap()
        .with("PATH", format!("\"{}\"", root.display()));
    let exe = root.join("tool.exe");
    assert_eq!(resolve(&env, "tool", Rule::Windows), found(&[&exe], &exe));
}

#[test]
fn extensions_other_than_com_exe_bat_cmd_are_never_run() {
    let (_dir, root) = folder();
    write(&root.join("tool.ps1"), "Write-Output 1\r\n");
    let env = env_with_path(&[&root]).with("PATHEXT", ".PS1;.EXE");
    assert_eq!(resolve(&env, "tool", Rule::Windows), Resolution::Missing);
}

#[test]
fn a_name_that_already_has_a_runnable_extension_is_tried_as_written() {
    let (_dir, root) = folder();
    write(&root.join("tool.exe"), "MZ");
    write(&root.join("tool.exe.exe"), "MZ");
    let env = env_with_path(&[&root]);
    let exe = root.join("tool.exe");
    assert_eq!(
        resolve(&env, "tool.exe", Rule::Windows),
        found(&[&exe], &exe)
    );
}

#[test]
fn a_folder_lookup_follows_the_windows_rule() {
    let (_dir, root) = folder();
    write(&root.join("agy/bin/agy.exe"), "MZ");
    write(&root.join("cmd/tool.cmd"), "@echo off\r\n");
    let (env, exe) = (env_with_path(&[]), root.join("agy/bin/agy.exe"));
    assert_eq!(
        resolve_in(&env, &root.join("agy/bin"), "agy", Rule::Windows),
        found(&[&exe], &exe)
    );
    let refused = unsupported(
        &root.join("cmd/tool.cmd"),
        "no installed npm package declares this command",
    );
    assert_eq!(
        resolve_in(&env, &root.join("cmd"), "tool", Rule::Windows),
        refused
    );
}

#[path = "resolve_shim_tests.rs"]
mod shim;
