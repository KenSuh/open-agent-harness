// 앱 세션: 한 프로세스의 상태(환경·연 프로젝트·작업·자기 쓰기·CLI 복사 결과)와 셸·CLI 가 부르는 작업들을 둔다.
//! The session of one process and the operations the shell and the CLI call (CONTRACTS C2, M1-S1).
//!
//! An operation is a plain synchronous function `fn op(ctx: &Ctx, req: Req) -> Result<Res, Error>`. Long ones
//! run as tasks: the shell registers one with [`TaskRegistry::begin`](crate::tasks::TaskRegistry::begin), runs
//! [`run_task`] on a blocking thread and returns the task id at once. The session is the only long-lived state;
//! every durable fact is on disk, so dropping it loses nothing.

mod agents;
mod cli_copy;
mod doctor;
mod installed;
mod installed_management;
mod refresh;
mod review;
mod settings;
mod skill_apply;
pub mod sync;

pub use crate::workflow::{
    preset_apply, project_tools_status, rules_check, rules_sync, rules_sync_for_project,
    sections_show, sections_show_for_project, stages_set, workflow_status,
};
pub use agents::{
    AGENTS_PROBE, SIGN_IN_INTERVAL, agents_list, agents_probe, agents_probe_key, agents_probe_with,
    sign_in_due,
};
pub use cli_copy::{CliCopy, cli_copy};
pub use doctor::{
    Doctor, doctor, doctor_exit, doctor_project_commands, doctor_project_commands_with,
    doctor_with, doctor_with_options,
};
pub use installed::skills_installed;
pub use installed_management::{
    inspect_installed_skill, register_skill_source, remove_installed_skill,
};
pub use refresh::{REFRESH, refresh, refresh_key};
pub use review::{REVIEW, ReviewFailed, review, review_key};
pub use settings::{settings_get, settings_set};
pub use skill_apply::apply_project_skill;

use crate::dto::{
    AppInfoDto, CliDto, Empty, ProjectDto, TaskCancelReq, TaskSnapshotDto, display_path,
};
use crate::error::{Error, ProjectSkillError};
use crate::fsx::SelfWrites;
use crate::fsx::skill_publish::ProjectRoot;
use crate::net::{HttpsTransport, Transport};
use crate::paths::{self, Env};
use crate::project::{self, OpenProject};
use crate::tasks::{CancelToken, ProgressSink, TaskHandle, TaskRegistry, TaskState};
use std::borrow::Cow;
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, PoisonError};

/// The state of one process: its environment, the open project, the running tasks, OAH's own verified writes and
/// the result of this start's CLI copy. The CLI builds one per command; the app keeps one for its lifetime.
#[derive(Debug)]
pub struct Session {
    env: Env,
    project: Mutex<ProjectSlot>,
    tasks: TaskRegistry,
    self_writes: SelfWrites,
    cli: Mutex<Option<CliCopy>>,
    agents: Mutex<agents::AgentCache>,
    transport: TransportSlot,
}

/// The transport the session's network operations send through: the one [`Session::with_transport`] gave, else the
/// real HTTPS transport, made on first use.
#[derive(Default)]
struct TransportSlot(OnceLock<Arc<dyn Transport>>);

impl fmt::Debug for TransportSlot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let state = if self.0.get().is_some() {
            "ready"
        } else {
            "unused"
        };
        write!(f, "TransportSlot({state})")
    }
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

/// The order of one project read: the explicit open that started its selection, then the read itself. Stamps are
/// taken before the read's IO; a read is published only when its stamp is newer than the held one, so neither an
/// older open nor a refresh of a selection that was replaced meanwhile (A, then B, then A again) can win.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct Stamp {
    selection: u64,
    read: u64,
}

/// The session's project with the stamp of the read that produced it, and the last number handed out.
#[derive(Debug, Default)]
struct ProjectSlot {
    issued: u64,
    held: Option<ProjectSelection>,
}

