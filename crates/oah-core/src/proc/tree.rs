// 프로세스 트리 실행: 프로그램 하나를 자기 프로세스 그룹(Unix)이나 Job Object(Windows) 안에서 돌리고, 마감·취소·출력 상한에 걸리면 그 트리 전체를 끝낸다.
//! Runs one program whose whole process tree ends with the run.
//!
//! [`run`](super::run) stops only the program it started. This is for a tool that starts programs of its own: it
//! runs as the leader of a new process group (Unix) or inside a Job Object (Windows), so stopping it reaches what
//! it started as well. One loop on the calling thread reads both streams without blocking and looks at the
//! deadline and the cancellation every [`TICK`]. There is no reader thread to leave behind, and a grandchild that
//! keeps a pipe open cannot delay the return.
//!
//! A run ends in one of two ways. The program exits: its output is returned, and whatever it left in its group or
//! job is killed. Or the run is stopped, because it was cancelled, passed its deadline or passed an output limit:
//! on Unix the group is first asked to end (`SIGTERM`) and gets `grace` to do so, then it is killed; Windows has
//! no such request, so the job is terminated at once. Either way nothing of the tree is left when this returns,
//! with one gap on Unix: a program the tool started in a session of its own is outside the group, and ends only
//! if the tool ends it when asked. A Job Object has no such gap.

#[cfg(unix)]
mod unix;
#[cfg(windows)]
mod windows;

#[cfg(unix)]
use unix as os;
#[cfg(windows)]
use windows as os;

use super::Output;
// Named through the library, not `crate`: the review-core test harness mounts this module by path.
use oah_core::tasks::CancelToken;
use os::Source;
use std::ffi::{OsStr, OsString};
use std::io;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// How often the loop looks at the streams, the deadline and the cancellation.
const TICK: Duration = Duration::from_millis(10);
/// The most one look drops from a stream of a tree that is being stopped.
const DISCARDED_PER_TICK: usize = 64;

/// One run: the program's absolute path, its arguments, its whole environment and its folder, the deadline, the
/// byte limit of each stream, and how long a stopped tree may take to end by itself before it is killed.
#[derive(Debug, Clone, Copy)]
pub(crate) struct TreeRun<'a> {
    pub program: &'a Path,
    pub args: &'a [&'a OsStr],
    pub env: &'a [(OsString, OsString)],
    pub cwd: &'a Path,
    pub deadline: Duration,
    pub stdout_limit: usize,
    pub stderr_limit: usize,
    pub grace: Duration,
}

#[derive(Debug)]
pub(crate) enum TreeError {
    /// The program path is not absolute; nothing was started.
    NotAbsolute,
    /// The program, or the group or job that holds it, could not be started; nothing is left running.
    Spawn(io::Error),
    /// The cancellation was requested while the tree ran; the tree was stopped.
    Cancelled,
    /// The deadline passed; the tree was stopped.
    Timeout,
    /// A stream passed its byte limit; the tree was stopped.
    TooLarge,
    /// Watching the program or reading its output failed; the tree was stopped.
    Wait(io::Error),
}

/// What one non-blocking read found.
pub(super) enum Ready {
    Data(usize),
    Nothing,
    End,
}

/// Runs `run.program` to its end and returns its exit status and output, or stops its tree and says why.
///
/// The child sees exactly `run.env`, no standard input and `run.cwd` as its folder. A program that has ended
/// wins over a cancellation or a deadline noticed in the same look: its complete output is the answer.
pub(crate) fn run_tree(run: &TreeRun<'_>, cancel: &CancelToken) -> Result<Output, TreeError> {
    run_tree_input(run, cancel, &[])
}

