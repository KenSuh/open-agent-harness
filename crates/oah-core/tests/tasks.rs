// 작업 등록부 검사: 같은 종류·대상의 배타성과 합류, 취소가 데이터 없는 cancelled 로 끝나는지, 게시 뒤 취소는 done 인지 본다.
use oah_core::app::{self, Session};
use oah_core::dto::{Empty, RefreshDto};
use oah_core::error::codes;
use oah_core::fsx::LockOptions;
use oah_core::paths::Env;
use oah_core::plan::{self, Action, Basis, ChangePlan, Owner, PlanItem};
use oah_core::project::ProjectError;
use oah_core::tasks::{
    Begin, Listener, Progress, ProgressSink, TaskEvent, TaskHandle, TaskKey, TaskRegistry,
    TaskState,
};
use oah_core::{Coded, Error};
use std::fs;
use std::path::Path;
use std::sync::{Arc, Mutex, mpsc};

const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };

type Events<R> = Arc<Mutex<Vec<TaskEvent<R>>>>;

fn recorder<R: Clone + Send + 'static>() -> (Events<R>, Listener<R>) {
    let events: Events<R> = Arc::new(Mutex::new(Vec::new()));
    let sink = Arc::clone(&events);
    let listener: Listener<R> = Arc::new(move |event: &TaskEvent<R>| {
        sink.lock().unwrap().push(event.clone());
    });
    (events, listener)
}

fn started<R>(begin: Begin<R>) -> TaskHandle<R> {
    match begin {
        Begin::Started(handle) => handle,
        Begin::Joined(id) => panic!("expected a new task, joined {id}"),
    }
}

fn session(dir: &Path) -> Session {
    let mut env = Env::new(dir).unwrap();
    env.set(HOME_VAR, dir);
    Session::new(env)
}

#[test]
fn a_second_start_for_the_same_kind_and_subject_is_busy_until_the_first_ends() {
    let registry = TaskRegistry::new();
    let (_, listener) = recorder::<u32>();
    let key = TaskKey::exclusive("review", "skill-a");
    let first = started(registry.begin(key.clone(), Arc::clone(&listener)).unwrap());
    let busy = registry.begin(key.clone(), Arc::clone(&listener));
    let Err(error) = busy else {
        panic!("a second start of a running exclusive task must be refused");
    };
    assert_eq!(error.code(), codes::TASK_BUSY);
    let other = started(
        registry
            .begin(
                TaskKey::exclusive("review", "skill-b"),
                Arc::clone(&listener),
            )
            .unwrap(),
    );
    assert_ne!(other.id(), first.id());
    let env = Env::new(std::env::temp_dir()).unwrap();
    assert_eq!(first.finish(Ok(1), &env), TaskState::Done);
    let again = started(registry.begin(key, listener).unwrap());
    assert_ne!(again.id().as_str(), "task-1");
    assert_eq!(registry.list().len(), 2);
}