#[derive(Debug)]
struct ProjectSelection {
    stamp: Stamp,
    opened: OpenProject,
    skill_root: Result<ProjectRoot, ProjectSkillError>,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Session {
    pub fn new(env: Env) -> Session {
        Session {
            env,
            project: Mutex::default(),
            tasks: TaskRegistry::new(),
            self_writes: SelfWrites::new(),
            cli: Mutex::new(None),
            agents: Mutex::default(),
            transport: TransportSlot::default(),
        }
    }

    /// A session whose network operations send through `transport` instead of the real HTTPS transport (tests and
    /// recorded fixtures).
    pub fn with_transport(env: Env, transport: Arc<dyn Transport>) -> Session {
        let session = Session::new(env);
        let _ = session.transport.0.set(transport);
        session
    }

    /// The transport of the session's network operations.
    pub fn transport(&self) -> Arc<dyn Transport> {
        self.transport
            .0
            .get_or_init(|| Arc::new(HttpsTransport::new()))
            .clone()
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
        lock(&self.project)
            .held
            .as_ref()
            .map(|held| held.opened.clone())
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

    /// A stamp for an explicit open, which starts a new selection.
    pub(crate) fn stamp_open(&self) -> Stamp {
        let mut slot = lock(&self.project);
        slot.issued += 1;
        Stamp {
            selection: slot.issued,
            read: slot.issued,
        }
    }

    /// The root of the selected project with a stamp for re-reading it; `None` when no project is open.
    pub(crate) fn stamp_refresh(&self) -> Option<(Stamp, PathBuf)> {
        let mut slot = lock(&self.project);
        let held = slot.held.as_ref()?;
        let (selection, root) = (held.stamp.selection, held.opened.project.root.clone());
        slot.issued += 1;
        let read = slot.issued;
        Some((Stamp { selection, read }, root))
    }

    /// Refresh the view while retaining the pin from the explicit open. If re-discovery locates a different
    /// root, that view is still readable and needs an explicit open before skill publication.
    pub(crate) fn publish_project(&self, stamp: Stamp, read: OpenProject) -> OpenProject {
        let mut slot = lock(&self.project);
        let Some(held) = slot.held.as_mut() else {
            return read;
        };
        if held.stamp.selection == stamp.selection && held.stamp <= stamp {
            if held.opened.id != read.id || held.opened.project.root != read.project.root {
                held.skill_root = Err(ProjectSkillError::ProjectChanged);
            }
            held.stamp = stamp;
            held.opened = read;
        }
        held.opened.clone()
    }

    fn publish_selection(&self, selected: ProjectSelection) -> OpenProject {
        let mut slot = lock(&self.project);
        if let Some(held) = &slot.held
            && held.stamp.selection > selected.stamp.selection
        {
            return held.opened.clone();
        }
        let opened = selected.opened.clone();
        slot.held = Some(selected);
        opened
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
/// never sends a path: the shell calls this with the picker's answer (`project_open_dialog`). When a later open
/// published first, this older one changes nothing and returns the project the session holds.
pub fn project_open(ctx: &Ctx<'_>, folder: &Path) -> Result<ProjectDto, Error> {
    let stamp = ctx.session.stamp_open();
    let folder = if folder.is_absolute() {
        Cow::Borrowed(folder)
    } else {
        Cow::Owned(ctx.env.cwd().join(folder))
    };
    let home = paths::user_home(ctx.env).map_err(|_| ProjectSkillError::HomeUnverified);
    // Check the picker's path before discovery can resolve a link or return an ancestor.
    let picked = home
        .as_deref()
        .map_err(Clone::clone)
        .and_then(|home| ProjectRoot::open(&folder, home).map_err(ProjectSkillError::from));
    let opened = project::open(ctx.env, &folder)?;
    let root = &opened.project.root;
    let skill_root = home.and_then(|home| {
        skill_apply::refuse_home(root, &home)?;
        let picked = picked?;
        if folder.as_ref() == root {
            Ok(picked)
        } else {
            Ok(ProjectRoot::open(root, &home)?)
        }
    });
    let selected = ProjectSelection {
        stamp,
        opened,
        skill_root,
    };
    Ok(ctx.session.publish_selection(selected).to_dto(ctx.env))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_older_open_that_finishes_last_does_not_replace_the_newer_selection() {
        let dir = tempfile::tempdir().unwrap();
        // The same platform form the opened project reports: on Windows, a short drive path without `\\?\`.
        let root = paths::canonical_folder(dir.path()).unwrap();
        let mut env = Env::new(&root).unwrap();
        if let Some(path) = std::env::var_os("PATH") {
            env.set("PATH", path);
        }
        env.set(if cfg!(windows) { "USERPROFILE" } else { "HOME" }, &root);
        env.set("GIT_CEILING_DIRECTORIES", &root);
        let [a, b] = ["a", "b"].map(|name| root.join(name));
        for folder in [&a, &b] {
            std::fs::create_dir_all(folder).unwrap();
        }
        let session = Session::new(env);
        let older = session.stamp_open();
        let newer = session.stamp_open();
        let selection = |stamp, folder: &Path| ProjectSelection {
            stamp,
            opened: project::open(session.env(), folder).unwrap(),
            skill_root: ProjectRoot::open(folder, &root).map_err(ProjectSkillError::from),
        };
        let held_b = session.publish_selection(selection(newer, &b));
        let returned = session.publish_selection(selection(older, &a));
        assert_eq!(held_b.project.root, b);
        assert_eq!(
            returned.project.root, b,
            "the older open reports the held project"
        );
        assert_eq!(session.project().unwrap().project.root, b);
    }
}
