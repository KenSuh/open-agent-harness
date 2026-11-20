// 작업 등록부: 종류·대상마다 하나만 돌게 하고, 진행·결과를 듣는 쪽에 전하며, 취소 요청을 받는다.
use super::{CancelToken, Progress, ProgressSink, TaskError, TaskEvent, TaskId, TaskState};
use crate::error::Error;
use crate::paths::Env;
use serde::Serialize;
use std::any::Any;
use std::fmt;
use std::marker::PhantomData;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

/// Receives every event of one task, in order. The shell wraps its IPC channel in one.
pub type Listener<R> = Arc<dyn Fn(&TaskEvent<R>) + Send + Sync>;

/// What makes two tasks the same for exclusivity: kind and subject.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskKey {
    kind: &'static str,
    subject: String,
    joinable: bool,
}

impl TaskKey {
    /// A task whose second start for the same kind and subject is refused with `OAH_TASK_BUSY`.
    pub fn exclusive(kind: &'static str, subject: impl Into<String>) -> TaskKey {
        TaskKey {
            kind,
            subject: subject.into(),
            joinable: false,
        }
    }

    /// A task whose second start for the same kind and subject joins the running one.
    pub fn joinable(kind: &'static str, subject: impl Into<String>) -> TaskKey {
        TaskKey {
            kind,
            subject: subject.into(),
            joinable: true,
        }
    }

    pub fn kind(&self) -> &'static str {
        self.kind
    }

    pub fn subject(&self) -> &str {
        &self.subject
    }

    fn same_task(&self, other: &TaskKey) -> bool {
        self.kind == other.kind && self.subject == other.subject
    }
}

/// A running task as `tasks_list` shows it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TaskSnapshot {
    pub id: TaskId,
    pub kind: &'static str,
    pub state: TaskState,
    pub progress: Option<Progress>,
}

/// The outcome of [`TaskRegistry::begin`].
pub enum Begin<R> {
    /// A new task: run its work, then [`TaskHandle::finish`] it.
    Started(TaskHandle<R>),
    /// The same joinable task was running; the listener now receives its remaining events.
    Joined(TaskId),
}

impl<R> Begin<R> {
    pub fn id(&self) -> &TaskId {
        match self {
            Begin::Started(handle) => &handle.id,
            Begin::Joined(id) => id,
        }
    }
}

struct Entry {
    id: TaskId,
    key: TaskKey,
    cancel: CancelToken,
    progress: Option<Progress>,
    /// `Vec<Listener<R>>` for the task's result type `R`.
    listeners: Box<dyn Any + Send>,
}

#[derive(Default)]
struct State {
    issued: u64,
    running: Vec<Entry>,
}

/// The running tasks of one session.
#[derive(Default)]
pub struct TaskRegistry {
    shared: Arc<Mutex<State>>,
}

impl fmt::Debug for TaskRegistry {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("TaskRegistry")
            .field("running", &self.list())
            .finish()
    }
}

fn lock(shared: &Mutex<State>) -> MutexGuard<'_, State> {
    shared.lock().unwrap_or_else(PoisonError::into_inner)
}

impl TaskRegistry {
    pub fn new() -> TaskRegistry {
        TaskRegistry::default()
    }

