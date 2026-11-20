// 가짜 에이전트 확인 통로: 이름별 찾기 결과와 명령줄별 실행 결과를 미리 적어 두고, 실제로 묻거나 실행한 것을 순서대로 기록한다.
#![allow(dead_code)]

use oah_core::agents::{Os, Probe, Resolution, Resolved, RunError, RunOutput};
use oah_core::paths::Env;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

/// A synthetic text a status command prints; it must never come back out of core.
pub const STATUS_SENTINEL: &str = "status-text-sentinel-7f3a";

/// Answers lookups, runs and bundle checks from what a test wrote down; everything else is missing, fails to
/// launch or does not exist. Every question is recorded in `asked`.
pub struct FakeProbe {
    env: Env,
    os: Os,
    found: Vec<(String, Resolution)>,
    located: Vec<(String, PathBuf, Resolution)>,
    runs: Vec<(String, Result<RunOutput, RunError>)>,
    bundles: Vec<PathBuf>,
    asked: Mutex<Vec<String>>,
}

impl FakeProbe {
    pub fn new(os: Os) -> FakeProbe {
        let root = std::env::temp_dir().join("fake-agents");
        let env = Env::new(root.join("cwd"))
            .unwrap()
            .with("HOME", root.join("home"))
            .with("USERPROFILE", root.join("home"));
        FakeProbe {
            env,
            os,
            found: Vec::new(),
            located: Vec::new(),
            runs: Vec::new(),
            bundles: Vec::new(),
            asked: Mutex::new(Vec::new()),
        }
    }

    pub fn env(mut self, name: &str, value: &str) -> FakeProbe {
        self.env.set(name, value);
        self
    }

    pub fn home(&self) -> PathBuf {
        std::env::temp_dir().join("fake-agents").join("home")
    }

    /// `name` resolves to the program at `path`.
    pub fn program(mut self, name: &str, path: &str) -> FakeProbe {
        self.found.push((name.to_string(), found(path, &[])));
        self
    }

    pub fn resolves(mut self, name: &str, resolution: Resolution) -> FakeProbe {
        self.found.push((name.to_string(), resolution));
        self
    }

    /// `name` resolves to `path` when looked up in `folder` alone.
    pub fn located(mut self, name: &str, folder: &Path, path: &str) -> FakeProbe {
        self.located
            .push((name.to_string(), folder.to_path_buf(), found(path, &[])));
        self
    }

    /// Running the argument vector `line` (program and arguments, joined by spaces) gives `exit` and the texts.
    pub fn run(mut self, line: &str, exit: Option<i32>, stdout: &str, stderr: &str) -> FakeProbe {
        let output = RunOutput {
            exit,
            stdout: stdout.to_string(),
            stderr: stderr.to_string(),
        };
        self.runs.push((line.to_string(), Ok(output)));
        self
    }

    pub fn run_fails(mut self, line: &str, error: RunError) -> FakeProbe {
        self.runs.push((line.to_string(), Err(error)));
        self
    }

    pub fn bundle(mut self, path: &str) -> FakeProbe {
        self.bundles.push(PathBuf::from(path));
        self
    }

    pub fn asked(&self) -> Vec<String> {
        self.asked.lock().unwrap().clone()
    }

    /// The command lines that were run.
    pub fn ran(&self) -> Vec<String> {
        self.asked()
            .into_iter()
            .filter_map(|entry| entry.strip_prefix("run ").map(str::to_string))
            .collect()
    }

    fn note(&self, entry: String) {
        self.asked.lock().unwrap().push(entry);
    }
}

pub fn found(path: &str, extra: &[&str]) -> Resolution {
    let mut argv = vec![OsString::from(path)];
    argv.extend(extra.iter().map(OsString::from));
    Resolution::Found(Resolved {
        argv,
        source: PathBuf::from(path),
    })
}

impl Probe for FakeProbe {
    fn env(&self) -> &Env {
        &self.env
    }

    fn os(&self) -> Os {
        self.os
    }

    fn resolve(&self, command: &str) -> Resolution {
        self.note(format!("resolve {command}"));
        let answer = self.found.iter().find(|(name, _)| name == command);
        answer.map_or(Resolution::Missing, |(_, resolution)| resolution.clone())
    }

    fn resolve_in(&self, command: &str, folder: &Path) -> Resolution {
        self.note(format!("resolve {command} in {}", folder.display()));
        let answer = self
            .located
            .iter()
            .find(|(name, place, _)| name == command && place == folder);
        answer.map_or(Resolution::Missing, |(_, _, resolution)| resolution.clone())
    }

    fn run(
        &self,
        program: &Resolved,
        args: &[&str],
        _deadline: Duration,
    ) -> Result<RunOutput, RunError> {
        let mut parts: Vec<String> = program
            .argv
            .iter()
            .map(|part| part.to_string_lossy().into_owned())
            .collect();
        parts.extend(args.iter().map(|arg| arg.to_string()));
        let line = parts.join(" ");
        self.note(format!("run {line}"));
        let answer = self.runs.iter().find(|(known, _)| *known == line);
        answer.map_or(
            Err(RunError::Launch(String::from("not scripted"))),
            |(_, result)| result.clone(),
        )
    }

    fn exists(&self, path: &Path) -> bool {
        self.note(format!("exists {}", path.display()));
        self.bundles.iter().any(|bundle| bundle == path)
    }
}
