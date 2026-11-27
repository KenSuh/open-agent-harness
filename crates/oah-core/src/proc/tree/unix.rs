// 프로세스 트리의 Unix 쪽: 프로그램을 새 프로세스 그룹의 리더로 시작하고, 리더를 거두기 전에만 그 그룹에 신호를 보낸다.
//! The Unix side of a tree run: a new process group, signalled only while its leader has not been waited for.
//!
//! The group's id is the leader's process id. The kernel does not give that number to another process or group
//! while the leader, running or ended, is still unwaited, so every signal here goes out before the wait:
//! [`Tree::exited`] looks with `WNOWAIT` and leaves the ended leader in place, and only [`Tree::finish`] and
//! `Drop` wait for it, after their `SIGKILL`.

use super::Ready;
use rustix::fs::{OFlags, fcntl_getfl, fcntl_setfl};
use rustix::io::Errno;
use rustix::process::{Pid, Signal, WaitId, WaitIdOptions, kill_process_group, waitid};
use std::io::{self, Read};
use std::os::fd::AsFd;
use std::os::unix::process::CommandExt;
use std::process::{Child, ChildStderr, ChildStdout, Command, ExitStatus};

/// A started program and the process group it leads.
pub(super) struct Tree {
    child: Child,
    group: Pid,
    reaped: bool,
}

impl Tree {
    /// Starts `command` as the leader of a new process group, with read ends that never block.
    pub(super) fn spawn(command: &mut Command) -> io::Result<Tree> {
        let child = command.process_group(0).spawn()?;
        let group = Pid::from_child(&child);
        // From here a failure drops `tree`, which kills the group and waits for the leader.
        let tree = Tree {
            child,
            group,
            reaped: false,
        };
        if let Some(pipe) = &tree.child.stdout {
            nonblocking(pipe)?;
        }
        if let Some(pipe) = &tree.child.stderr {
            nonblocking(pipe)?;
        }
        Ok(tree)
    }

    pub(super) fn take_stdout(&mut self) -> Option<ChildStdout> {
        self.child.stdout.take()
    }

    pub(super) fn take_stderr(&mut self) -> Option<ChildStderr> {
        self.child.stderr.take()
    }

    pub(super) fn take_stdin(&mut self) -> Option<std::process::ChildStdin> {
        self.child.stdin.take()
    }

    /// Whether the program itself has ended. It is not waited for here, so its group id stays its own.
    pub(super) fn exited(&mut self) -> io::Result<bool> {
        let look = WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT;
        match waitid(WaitId::Pid(self.group), look) {
            Ok(status) => Ok(status.is_some()),
            Err(Errno::INTR) => Ok(false),
            Err(error) => Err(error.into()),
        }
    }

    /// Asks the whole group to end by itself. Always true here: Unix has such a request.
    pub(super) fn ask(&mut self) -> bool {
        self.signal(Signal::TERM);
        true
    }

    /// Kills what is left of the group and returns the program's exit status.
    pub(super) fn finish(mut self) -> io::Result<ExitStatus> {
        self.signal(Signal::KILL);
        self.reaped = true;
        self.child.wait()
    }

    fn signal(&self, signal: Signal) {
        if !self.reaped {
            // A failure means the group has no process left to signal, which is what the signal is for.
            let _ = kill_process_group(self.group, signal);
        }
    }
}

impl Drop for Tree {
    fn drop(&mut self) {
        if !self.reaped {
            self.signal(Signal::KILL);
            self.reaped = true;
            let _ = self.child.wait();
        }
    }
}

fn nonblocking(pipe: impl AsFd) -> io::Result<()> {
    let flags = fcntl_getfl(&pipe)?;
    fcntl_setfl(&pipe, flags | OFlags::NONBLOCK)?;
    Ok(())
}

/// A read end that says what is waiting instead of blocking.
pub(super) trait Source {
    fn read_ready(&mut self, chunk: &mut [u8]) -> io::Result<Ready>;
}

impl<R: Read> Source for R {
    fn read_ready(&mut self, chunk: &mut [u8]) -> io::Result<Ready> {
        match self.read(chunk) {
            Ok(0) => Ok(Ready::End),
            Ok(count) => Ok(Ready::Data(count)),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                Ok(Ready::Nothing)
            }
            Err(error) => Err(error),
        }
    }
}
