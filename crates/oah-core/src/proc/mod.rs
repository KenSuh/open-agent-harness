// 프로세스 실행: 절대 경로 하나를 셸 없이 인자 벡터로 실행하고, 마감 시간과 출력 상한을 넘으면 멈춘다.
//! Runs one program by absolute path, without a shell, with a deadline and an output limit (CONTRACTS C1).
//!
//! Crate-private. The child gets exactly the session's environment (`Env`), no standard input, and piped output.
//! Two short-lived reader threads drain stdout and stderr; the caller waits at most the deadline in total, even
//! when a grandchild keeps the pipes open, and a child that is still running then is killed.

pub(crate) mod resolve;
pub(crate) mod tree;

use crate::paths::Env;
use std::fmt;
use std::io::{self, Read};
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::time::{Duration, Instant};

/// One run: the program's absolute path, its arguments, the whole deadline and the byte limit of each stream.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Run<'a> {
    pub program: &'a Path,
    pub args: &'a [&'a str],
    pub deadline: Duration,
    pub limit: usize,
}

#[derive(Debug)]
pub(crate) struct Output {
    pub status: ExitStatus,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

#[derive(Debug)]
pub(crate) enum ProcError {
    /// The program path is not absolute; nothing was started.
    NotAbsolute,
    /// The program could not be started.
    Spawn(io::Error),
    /// The deadline passed; the child was killed.
    Timeout,
    /// A stream passed the byte limit; the child was killed.
    TooLarge,
    /// Waiting for the child or reading its output failed; the child was killed.
    Wait(io::Error),
}

impl fmt::Display for ProcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ProcError::NotAbsolute => f.write_str("the program path is not absolute"),
            ProcError::Spawn(error) => write!(f, "it could not be started: {error}"),
            ProcError::Timeout => f.write_str("it did not finish in time"),
            ProcError::TooLarge => f.write_str("it printed more than expected"),
            ProcError::Wait(error) => write!(f, "waiting for it failed: {error}"),
        }
    }
}

#[derive(Debug, Clone, Copy)]
enum Stream {
    Out,
    Err,
}

type Drained = (Stream, Result<Vec<u8>, ProcError>);

/// Runs `run.program` with `run.args` and the variables of `env`, and returns its exit status and output.
pub(crate) fn run(env: &Env, run: &Run<'_>) -> Result<Output, ProcError> {
    run_args(env, run.program, run.args, run.deadline, run.limit)
}

/// [`run`] with arguments that need not be UTF-8: an npm shim's script path, or a configured command's own.
pub(crate) fn run_args<A: AsRef<std::ffi::OsStr>>(
    env: &Env,
    program: &Path,
    args: &[A],
    deadline: Duration,
    limit: usize,
) -> Result<Output, ProcError> {
    if !program.is_absolute() {
        return Err(ProcError::NotAbsolute);
    }
    let deadline = Instant::now() + deadline;
    let mut child = Command::new(program)
        .args(args)
        .env_clear()
        .envs(env.vars())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(ProcError::Spawn)?;
    let (sender, receiver) = mpsc::channel();
    let readers = [
        drain(child.stdout.take(), Stream::Out, limit, sender.clone()),
        drain(child.stderr.take(), Stream::Err, limit, sender),
    ];
    if let Some(error) = readers.into_iter().find_map(Result::err) {
        return Err(stop(&mut child, ProcError::Spawn(error)));
    }
    let (mut stdout, mut stderr) = (None, None);
    while stdout.is_none() || stderr.is_none() {
        let left = deadline.saturating_duration_since(Instant::now());
        match receiver.recv_timeout(left) {
            Ok((Stream::Out, Ok(bytes))) => stdout = Some(bytes),
            Ok((Stream::Err, Ok(bytes))) => stderr = Some(bytes),
            Ok((_, Err(error))) => return Err(stop(&mut child, error)),
            Err(RecvTimeoutError::Timeout) => return Err(stop(&mut child, ProcError::Timeout)),
            Err(RecvTimeoutError::Disconnected) => {
                let error = io::Error::other("an output reader stopped");
                return Err(stop(&mut child, ProcError::Wait(error)));
            }
        }
    }
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                return Ok(Output {
                    status,
                    stdout: stdout.unwrap_or_default(),
                    stderr: stderr.unwrap_or_default(),
                });
            }
            Ok(None) if Instant::now() >= deadline => {
                return Err(stop(&mut child, ProcError::Timeout));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(5)),
            Err(error) => return Err(stop(&mut child, ProcError::Wait(error))),
        }
    }
}

