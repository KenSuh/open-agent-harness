// 명령 찾기: PATH 의 절대 폴더만 보고, Windows 에서는 .exe·.com 만 바로 실행하며 배치 파일은 npm shim 일 때만 node 로 바꾼다.
//! Command lookup on `PATH` without a shell (CONTRACTS C8, v0.4:src/oah/engines/proc.py:57-110).
//!
//! Only absolute `PATH` folders are searched. On Unix the first executable file wins. On Windows each folder is
//! tried with the `PATHEXT` extensions in order, of which only `.com` and `.exe` run directly; a `.bat` or `.cmd`
//! file is never launched: an npm shim becomes `node.exe` plus the package's declared script
//! ([`shim`]), anything else is [`Resolution::UnsupportedShim`].

#[path = "shim.rs"]
mod shim;

use crate::paths::Env;
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};

/// How a command name resolved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Resolution {
    Found(Resolved),
    Missing,
    /// A Windows batch file that is not an npm shim OAH can turn into `node` plus its script; never launched.
    UnsupportedShim {
        path: PathBuf,
        reason: String,
    },
}

/// A runnable program.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolved {
    /// The program, then the arguments every run starts with: an npm shim's script, or the rest of a configured
    /// command.
    pub argv: Vec<OsString>,
    /// The file found, as reported.
    pub source: PathBuf,
}

/// Which system's lookup rule applies. A parameter, so the Windows rule is tested on every system.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rule {
    Unix,
    Windows,
}

impl Rule {
    pub(crate) fn host() -> Rule {
        if cfg!(windows) {
            Rule::Windows
        } else {
            Rule::Unix
        }
    }
}

/// The extensions v0.4.0 assumed when `PATHEXT` is not set.
const DEFAULT_PATHEXT: &str = ".COM;.EXE;.BAT;.CMD";
const DIRECT: [&str; 2] = [".com", ".exe"];
const BATCH: [&str; 2] = [".bat", ".cmd"];

/// Looks `name` up on the `PATH` of `env` under `rule`.
pub fn resolve(env: &Env, name: &str, rule: Rule) -> Resolution {
    let found = match rule {
        Rule::Unix => find_unix(env, name),
        Rule::Windows => find_windows(env, name),
    };
    classify(env, found, name, rule)
}

/// Looks `name` up in the absolute `folder` alone, with the rules of a `PATH` lookup: a documented install
/// location outside `PATH`.
pub fn resolve_in(env: &Env, folder: &Path, name: &str, rule: Rule) -> Resolution {
    let found = match rule {
        _ if !folder.is_absolute() => None,
        Rule::Unix => Some(folder.join(name)).filter(|candidate| is_executable(candidate)),
        Rule::Windows => find_in(folder, &windows_names(env, name)),
    };
    classify(env, found, name, rule)
}

/// The program for a found file: a Windows batch file becomes `node` plus the npm script, or is refused.
fn classify(env: &Env, found: Option<PathBuf>, name: &str, rule: Rule) -> Resolution {
    let Some(found) = found else {
        return Resolution::Missing;
    };
    if rule == Rule::Windows && has_suffix(&found, &BATCH) {
        return match shim::npm_command(env, &found, name) {
            Ok(argv) => Resolution::Found(Resolved {
                argv,
                source: found,
            }),
            Err(reason) => Resolution::UnsupportedShim {
                path: found,
                reason: reason.to_string(),
            },
        };
    }
    Resolution::Found(Resolved {
        argv: vec![found.clone().into_os_string()],
        source: found,
    })
}

fn find_unix(env: &Env, name: &str) -> Option<PathBuf> {
    let path = env.var("PATH")?;
    std::env::split_paths(path)
        .filter(|folder| folder.is_absolute())
        .map(|folder| folder.join(name))
        .find(|candidate| is_executable(candidate))
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
}

#[cfg(not(unix))]
fn is_executable(path: &Path) -> bool {
    path.is_file()
}

/// v0.4.0's Windows search: per absolute folder, the `PATHEXT` names in order, matched without regard to case.
pub(crate) fn find_windows(env: &Env, name: &str) -> Option<PathBuf> {
    let names = windows_names(env, name);
    let path = env.var("PATH")?.to_string_lossy().into_owned();
    path.split(';')
        .map(|entry| {
            entry
                .strip_prefix('"')
                .and_then(|e| e.strip_suffix('"'))
                .unwrap_or(entry)
        })
        .map(Path::new)
        .filter(|folder| folder.is_absolute())
        .find_map(|folder| find_in(folder, &names))
}

/// The file names to try for `name`: itself when it already ends with a runnable extension, else `name` plus each
/// `PATHEXT` extension that OAH may run or map (`.com`, `.exe`, `.bat`, `.cmd`), in `PATHEXT` order.
fn windows_names(env: &Env, name: &str) -> Vec<String> {
    let pathext = env.var("PATHEXT").map(OsStr::to_string_lossy);
    let pathext = pathext.as_deref().unwrap_or(DEFAULT_PATHEXT);
    let extensions: Vec<&str> = pathext
        .split(';')
        .filter(|ext| ext.starts_with('.') && !ext.contains(['/', '\\']))
        .filter(|ext| {
            DIRECT
                .iter()
                .chain(&BATCH)
                .any(|allowed| ext.eq_ignore_ascii_case(allowed))
        })
        .collect();
    let lower = name.to_lowercase();
    if extensions
        .iter()
        .any(|ext| lower.ends_with(&ext.to_lowercase()))
    {
        return vec![name.to_string()];
    }
    extensions
        .iter()
        .map(|ext| format!("{name}{ext}"))
        .collect()
}

/// The first of `names` present in `folder` as a file, compared without regard to case, with its name as stored.
fn find_in(folder: &Path, names: &[String]) -> Option<PathBuf> {
    let entries: Vec<(String, PathBuf)> = std::fs::read_dir(folder)
        .ok()?
        .filter_map(Result::ok)
        .map(|entry| {
            (
                entry.file_name().to_string_lossy().to_lowercase(),
                entry.path(),
            )
        })
        .collect();
    names.iter().find_map(|name| {
        let wanted = name.to_lowercase();
        entries
            .iter()
            .find(|(stored, path)| *stored == wanted && path.is_file())
            .map(|(_, path)| path.clone())
    })
}

fn has_suffix(path: &Path, suffixes: &[&str]) -> bool {
    let name = path
        .file_name()
        .map(OsStr::to_string_lossy)
        .unwrap_or_default()
        .to_lowercase();
    suffixes.iter().any(|suffix| name.ends_with(suffix))
}

#[cfg(test)]
#[path = "resolve_tests.rs"]
mod tests;
