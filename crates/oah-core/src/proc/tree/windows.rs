// 프로세스 트리의 Windows 쪽: 프로그램을 멈춘 채로 시작해, 닫히면 모두 끝나는 Job Object 에 넣은 뒤 깨우고, 파이프는 기다리는 바이트 수를 먼저 물어 막히지 않게 읽는다.
//! The Windows side of a tree run: a Job Object that ends every process in it when it is terminated or closed.
//!
//! The program is created suspended, put in the job, and only then resumed, so nothing it starts can be outside
//! the job. On stable Rust `std` does not hand out the new process's first thread, so the threads of the still
//! suspended process are found through a Toolhelp snapshot. Jobs nest (Windows 8 and later): a job the tool makes
//! for its own children ends with this one. Closing the job handle ends the tree too, so it cannot outlive this
//! process.

use super::Ready;
use std::io::{self, Read};
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Child, ChildStderr, ChildStdout, Command, ExitStatus};
use std::ptr::{null, null_mut};
use windows_sys::Win32::Foundation::{
    CloseHandle, ERROR_BROKEN_PIPE, HANDLE, INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject,
};
use windows_sys::Win32::System::Pipes::PeekNamedPipe;
use windows_sys::Win32::System::Threading::{
    CREATE_NO_WINDOW, CREATE_SUSPENDED, OpenThread, ResumeThread, THREAD_SUSPEND_RESUME,
};

/// A kernel handle this module opened; closed once, on drop.
struct Owned(HANDLE);

impl Drop for Owned {
    fn drop(&mut self) {
        // SAFETY: the handle is live and owned here, and nothing uses it after this.
        unsafe { CloseHandle(self.0) };
    }
}

/// A started program and the job that holds it and everything it starts.
pub(super) struct Tree {
    child: Child,
    job: Owned,
    reaped: bool,
}

impl Tree {
    /// Starts `command` suspended and without a console window, puts it in a new job, then lets it run.
    pub(super) fn spawn(command: &mut Command) -> io::Result<Tree> {
        let job = new_job()?;
        let child = command
            .creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW)
            .spawn()?;
        // From here a failure drops `tree`, which ends the process whether or not it reached the job.
        let tree = Tree {
            child,
            job,
            reaped: false,
        };
        // SAFETY: both handles are live: `tree` owns the job, and its `Child` owns the process handle.
        if unsafe { AssignProcessToJobObject(tree.job.0, tree.child.as_raw_handle()) } == 0 {
            return Err(io::Error::last_os_error());
        }
        resume(tree.child.id())?;
        Ok(tree)
    }

    pub(super) fn take_stdout(&mut self) -> Option<ChildStdout> {
        self.child.stdout.take()
    }

    pub(super) fn take_stderr(&mut self) -> Option<ChildStderr> {
        self.child.stderr.take()
    }

    /// Whether the program itself has ended.
    pub(super) fn exited(&mut self) -> io::Result<bool> {
        Ok(self.child.try_wait()?.is_some())
    }

    /// Asks the tree to end by itself. Always false here: Windows has no such request for a job.
    pub(super) fn ask(&mut self) -> bool {
        false
    }

    /// Kills what is left of the job and returns the program's exit status.
    pub(super) fn finish(mut self) -> io::Result<ExitStatus> {
        self.end();
        self.reaped = true;
        self.child.wait()
    }

    fn end(&mut self) {
        // SAFETY: the job handle is live. This ends every process in the job and in the jobs nested under it.
        unsafe { TerminateJobObject(self.job.0, 1) };
        // A process that never reached the job is ended directly; one that has ended is left as it is.
        let _ = self.child.kill();
    }
}

impl Drop for Tree {
    fn drop(&mut self) {
        if !self.reaped {
            self.end();
            self.reaped = true;
            let _ = self.child.wait();
        }
    }
}

/// A new job whose processes all end when it is terminated or its last handle is closed.
fn new_job() -> io::Result<Owned> {
    // SAFETY: both arguments may be null: an unnamed job with the default security.
    let handle = unsafe { CreateJobObjectW(null(), null()) };
    if handle.is_null() {
        return Err(io::Error::last_os_error());
    }
    let job = Owned(handle);
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    // SAFETY: `limits` is a live value of the type this information class names, and the length is its size.
    let set = unsafe {
        SetInformationJobObject(
            job.0,
            JobObjectExtendedLimitInformation,
            (&raw const limits).cast(),
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    };
    if set == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(job)
}

/// Resumes the threads of the suspended process `pid`: its first thread, and any that was added from outside.
fn resume(pid: u32) -> io::Result<()> {
    // SAFETY: a snapshot of every thread of the system; the process id is ignored for this kind of snapshot.
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let snapshot = Owned(snapshot);
    let mut entry = THREADENTRY32 {
        dwSize: size_of::<THREADENTRY32>() as u32,
        ..Default::default()
    };
    let mut resumed = false;
    // SAFETY: the snapshot handle is live, and `entry` is writable and carries its own size.
    let mut more = unsafe { Thread32First(snapshot.0, &mut entry) } != 0;
    while more {
        if entry.th32OwnerProcessID == pid {
            // SAFETY: plain values; a null result is handled just below.
            let thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
            if !thread.is_null() {
                let thread = Owned(thread);
                // SAFETY: the thread handle is live and was opened with the right to resume it.
                resumed |= unsafe { ResumeThread(thread.0) } != u32::MAX;
            }
        }
        // SAFETY: as for `Thread32First`.
        more = unsafe { Thread32Next(snapshot.0, &mut entry) } != 0;
    }
    if resumed {
        Ok(())
    } else {
        Err(io::Error::other("the new process could not be resumed"))
    }
}

/// A read end that says what is waiting instead of blocking.
pub(super) trait Source {
    fn read_ready(&mut self, chunk: &mut [u8]) -> io::Result<Ready>;
}

impl<R: Read + AsRawHandle> Source for R {
    fn read_ready(&mut self, chunk: &mut [u8]) -> io::Result<Ready> {
        let mut waiting = 0u32;
        // SAFETY: the pipe handle is live and owned by `self`; only the count of waiting bytes is asked for.
        let peeked = unsafe {
            PeekNamedPipe(
                self.as_raw_handle(),
                null_mut(),
                0,
                null_mut(),
                &mut waiting,
                null_mut(),
            )
        };
        if peeked == 0 {
            let error = io::Error::last_os_error();
            return if error.raw_os_error() == Some(ERROR_BROKEN_PIPE as i32) {
                Ok(Ready::End)
            } else {
                Err(error)
            };
        }
        if waiting == 0 {
            return Ok(Ready::Nothing);
        }
        let wanted = chunk.len().min(waiting as usize);
        Ok(match self.read(&mut chunk[..wanted])? {
            0 => Ready::End,
            count => Ready::Data(count),
        })
    }
}
