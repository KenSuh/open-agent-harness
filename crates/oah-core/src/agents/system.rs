// 실제 확인 통로: 세션 환경의 PATH 에서 셸 없이 명령을 찾고 마감 시간 안에 실행하며, 설정한 명령은 PATH 찾기를 대신한다.
//! The production [`Probe`]: `proc`'s shell-free lookup and bounded run on the session's environment, and
//! [`Configured`], which puts a project's `[agents.<id>] command` in place of the `PATH` lookup
//! (v0.4:src/oah/models.py:203-218).

use super::capability::Os;
use super::probe::{Probe, Resolution, Resolved, RunError, RunOutput};
use crate::paths::Env;
use crate::proc::resolve::{Rule, resolve, resolve_in};
use crate::proc::{self, ProcError};
use std::ffi::{OsStr, OsString};
use std::path::Path;
use std::time::Duration;

/// The most a version or status run may print on one stream.
const OUTPUT_LIMIT: usize = 256 * 1024;

/// Looks commands up on the `PATH` of `env` with this system's rule and runs them without a shell.
#[derive(Debug, Clone, Copy)]
pub struct SystemProbe<'a> {
    env: &'a Env,
}

impl<'a> SystemProbe<'a> {
    pub fn new(env: &'a Env) -> SystemProbe<'a> {
        SystemProbe { env }
    }
}

impl Probe for SystemProbe<'_> {
    fn env(&self) -> &Env {
        self.env
    }

    fn os(&self) -> Os {
        Os::host()
    }

    fn resolve(&self, command: &str) -> Resolution {
        resolve(self.env, command, Rule::host())
    }

    fn resolve_in(&self, command: &str, folder: &Path) -> Resolution {
        resolve_in(self.env, folder, command, Rule::host())
    }

    fn run(
        &self,
        program: &Resolved,
        args: &[&str],
        deadline: Duration,
    ) -> Result<RunOutput, RunError> {
        let Some((first, fixed)) = program.argv.split_first() else {
            return Err(RunError::Launch(String::from("there is no program to run")));
        };
        let all: Vec<&OsStr> = fixed
            .iter()
            .map(OsString::as_os_str)
            .chain(args.iter().map(OsStr::new))
            .collect();
        match proc::run_args(self.env, Path::new(first), &all, deadline, OUTPUT_LIMIT) {
            Ok(output) => Ok(RunOutput {
                exit: output.status.code(),
                stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
                stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            }),
            Err(ProcError::Timeout) => Err(RunError::TimedOut),
            Err(ProcError::Spawn(error)) => Err(RunError::Launch(error.to_string())),
            Err(error) => Err(RunError::Launch(error.to_string())),
        }
    }

    fn exists(&self, path: &Path) -> bool {
        path.exists()
    }
}

/// A probe whose lookup of one agent's command name returns the project's configured command instead. A first
/// element with a `/` or `\` is a path, used as written (relative to the invocation folder); a bare name is looked
/// up on `PATH`. The rest of the command goes before every run's own arguments. Everything else is `inner`'s.
pub struct Configured<'a> {
    pub inner: &'a dyn Probe,
    /// The agent's own command name, which the configured command replaces.
    pub name: &'a str,
    pub command: &'a [String],
}

impl Probe for Configured<'_> {
    fn env(&self) -> &Env {
        self.inner.env()
    }

    fn os(&self) -> Os {
        self.inner.os()
    }

    fn resolve_in(&self, command: &str, folder: &Path) -> Resolution {
        self.inner.resolve_in(command, folder)
    }

    fn resolve(&self, command: &str) -> Resolution {
        let Some((first, rest)) = self.command.split_first().filter(|_| command == self.name)
        else {
            return self.inner.resolve(command);
        };
        let resolution = if first.contains(['/', '\\']) {
            literal(self.env(), first)
        } else {
            self.inner.resolve(first)
        };
        match resolution {
            Resolution::Found(mut program) => {
                program.argv.extend(rest.iter().map(OsString::from));
                Resolution::Found(program)
            }
            other => other,
        }
    }

    fn run(
        &self,
        program: &Resolved,
        args: &[&str],
        deadline: Duration,
    ) -> Result<RunOutput, RunError> {
        self.inner.run(program, args, deadline)
    }

    fn exists(&self, path: &Path) -> bool {
        self.inner.exists(path)
    }
}

/// A configured path, made absolute against the invocation folder. A batch file is never launched, even when
/// configured, on any system (v0.4:src/oah/engines/proc.py `run_command`).
fn literal(env: &Env, first: &str) -> Resolution {
    let path = env.cwd().join(first);
    let lower = first.to_lowercase();
    if lower.ends_with(".bat") || lower.ends_with(".cmd") {
        return Resolution::UnsupportedShim {
            path,
            reason: String::from("batch files are never launched directly"),
        };
    }
    Resolution::Found(Resolved {
        argv: vec![path.clone().into_os_string()],
        source: path,
    })
}

#[cfg(test)]
#[path = "system_tests.rs"]
mod tests;