fn stop(child: &mut Child, error: ProcError) -> ProcError {
    let _ = child.kill();
    let _ = child.wait();
    error
}

/// Reads one stream on its own thread and sends the bytes, or `TooLarge` as soon as they pass `limit`.
fn drain(
    source: Option<impl Read + Send + 'static>,
    stream: Stream,
    limit: usize,
    sender: Sender<Drained>,
) -> io::Result<()> {
    std::thread::Builder::new()
        .name(String::from("oah-proc-output"))
        .spawn(move || {
            let result = match source {
                Some(mut source) => read_bounded(&mut source, limit),
                None => Ok(Vec::new()),
            };
            let _ = sender.send((stream, result));
        })
        .map(|_| ())
}

fn read_bounded(source: &mut impl Read, limit: usize) -> Result<Vec<u8>, ProcError> {
    let mut out = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let count = match source.read(&mut chunk) {
            Ok(0) => return Ok(out),
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(ProcError::Wait(error)),
        };
        if out.len() + count > limit {
            return Err(ProcError::TooLarge);
        }
        out.extend_from_slice(&chunk[..count]);
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn env() -> Env {
        let mut env = Env::new(std::env::temp_dir()).unwrap();
        env.set("PATH", "/usr/bin:/bin");
        env
    }

    fn sh(script: &str, deadline: Duration, limit: usize) -> Result<Output, ProcError> {
        let program = PathBuf::from("/bin/sh");
        run(
            &env(),
            &Run {
                program: &program,
                args: &["-c", script],
                deadline,
                limit,
            },
        )
    }

    #[test]
    fn a_finished_run_returns_its_status_and_both_streams() {
        let output = sh(
            "echo out; echo err >&2; exit 3",
            Duration::from_secs(10),
            64,
        )
        .unwrap();
        assert_eq!(output.status.code(), Some(3));
        assert_eq!(output.stdout, b"out\n");
        assert_eq!(output.stderr, b"err\n");
    }

    #[test]
    fn the_child_sees_only_the_session_environment() {
        let mut env = env();
        env.set("OAH_SAMPLE", "from-env");
        let program = PathBuf::from("/bin/sh");
        let output = run(
            &env,
            &Run {
                program: &program,
                args: &["-c", "echo \"$OAH_SAMPLE:${CARGO:-unset}\""],
                deadline: Duration::from_secs(10),
                limit: 64,
            },
        )
        .unwrap();
        assert_eq!(output.stdout, b"from-env:unset\n");
    }

    #[test]
    fn a_relative_program_is_refused_before_anything_starts() {
        let program = PathBuf::from("sh");
        let refused = run(
            &env(),
            &Run {
                program: &program,
                args: &["-c", "exit 0"],
                deadline: Duration::from_secs(1),
                limit: 64,
            },
        );
        assert!(matches!(refused, Err(ProcError::NotAbsolute)));
    }

    #[test]
    fn a_missing_program_is_a_spawn_failure() {
        let program = PathBuf::from("/nonexistent-oah-sample/oah");
        let failed = run(
            &env(),
            &Run {
                program: &program,
                args: &[],
                deadline: Duration::from_secs(1),
                limit: 64,
            },
        );
        assert!(matches!(failed, Err(ProcError::Spawn(_))));
    }

    #[test]
    fn a_run_past_its_deadline_is_killed_promptly() {
        let started = Instant::now();
        let failed = sh("sleep 5", Duration::from_millis(200), 64);
        assert!(matches!(failed, Err(ProcError::Timeout)), "{failed:?}");
        assert!(started.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn a_grandchild_holding_the_pipes_does_not_extend_the_deadline() {
        let started = Instant::now();
        let failed = sh("sleep 5 & echo started", Duration::from_millis(300), 64);
        assert!(matches!(failed, Err(ProcError::Timeout)), "{failed:?}");
        assert!(started.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn output_past_the_limit_stops_the_run() {
        let started = Instant::now();
        let failed = sh(
            "while :; do echo 0123456789; done",
            Duration::from_secs(10),
            1024,
        );
        assert!(matches!(failed, Err(ProcError::TooLarge)), "{failed:?}");
        assert!(started.elapsed() < Duration::from_secs(5));
    }
}