    /// Registers a task for `key`, with `listener` receiving its events.
    ///
    /// When a task with the same kind and subject is running, a joinable key joins it ([`Begin::Joined`]) and
    /// any other key is refused with `OAH_TASK_BUSY`. Nothing runs here; the caller runs the work.
    pub fn begin<R: 'static>(
        &self,
        key: TaskKey,
        listener: Listener<R>,
    ) -> Result<Begin<R>, Error> {
        let mut state = lock(&self.shared);
        if let Some(entry) = state
            .running
            .iter_mut()
            .find(|entry| entry.key.same_task(&key))
        {
            if key.joinable
                && entry.key.joinable
                && let Some(listeners) = entry.listeners.downcast_mut::<Vec<Listener<R>>>()
            {
                listeners.push(listener);
                return Ok(Begin::Joined(entry.id.clone()));
            }
            return Err(TaskError::Busy {
                kind: key.kind,
                subject: key.subject,
            }
            .into());
        }
        state.issued += 1;
        let id = TaskId::new(format!("task-{}", state.issued));
        let cancel = CancelToken::new();
        state.running.push(Entry {
            id: id.clone(),
            key,
            cancel: cancel.clone(),
            progress: None,
            listeners: Box::new(vec![listener]),
        });
        Ok(Begin::Started(TaskHandle {
            shared: Arc::clone(&self.shared),
            id,
            cancel,
            finished: false,
            result: PhantomData,
        }))
    }

    /// Asks the running task `id` to stop at its next check. Returns `false`, and does nothing, when no task
    /// with that id is running (it finished, or the id is unknown).
    pub fn cancel(&self, id: &str) -> bool {
        let state = lock(&self.shared);
        match state.running.iter().find(|entry| entry.id.as_str() == id) {
            Some(entry) => {
                entry.cancel.cancel();
                true
            }
            None => false,
        }
    }

    /// The running tasks, oldest first, with their last progress.
    pub fn list(&self) -> Vec<TaskSnapshot> {
        lock(&self.shared)
            .running
            .iter()
            .map(|entry| TaskSnapshot {
                id: entry.id.clone(),
                kind: entry.key.kind,
                state: TaskState::Running,
                progress: entry.progress,
            })
            .collect()
    }
}

/// The running side of a started task: its cancellation token, its progress sink and its end.
///
/// Dropping a handle without [`TaskHandle::finish`] (only when the work panicked) unregisters the task and sends
/// no final event.
pub struct TaskHandle<R> {
    shared: Arc<Mutex<State>>,
    id: TaskId,
    cancel: CancelToken,
    finished: bool,
    result: PhantomData<fn() -> R>,
}

impl<R: 'static> TaskHandle<R> {
    pub fn id(&self) -> &TaskId {
        &self.id
    }

    pub fn cancel_token(&self) -> &CancelToken {
        &self.cancel
    }

    /// Ends the task with the work's result: unregisters it, then sends `done`, `failed` or `cancelled` to
    /// every listener. A cancellation stop becomes `cancelled`; any other error is `failed` with its
    /// [`crate::ErrorDto`], whose paths are written with `~` from `env`.
    pub fn finish(mut self, result: Result<R, Error>, env: &Env) -> TaskState {
        let (state, event) = match result {
            Ok(value) => (TaskState::Done, TaskEvent::Done(value)),
            Err(error) if error.is_cancelled() => (TaskState::Cancelled, TaskEvent::Cancelled),
            Err(error) => (TaskState::Failed, TaskEvent::Failed(error.to_dto(env))),
        };
        let listeners = self
            .unregister()
            .and_then(|entry| entry.listeners.downcast::<Vec<Listener<R>>>().ok())
            .map(|listeners| *listeners)
            .unwrap_or_default();
        for listener in &listeners {
            listener(&event);
        }
        state
    }
}

impl<R> TaskHandle<R> {
    fn unregister(&mut self) -> Option<Entry> {
        self.finished = true;
        let mut state = lock(&self.shared);
        let index = state.running.iter().position(|entry| entry.id == self.id)?;
        Some(state.running.remove(index))
    }
}

impl<R: 'static> ProgressSink for TaskHandle<R> {
    /// Records `progress` as the task's last progress and sends it to every listener.
    fn progress(&self, progress: Progress) {
        let listeners = {
            let mut state = lock(&self.shared);
            match state.running.iter_mut().find(|entry| entry.id == self.id) {
                Some(entry) => {
                    entry.progress = Some(progress);
                    entry
                        .listeners
                        .downcast_ref::<Vec<Listener<R>>>()
                        .cloned()
                        .unwrap_or_default()
                }
                None => Vec::new(),
            }
        };
        let event = TaskEvent::Progress(progress);
        for listener in &listeners {
            listener(&event);
        }
    }
}

impl<R> Drop for TaskHandle<R> {
    fn drop(&mut self) {
        if !self.finished {
            self.unregister();
        }
    }
}