#[test]
fn a_second_refresh_joins_the_running_one_and_every_listener_gets_the_end_once() {
    let dir = tempfile::tempdir().unwrap();
    let session = Arc::new(session(dir.path()));
    let (first_events, first) = recorder::<RefreshDto>();
    let (second_events, second) = recorder::<RefreshDto>();
    let handle = started(session.tasks().begin(app::refresh_key(), first).unwrap());
    let id = handle.id().clone();
    let (running_tx, running_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let worker_session = Arc::clone(&session);
    let worker = std::thread::spawn(move || {
        app::run_task(&worker_session, handle, |ctx| {
            running_tx.send(()).unwrap();
            release_rx.recv().unwrap();
            app::refresh(ctx, Empty {})
        })
    });
    running_rx.recv().unwrap();
    let joined = session.tasks().begin(app::refresh_key(), second).unwrap();
    assert!(matches!(&joined, Begin::Joined(joined_id) if *joined_id == id));
    assert_eq!(session.tasks().list().len(), 1);
    release_tx.send(()).unwrap();
    assert_eq!(worker.join().unwrap(), TaskState::Done);
    let done = TaskEvent::Done(RefreshDto {
        projects: 0,
        notices: 0,
    });
    for events in [&first_events, &second_events] {
        let events = events.lock().unwrap();
        assert_eq!(events.iter().filter(|event| **event == done).count(), 1);
        assert_eq!(events.last(), Some(&done));
    }
    assert!(session.tasks().list().is_empty());
}

#[test]
fn a_cancelled_task_ends_with_a_cancelled_event_that_has_no_data() {
    let dir = tempfile::tempdir().unwrap();
    let session = session(dir.path());
    let (events, listener) = recorder::<RefreshDto>();
    let handle = started(session.tasks().begin(app::refresh_key(), listener).unwrap());
    assert!(session.tasks().cancel(handle.id().as_str()));
    let state = app::run_task(&session, handle, |ctx| app::refresh(ctx, Empty {}));
    assert_eq!(state, TaskState::Cancelled);
    let events = events.lock().unwrap();
    assert_eq!(events.as_slice(), [TaskEvent::Cancelled]);
    let json = serde_json::to_value(&events[0]).unwrap();
    assert_eq!(json, serde_json::json!({ "event": "cancelled" }));
    assert!(session.tasks().list().is_empty());
}

#[test]
fn cancelling_an_id_that_is_not_running_changes_nothing() {
    let registry = TaskRegistry::new();
    let (_, listener) = recorder::<u32>();
    let handle = started(
        registry
            .begin(TaskKey::exclusive("review", "a"), listener)
            .unwrap(),
    );
    assert!(!registry.cancel("task-99"));
    assert!(!handle.cancel_token().is_cancelled());
}

#[test]
fn failed_work_ends_failed_with_its_error_and_home_written_as_a_tilde() {
    let dir = tempfile::tempdir().unwrap();
    let home = dir.path().canonicalize().unwrap();
    let session = session(&home);
    let (events, listener) = recorder::<RefreshDto>();
    let handle = started(session.tasks().begin(app::refresh_key(), listener).unwrap());
    let state = app::run_task(&session, handle, |_ctx| {
        Err(Error::from(ProjectError::NotFolder {
            path: home.join("notes.txt"),
        }))
    });
    assert_eq!(state, TaskState::Failed);
    let events = events.lock().unwrap();
    let [TaskEvent::Failed(error)] = events.as_slice() else {
        panic!("expected one failed event, got {events:?}");
    };
    assert_eq!(error.code, "OAH_PROJECT_NOT_FOLDER");
    let shown = Path::new("~").join("notes.txt");
    assert_eq!(
        error.message,
        format!("{} is not a folder", shown.display())
    );
    assert_eq!(error.hint.as_deref(), Some("Choose a folder, not a file."));
}

#[test]
fn progress_reaches_every_listener_and_the_task_list_until_the_end() {
    let registry = TaskRegistry::new();
    let (events, listener) = recorder::<u32>();
    let handle = started(
        registry
            .begin(TaskKey::joinable("refresh", ""), listener)
            .unwrap(),
    );
    let step = Progress {
        phase: "read-project",
        done: 1,
        total: Some(2),
    };
    handle.progress(step);
    assert_eq!(registry.list()[0].progress, Some(step));
    let env = Env::new(std::env::temp_dir()).unwrap();
    handle.finish(Ok(7), &env);
    assert_eq!(
        events.lock().unwrap().as_slice(),
        [TaskEvent::Progress(step), TaskEvent::Done(7)]
    );
    assert!(registry.list().is_empty());
}

#[test]
fn a_handle_dropped_without_finishing_frees_its_key() {
    let registry = TaskRegistry::new();
    let (events, listener) = recorder::<u32>();
    let key = TaskKey::exclusive("review", "a");
    drop(started(
        registry.begin(key.clone(), Arc::clone(&listener)).unwrap(),
    ));
    assert!(registry.list().is_empty());
    assert!(events.lock().unwrap().is_empty());
    started(registry.begin(key, listener).unwrap());
}

/// Two targets written through `plan::apply` inside a task; `cancel_at` asks for cancellation at that point.
fn apply_in_task(
    dir: &Path,
    cancel_after_first_publish: bool,
) -> (TaskState, Vec<TaskEvent<usize>>) {
    let a = dir.join("a.txt");
    let b = dir.join("b.txt");
    fs::write(&a, "a0").unwrap();
    fs::write(&b, "b0").unwrap();
    let item = |id: &str, target: &Path| PlanItem {
        id: id.to_string(),
        target: target.to_path_buf(),
        owner: Owner::Project,
        action: Action::Update,
        summary: format!("update {id}"),
        detail: None,
    };
    let plan = ChangePlan {
        id: String::from("plan-1"),
        items: vec![item("a", &a), item("b", &b)],
        warnings: Vec::new(),
        basis: vec![Basis::observe(&a).unwrap(), Basis::observe(&b).unwrap()],
    };
    let session = session(dir);
    let (events, listener) = recorder::<usize>();
    let handle = started(
        session
            .tasks()
            .begin(TaskKey::exclusive("apply", "plan-1"), listener)
            .unwrap(),
    );
    let id = handle.id().clone();
    if !cancel_after_first_publish {
        session.tasks().cancel(id.as_str());
    }
    let state = app::run_task(&session, handle, |ctx| {
        let accepted = plan.accept(&["a", "b"]);
        let report = plan::apply(&accepted, ctx.cancel, |target| {
            let outcome = plan::write_target(target, b"new", LockOptions::default());
            if cancel_after_first_publish {
                ctx.session.tasks().cancel(id.as_str());
            }
            outcome
        })?;
        Ok(report.receipts.len())
    });
    let events = events.lock().unwrap().clone();
    (state, events)
}

#[test]
fn a_task_cancelled_before_it_publishes_writes_nothing_and_ends_cancelled() {
    let dir = tempfile::tempdir().unwrap();
    let (state, events) = apply_in_task(dir.path(), false);
    assert_eq!(state, TaskState::Cancelled);
    assert_eq!(events, [TaskEvent::Cancelled]);
    assert_eq!(fs::read(dir.path().join("a.txt")).unwrap(), b"a0");
    assert_eq!(fs::read(dir.path().join("b.txt")).unwrap(), b"b0");
}

#[test]
fn a_task_cancelled_after_its_first_publish_finishes_and_ends_done() {
    let dir = tempfile::tempdir().unwrap();
    let (state, events) = apply_in_task(dir.path(), true);
    assert_eq!(state, TaskState::Done);
    assert_eq!(events, [TaskEvent::Done(2)]);
    assert_eq!(fs::read(dir.path().join("a.txt")).unwrap(), b"new");
    assert_eq!(fs::read(dir.path().join("b.txt")).unwrap(), b"new");
}
