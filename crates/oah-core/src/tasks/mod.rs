// 작업 기본형: 긴 작업의 id·상태·진행·협조적 취소와, 같은 종류·대상에 하나만 돌게 하는 등록부를 둔다.
//! Long operations as tasks (CONTRACTS C4).
//!
//! A task is registered in a [`TaskRegistry`] under a [`TaskKey`] (kind and subject); only one task per key runs
//! at a time. A second start for the same key is refused with `OAH_TASK_BUSY`, except for a joinable key (the
//! full refresh), where the second caller joins the running task and receives its remaining events.
//!
//! Cancellation is cooperative: work calls [`CancelToken::check`] between steps and never inside a publishing
//! step. A task ends `cancelled` only when its work returned the cancellation stop, which work does only before
//! anything durable was published.

mod registry;

pub use registry::{Begin, Listener, TaskHandle, TaskKey, TaskRegistry, TaskSnapshot};

use crate::error::{ErrorDto, codes};
use serde::Serialize;
use std::fmt;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

/// A task's id, unique within one session.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
#[serde(transparent)]
pub struct TaskId(String);

impl TaskId {
    pub(crate) fn new(text: String) -> TaskId {
        TaskId(text)
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for TaskId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// The four states of a task.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum TaskState {
    Running,
    Done,
    Failed,
    Cancelled,
}

/// One message on a task's channel. JSON: `{"event": "progress", "data": {...}}`; `cancelled` has no `data`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "event", content = "data", rename_all = "kebab-case")]
pub enum TaskEvent<R> {
    Progress(Progress),
    Done(R),
    Failed(ErrorDto),
    Cancelled,
}

/// How far a task has come. `phase` is a short kebab id such as `read-project`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Progress {
    pub phase: &'static str,
    pub done: u32,
    pub total: Option<u32>,
}

/// The stop a cancelled task's work returns.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cancelled;

/// A cooperative cancellation flag shared by a task and whoever may cancel it.
#[derive(Debug, Clone, Default)]
pub struct CancelToken(Arc<AtomicBool>);

impl CancelToken {
    /// A token that is not cancelled.
    pub fn new() -> CancelToken {
        CancelToken::default()
    }

    /// Asks the task to stop at its next check.
    pub fn cancel(&self) {
        self.0.store(true, Ordering::SeqCst);
    }

    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }

    /// `Err(Cancelled)` once cancellation was asked for. Call it only between steps, never inside a publish.
    pub fn check(&self) -> Result<(), Cancelled> {
        if self.is_cancelled() {
            Err(Cancelled)
        } else {
            Ok(())
        }
    }
}

/// Receives a task's progress.
pub trait ProgressSink: Sync {
    fn progress(&self, progress: Progress);
}

/// A sink that drops progress, for operations that are not running as a task.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoProgress;

impl ProgressSink for NoProgress {
    fn progress(&self, _progress: Progress) {}
}

/// A task failure that is not another module's error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TaskError {
    /// A task with the same kind and subject is running.
    Busy { kind: &'static str, subject: String },
    /// The work stopped at a cancellation check. Never serialized: the task ends with the `cancelled` event.
    Cancelled,
}

impl crate::error::Coded for TaskError {
    fn code(&self) -> &'static str {
        match self {
            TaskError::Busy { .. } => codes::TASK_BUSY,
            TaskError::Cancelled => codes::TASK_CANCELLED,
        }
    }

    fn message(&self) -> String {
        match self {
            TaskError::Busy { kind, subject } if subject.is_empty() => {
                format!("a {kind} task is already running")
            }
            TaskError::Busy { kind, subject } => {
                format!("a {kind} task for {subject} is already running")
            }
            TaskError::Cancelled => {
                String::from("the task was cancelled before it changed anything")
            }
        }
    }
}
