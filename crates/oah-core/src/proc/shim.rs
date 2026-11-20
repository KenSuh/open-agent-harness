// npm shim 바꾸기: 배치 파일을 띄우는 대신 node_modules 에서 그 명령을 선언한 패키지 스크립트 하나와 node.exe 를 찾는다.
//! An npm shim as `node.exe` plus the declared script (v0.4:src/oah/engines/proc.py:113-178). Windows runs a
//! `.cmd`/`.bat` file through `cmd.exe`, which re-parses arguments, so OAH never launches one.

use super::find_windows;
use crate::paths::Env;
use serde_json::Value;
use std::ffi::OsString;
use std::fmt;
use std::path::{Path, PathBuf};

/// Why a batch file cannot be run as `node` plus a script.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ShimError {
    NoPackage,
    SeveralPackages,
    NoNode,
}

impl fmt::Display for ShimError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            ShimError::NoPackage => "no installed npm package declares this command",
            ShimError::SeveralPackages => "several npm packages declare this command",
            ShimError::NoNode => "node.exe was not found for this npm shim",
        })
    }
}

/// `[node.exe, script]` for the shim at `shim`, which was found for the command `name`.
pub(crate) fn npm_command(env: &Env, shim: &Path, name: &str) -> Result<Vec<OsString>, ShimError> {
    let script = bin_script(shim, name)?;
    let node = node_executable(env, shim)?;
    Ok(vec![node.into_os_string(), script.into_os_string()])
}

/// A global install keeps shims beside `node_modules`; a project install keeps them in `node_modules/.bin`.
fn modules_dir(shim: &Path) -> PathBuf {
    let parent = shim.parent().unwrap_or(Path::new(""));
    let is_named = |path: Option<&Path>, wanted: &str| {
        path.and_then(Path::file_name)
            .is_some_and(|name| name.to_string_lossy().eq_ignore_ascii_case(wanted))
    };
    if is_named(Some(parent), ".bin") && is_named(parent.parent(), "node_modules") {
        return parent.parent().unwrap_or(parent).to_path_buf();
    }
    parent.join("node_modules")
}

fn bin_script(shim: &Path, name: &str) -> Result<PathBuf, ShimError> {
    let modules = modules_dir(shim);
    let matches: Vec<PathBuf> = packages(&modules)
        .iter()
        .filter_map(|package| declared_bin(package, &modules, name))
        .collect();
    match matches.as_slice() {
        [] => Err(ShimError::NoPackage),
        [script] => Ok(script.clone()),
        _ => Err(ShimError::SeveralPackages),
    }
}

/// The package folders under `modules`, with each `@scope` folder's packages; hidden folders are skipped.
fn packages(modules: &Path) -> Vec<PathBuf> {
    let mut packages = Vec::new();
    for entry in sorted_dirs(modules) {
        let name = entry
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default();
        if name.starts_with('.') {
            continue;
        }
        if name.starts_with('@') {
            packages.extend(sorted_dirs(&entry));
        } else {
            packages.push(entry);
        }
    }
    packages
}

fn sorted_dirs(folder: &Path) -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = std::fs::read_dir(folder)
        .map(|entries| {
            entries
                .filter_map(Result::ok)
                .map(|entry| entry.path())
                .filter(|p| p.is_dir())
                .collect()
        })
        .unwrap_or_default();
    dirs.sort();
    dirs
}

/// The script `package` declares for `name` in its `package.json` `bin`, when it is a file inside `modules`.
fn declared_bin(package: &Path, modules: &Path, name: &str) -> Option<PathBuf> {
    let text = std::fs::read_to_string(package.join("package.json")).ok()?;
    let Value::Object(manifest) = serde_json::from_str::<Value>(&text).ok()? else {
        return None;
    };
    let relative = match manifest.get("bin")? {
        Value::String(target) => {
            let package_name = match manifest.get("name") {
                Some(Value::String(text)) => text.clone(),
                Some(other) => other.to_string(),
                None => String::new(),
            };
            let command = package_name.rsplit('/').next().unwrap_or_default();
            if command.to_lowercase() != name.to_lowercase() {
                return None;
            }
            target.clone()
        }
        Value::Object(commands) => commands
            .iter()
            .find(|(command, _)| command.to_lowercase() == name.to_lowercase())
            .and_then(|(_, target)| target.as_str().map(str::to_string))?,
        _ => return None,
    };
    // v0.4.0 joined a `PurePosixPath`, which drops empty and `.` parts; a rooted target left the package.
    if relative.starts_with('/') {
        return None;
    }
    let script = relative
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .fold(package.to_path_buf(), |path, part| path.join(part));
    let inside = |script: &Path| {
        let (Ok(script), Ok(modules)) = (script.canonicalize(), modules.canonicalize()) else {
            return false;
        };
        script.starts_with(modules)
    };
    (script.is_file() && inside(&script)).then_some(script)
}

/// `node.exe` beside the shim, else the first `node.exe` on `PATH`.
fn node_executable(env: &Env, shim: &Path) -> Result<PathBuf, ShimError> {
    let local = shim.parent().unwrap_or(Path::new("")).join("node.exe");
    if local.is_file() {
        return Ok(local);
    }
    find_windows(env, "node.exe").ok_or(ShimError::NoNode)
}
