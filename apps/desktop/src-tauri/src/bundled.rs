// 번들 CLI 경로 해석: 실행 파일 옆의 oah[.exe] 를 고르고 개발 빌드에서만 OAH_DEV_CLI 를 허용한다.
//! Where the bundled `oah` CLI is (CONTRACTS C7). The shell finds the path and passes it to
//! `oah_core::app::cli_copy`; core decides what to do with it, including when the file is missing. Nothing here
//! reads, runs or writes the file.
//!
//! The project-tools runner is found the same way, beside the executable ([`project_tools_source`]), and handed
//! to `oah_core::project_tools::runner::ProjectTools`, the only code that starts it.

use std::path::{Path, PathBuf};

use oah_core::paths::Env;
use oah_core::project_tools::runner;

/// The sidecar's file name after the bundler stripped the target triple.
pub const SIDECAR_NAME: &str = if cfg!(windows) { "oah.exe" } else { "oah" };

/// The variable `tauri dev` and tests use to name a CLI when none sits beside the executable.
pub const DEV_CLI_VAR: &str = "OAH_DEV_CLI";

/// The bundled CLI beside the executable, with an environment fallback in debug development builds.
pub fn cli_source(env: &Env) -> PathBuf {
    let beside = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|folder| folder.join(SIDECAR_NAME)));
    choose(beside, dev_cli(env), |path| path.is_file())
}

fn dev_cli(env: &Env) -> Option<PathBuf> {
    if cfg!(debug_assertions) && tauri::is_dev() {
        env.var(DEV_CLI_VAR)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
    } else {
        None
    }
}

/// The project-tools runner beside the executable, chosen like the CLI: the file named `runner::SIDECAR_NAME` in
/// the folder of the app's own executable. Never a path from the project, the environment or `PATH`. When this
/// install has no runner the path beside the executable is still returned, and
/// `oah_core::project_tools::runner::ProjectTools` reports the tools as unavailable without starting anything.
pub fn project_tools_source() -> PathBuf {
    let beside = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|folder| folder.join(runner::SIDECAR_NAME)));
    choose(beside, staged_project_tools(), |path| path.is_file())
}

/// A macOS development run has no runner beside its executable: the bundler places it only inside the app
/// bundle. A debug development build therefore also looks where `scripts/stage-cli.mjs` stages it, a path fixed
/// when this crate is compiled and never read from the environment. No other build contains that path.
#[cfg(all(debug_assertions, target_os = "macos"))]
fn staged_project_tools() -> Option<PathBuf> {
    tauri::is_dev().then(|| {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join("oah-project-tools-macos")
    })
}

#[cfg(not(all(debug_assertions, target_os = "macos")))]
fn staged_project_tools() -> Option<PathBuf> {
    None
}

/// The first existing absolute candidate, beside the executable first. When none exists, the path beside the
/// executable (or the development path) is still returned, so core reports that exact file as not found. A
/// relative candidate is never used: it would depend on the folder the app was started from.
fn choose(
    beside: Option<PathBuf>,
    dev: Option<PathBuf>,
    is_file: impl Fn(&Path) -> bool,
) -> PathBuf {
    let candidates: Vec<PathBuf> = [beside, dev]
        .into_iter()
        .flatten()
        .filter(|path| path.is_absolute())
        .collect();
    candidates
        .iter()
        .find(|path| is_file(path))
        .or_else(|| candidates.first())
        .cloned()
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn abs(name: &str) -> PathBuf {
        std::env::temp_dir().join("oah-bundled-test").join(name)
    }

    #[test]
    fn the_file_beside_the_executable_comes_first() {
        let chosen = choose(Some(abs("app/oah")), Some(abs("dev/oah")), |_| true);
        assert_eq!(chosen, abs("app/oah"));
    }

    #[test]
    fn the_development_cli_is_used_when_nothing_is_beside_the_executable() {
        let dev = abs("dev/oah");
        let chosen = choose(Some(abs("app/oah")), Some(dev.clone()), |path| path == dev);
        assert_eq!(chosen, dev);
    }

    #[test]
    fn a_missing_cli_is_reported_as_the_path_beside_the_executable() {
        let chosen = choose(Some(abs("app/oah")), Some(abs("dev/oah")), |_| false);
        assert_eq!(chosen, abs("app/oah"));
    }

    #[test]
    fn relative_candidates_are_never_used() {
        let chosen = choose(None, Some(PathBuf::from("oah")), |_| true);
        assert_eq!(chosen, PathBuf::new());
    }

    #[test]
    fn only_a_debug_development_build_on_macos_looks_at_the_staged_project_tools() {
        let staged = staged_project_tools();
        if cfg!(all(debug_assertions, target_os = "macos")) && tauri::is_dev() {
            let staged = staged.unwrap();
            assert!(staged.is_absolute());
            assert!(staged.ends_with("binaries/oah-project-tools-macos"));
        } else {
            assert_eq!(staged, None);
        }
    }

    #[test]
    fn only_a_debug_development_build_reads_the_environment_fallback() {
        let mut env = Env::new(std::env::temp_dir()).unwrap();
        let fallback = abs("dev/oah");
        env.set(DEV_CLI_VAR, &fallback);

        if cfg!(debug_assertions) && tauri::is_dev() {
            assert_eq!(dev_cli(&env), Some(fallback));
        } else {
            assert_eq!(dev_cli(&env), None);
        }
    }
}
