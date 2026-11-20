// 번들 CLI 경로 해석: 실행 파일 옆의 oah[.exe] 를 고르고 개발 빌드에서만 OAH_DEV_CLI 를 허용한다.
//! Where the bundled `oah` CLI is (CONTRACTS C7). The shell finds the path and passes it to
//! `oah_core::app::cli_copy`; core decides what to do with it, including when the file is missing. Nothing here
//! reads, runs or writes the file.

use std::path::{Path, PathBuf};

use oah_core::paths::Env;

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
