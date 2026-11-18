// 앱 세션: 한 프로세스의 상태(환경·연 프로젝트·작업·자기 쓰기·CLI 복사 결과)와 셸·CLI 가 부르는 작업들을 둔다.
//! The session of one process and the operations the shell and the CLI call (CONTRACTS C2, M1-S1).
//!
//! An operation is a plain synchronous function `fn op(ctx: &Ctx, req: Req) -> Result<Res, Error>`. Long ones
//! run as tasks: the shell registers one with [`TaskRegistry::begin`](crate::tasks::TaskRegistry::begin), runs
//! [`run_task`] on a blocking thread and returns the task id at once. The session is the only long-lived state;
//! every durable fact is on disk, so dropping it loses nothing.

mod cli_copy;
mod refresh;
mod settings;

pub use cli_copy::{CliCopy, cli_copy};
pub use refresh::{REFRESH, refresh, refresh_key};
pub use settings::{settings_get, settings_set};

use crate::dto::{
    AppInfoDto, CliDto, Empty, ProjectDto, TaskCancelReq, TaskSnapshotDto, display_path,
};
use crate::error::Error;
use crate::fsx::SelfWrites;
use crate::paths::{self, Env};
use crate::project::{self, OpenProject};
use crate::tasks::{CancelToken, ProgressSink, TaskHandle, TaskRegistry, TaskState};
use std::path::Path;
use std::sync::{Mutex, MutexGuard, PoisonError};

/// The state of one process: its environment, the open project, the running tasks, OAH's own verified writes and
/// the result of this start's CLI copy. The CLI builds one per command; the app keeps one for its lifetime.
#[derive(Debug)]
pub struct Session {
    env: Env,
    project: Mutex<Option<OpenProject>>,
    tasks: TaskRegistry,
    self_writes: SelfWrites,
    cli: Mutex<Option<CliCopy>>,
}

/// What an operation receives: the environment, the session, and the cancellation and progress of the task it
/// runs in (a fresh token and [`crate::tasks::NoProgress`] outside a task).
#[derive(Clone, Copy)]
pub struct Ctx<'a> {
    pub env: &'a Env,
    pub session: &'a Session,
    pub cancel: &'a CancelToken,
    pub progress: &'a dyn ProgressSink,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Session {
    pub fn new(env: Env) -> Session {
        Session {
            env,
            project: Mutex::new(None),
            tasks: TaskRegistry::new(),
            self_writes: SelfWrites::new(),
            cli: Mutex::new(None),
        }
    }

    pub fn env(&self) -> &Env {
        &self.env
    }

    pub fn tasks(&self) -> &TaskRegistry {
        &self.tasks
    }

    /// OAH's own verified writes; every write to a watched path records its receipt here.
    pub fn self_writes(&self) -> &SelfWrites {
        &self.self_writes
    }

    /// The open project, as last read.
    pub fn project(&self) -> Option<OpenProject> {
        lock(&self.project).clone()
    }

    /// The CLI copy of this start; `None` until [`cli_copy`] ran.
    pub fn cli(&self) -> Option<CliCopy> {
        lock(&self.cli).clone()
    }

    /// A context for one operation with this session's environment.
    pub fn ctx<'a>(&'a self, cancel: &'a CancelToken, progress: &'a dyn ProgressSink) -> Ctx<'a> {
        Ctx {
            env: &self.env,
            session: self,
            cancel,
            progress,
        }
    }

    pub(crate) fn set_project(&self, project: OpenProject) {
        *lock(&self.project) = Some(project);
    }

    pub(crate) fn set_cli(&self, copy: CliCopy) {
        *lock(&self.cli) = Some(copy);
    }
}

/// Runs a started task's work on the calling thread with a context made of `session` and the task's own
/// cancellation token and progress sink, then finishes the task (CONTRACTS C4) and returns its final state.
pub fn run_task<R: 'static>(
    session: &Session,
    handle: TaskHandle<R>,
    work: impl FnOnce(&Ctx<'_>) -> Result<R, Error>,
) -> TaskState {
    let result = work(&session.ctx(handle.cancel_token(), &handle));
    handle.finish(result, session.env())
}

/// `app_info`: the version, the OS, `OAH_HOME`, this start's CLI copy and the notices raised while starting.
pub fn app_info(ctx: &Ctx<'_>, _req: Empty) -> Result<AppInfoDto, Error> {
    let home = paths::oah_home(ctx.env)?;
    let copy = ctx.session.cli();
    let cli = match &copy {
        Some(copy) => Some(CliDto {
            state: copy.state,
            path: display_path(ctx.env, &paths::cli_bin(ctx.env)?),
            version: copy.version.clone(),
        }),
        None => None,
    };
    Ok(AppInfoDto {
        version: crate::VERSION.to_string(),
        os: std::env::consts::OS.to_string(),
        oah_home: display_path(ctx.env, &home),
        cli,
        notices: copy.and_then(|copy| copy.notice).into_iter().collect(),
    })
}

/// Opens the folder the native picker returned, read-only, and makes it the session's project. The webview
/// never sends a path: the shell calls this with the picker's answer (`project_open_dialog`).
pub fn project_open(ctx: &Ctx<'_>, folder: &Path) -> Result<ProjectDto, Error> {
    let opened = project::open(ctx.env, folder)?;
    let dto = opened.to_dto(ctx.env);
    ctx.session.set_project(opened);
    Ok(dto)
}

/// `project_current`: the open project as last read, or `None`.
pub fn project_current(ctx: &Ctx<'_>, _req: Empty) -> Result<Option<ProjectDto>, Error> {
    Ok(ctx.session.project().map(|open| open.to_dto(ctx.env)))
}

/// `tasks_list`: the running tasks with their last progress.
pub fn tasks_list(ctx: &Ctx<'_>, _req: Empty) -> Result<Vec<TaskSnapshotDto>, Error> {
    Ok(ctx.session.tasks().list())
}

/// `task_cancel`: asks a running task to stop at its next check. An id that is not running is ignored.
pub fn task_cancel(ctx: &Ctx<'_>, req: TaskCancelReq) -> Result<Empty, Error> {
    ctx.session.tasks().cancel(&req.id);
    Ok(Empty {})
}