pub(crate) fn run_tree_input(
    run: &TreeRun<'_>,
    cancel: &CancelToken,
    input: &[u8],
) -> Result<Output, TreeError> {
    if !run.program.is_absolute() {
        return Err(TreeError::NotAbsolute);
    }
    let deadline = Instant::now() + run.deadline;
    let mut command = Command::new(run.program);
    command
        .args(run.args)
        .env_clear()
        .envs(run.env.iter().map(|(name, value)| (name, value)))
        .current_dir(run.cwd)
        .stdin(if input.is_empty() {
            Stdio::null()
        } else {
            Stdio::piped()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut tree = os::Tree::spawn(&mut command).map_err(TreeError::Spawn)?;
    let mut stdout = Pipe::new(tree.take_stdout(), run.stdout_limit);
    let mut stderr = Pipe::new(tree.take_stderr(), run.stderr_limit);
    std::thread::scope(|scope| {
        let writer = tree
            .take_stdin()
            .map(|mut pipe| scope.spawn(move || pipe.write_all(input)));
        let output = finish_run(tree, run, &mut stdout, &mut stderr, deadline, cancel)?;
        if let Some(writer) = writer {
            match writer.join() {
                Ok(Ok(())) => {}
                Ok(Err(error))
                    if error.kind() == io::ErrorKind::BrokenPipe && !output.status.success() => {}
                Ok(Err(error)) => return Err(TreeError::Wait(error)),
                Err(_) => {
                    return Err(TreeError::Wait(io::Error::other(
                        "the input writer stopped",
                    )));
                }
            }
        }
        Ok(output)
    })
}

fn finish_run(
    mut tree: os::Tree,
    run: &TreeRun<'_>,
    stdout: &mut Pipe<impl Source>,
    stderr: &mut Pipe<impl Source>,
    deadline: Instant,
    cancel: &CancelToken,
) -> Result<Output, TreeError> {
    match watch(&mut tree, stdout, stderr, deadline, cancel) {
        Ok(()) => {
            let status = tree.finish().map_err(TreeError::Wait)?;
            Ok(Output {
                status,
                stdout: std::mem::take(&mut stdout.bytes),
                stderr: std::mem::take(&mut stderr.bytes),
            })
        }
        Err(error) => {
            if tree.ask() {
                let until = Instant::now() + run.grace;
                while Instant::now() < until && !tree.exited().unwrap_or(true) {
                    stdout.discard();
                    stderr.discard();
                    std::thread::sleep(TICK);
                }
            }
            // Dropping the tree kills what is left of it and waits for the program.
            drop(tree);
            Err(error)
        }
    }
}

/// Reads both streams until the program has ended. An error is why the run has to be stopped instead.
fn watch(
    tree: &mut os::Tree,
    stdout: &mut Pipe<impl Source>,
    stderr: &mut Pipe<impl Source>,
    deadline: Instant,
    cancel: &CancelToken,
) -> Result<(), TreeError> {
    loop {
        // Asked before the streams are read: what the program wrote before it ended is then already waiting.
        let exited = tree.exited().map_err(TreeError::Wait)?;
        stdout.pump()?;
        stderr.pump()?;
        if exited {
            return Ok(());
        }
        if cancel.is_cancelled() {
            return Err(TreeError::Cancelled);
        }
        if Instant::now() >= deadline {
            return Err(TreeError::Timeout);
        }
        std::thread::sleep(TICK);
    }
}

/// One output stream of the program: what was read so far, and the most it may be.
struct Pipe<S> {
    source: Option<S>,
    bytes: Vec<u8>,
    limit: usize,
}

impl<S: Source> Pipe<S> {
    fn new(source: Option<S>, limit: usize) -> Pipe<S> {
        Pipe {
            source,
            bytes: Vec::new(),
            limit,
        }
    }

    /// Moves everything that is waiting into `bytes` without blocking; `TooLarge` once that passes the limit.
    fn pump(&mut self) -> Result<(), TreeError> {
        let mut chunk = [0u8; 8192];
        while let Some(source) = self.source.as_mut() {
            match source.read_ready(&mut chunk).map_err(TreeError::Wait)? {
                Ready::Data(count) if self.bytes.len() + count > self.limit => {
                    return Err(TreeError::TooLarge);
                }
                Ready::Data(count) => self.bytes.extend_from_slice(&chunk[..count]),
                Ready::Nothing => break,
                Ready::End => self.source = None,
            }
        }
        Ok(())
    }

    /// Reads and drops what is waiting, so a program that is ending is not held up by a full pipe.
    fn discard(&mut self) {
        let mut chunk = [0u8; 8192];
        for _ in 0..DISCARDED_PER_TICK {
            let Some(source) = self.source.as_mut() else {
                return;
            };
            if !matches!(source.read_ready(&mut chunk), Ok(Ready::Data(_))) {
                return;
            }
        }
    }
}

#[cfg(test)]
#[path = "tree_tests.rs"]
mod tests;
