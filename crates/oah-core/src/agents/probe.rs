// 확인 통로: 어댑터가 디스크와 프로세스를 직접 건드리지 않도록 명령 찾기·실행·경로 확인을 한 트레이트로 받는다.
//! The [`Probe`] seam: everything detection and sign-in may touch (CONTRACTS C8).

pub use crate::proc::resolve::{Resolution, Resolved};

use super::capability::Os;
use crate::paths::Env;
use std::fmt;
use std::path::Path;
use std::time::Duration;

/// What detection may do: look a command up, run a resolved program, and ask whether a path exists. Production
/// uses [`SystemProbe`](super::SystemProbe); tests give captured answers or a fake home with fake programs.
pub trait Probe: Sync {
    /// The environment lookups use: the home folder, `PATH`, `LOCALAPPDATA`.
    fn env(&self) -> &Env;
    /// The system whose install locations and lookup rule apply.
    fn os(&self) -> Os;
    /// Looks `command` up the way v0.4.0 did (v0.4:src/oah/engines/proc.py); never through a shell.
    fn resolve(&self, command: &str) -> Resolution;
    /// Looks `command` up in the absolute `folder` alone, with the same rules: a documented install location.
    fn resolve_in(&self, command: &str, folder: &Path) -> Resolution;
    /// Runs `program` followed by `args`, stopping it at `deadline`.
    fn run(
        &self,
        program: &Resolved,
        args: &[&str],
        deadline: Duration,
    ) -> Result<RunOutput, RunError>;
    /// Whether `path` exists: an application bundle or a documented install location.
    fn exists(&self, path: &Path) -> bool;
}

/// What a finished run printed. `Debug` shows only the exit code and the sizes, so a status command's text never
/// reaches a log through it.
#[derive(Clone, PartialEq, Eq)]
pub struct RunOutput {
    /// `None` when the program ended without an exit code (a signal).
    pub exit: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

impl fmt::Debug for RunOutput {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RunOutput")
            .field("exit", &self.exit)
            .field("stdout_bytes", &self.stdout.len())
            .field("stderr_bytes", &self.stderr.len())
            .finish()
    }
}

/// Why a run produced no output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunError {
    /// The program could not be started or read; the reason as the system gave it.
    Launch(String),
    /// The deadline passed and the program was stopped.
    TimedOut,
}
