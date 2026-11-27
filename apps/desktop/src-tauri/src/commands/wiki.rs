// 위키 IPC 명령: 프로젝트 위키의 개요·미리보기·적용 시작·검색을 oah-core 호출 하나로 넘기고 결과만 돌려준다.
//! The Project Wiki commands: `project_wiki_overview`, `project_wiki_plan`, `start_project_wiki_apply` and
//! `project_wiki_search`. Each takes one `req` (`{ project_id, input }`); the start command also takes the task
//! channel `on_event`. Core checks the project id against the session's open project and takes the project folder
//! from the selection made when it was opened; no command here receives a path to a folder outside that project.
//!
//! The three reads list folders and read files, so they run on Tauri's blocking pool and cannot be interrupted;
//! the webview drops an answer that arrives after the project changed. The apply is a task (CONTRACTS C4):
//! `task_cancel` stops it before the next file, and what it reports then is its `done` payload.

use std::sync::Arc;

use oah_core::app::{self, Ctx};
use oah_core::dto::{
    Empty, ProjectWikiDto, WikiActionReq, WikiApplyDto, WikiApplyReq, WikiPlanDto, WikiSearchDto,
    WikiSearchReq, WorkflowProjectReq,
};
use oah_core::tasks::{Begin, Listener, TaskEvent, TaskId, TaskState};
use oah_core::{Error, ErrorDto};
use tauri::ipc::Channel;
use tauri::{AppHandle, State};

use super::{Shell, emit_scan_changed};

/// Runs one wiki read on the blocking pool.
async fn read<R: Send + 'static>(
    shell: State<'_, Shell>,
    operation: impl FnOnce(&Ctx<'_>) -> Result<R, Error> + Send + 'static,
) -> Result<R, ErrorDto> {
    let shell = Shell {
        session: Arc::clone(&shell.session),
    };
    tauri::async_runtime::spawn_blocking(move || shell.call(operation))
        .await
        .map_err(|_| ErrorDto {
            code: "OAH_WIKI_INTERRUPTED".to_owned(),
            message: "The wiki check stopped before reporting its result.".to_owned(),
            hint: Some("Nothing was written. Try again.".to_owned()),
        })?
}

#[tauri::command]
pub async fn project_wiki_overview(
    req: WorkflowProjectReq<Empty>,
    shell: State<'_, Shell>,
) -> Result<ProjectWikiDto, ErrorDto> {
    read(shell, move |ctx| app::project_wiki_overview(ctx, req)).await
}

#[tauri::command]
pub async fn project_wiki_plan(
    req: WorkflowProjectReq<WikiActionReq>,
    shell: State<'_, Shell>,
) -> Result<WikiPlanDto, ErrorDto> {
    read(shell, move |ctx| app::project_wiki_plan(ctx, req)).await
}

#[tauri::command]
pub async fn project_wiki_search(
    req: WorkflowProjectReq<WikiSearchReq>,
    shell: State<'_, Shell>,
) -> Result<WikiSearchDto, ErrorDto> {
    read(shell, move |ctx| app::project_wiki_search(ctx, req)).await
}

impl Shell {
    /// Starts the wiki apply for `req` on Tauri's blocking pool and returns its task id at once. One apply runs per
    /// project: a second start while it runs is `OAH_TASK_BUSY`. `finished` is told how the task ended.
    fn spawn_wiki_apply(
        &self,
        req: WorkflowProjectReq<WikiApplyReq>,
        listener: Listener<WikiApplyDto>,
        finished: impl FnOnce(TaskState) + Send + 'static,
    ) -> Result<TaskId, ErrorDto> {
        let begun = self
            .session
            .tasks()
            .begin(app::wiki_apply_key(&req.project_id), listener)
            .map_err(|error| self.to_dto(&error))?;
        match begun {
            Begin::Joined(id) => Ok(id),
            Begin::Started(handle) => {
                let id = handle.id().clone();
                let session = Arc::clone(&self.session);
                tauri::async_runtime::spawn_blocking(move || {
                    finished(app::run_task(&session, handle, |ctx| {
                        app::project_wiki_apply(ctx, req)
                    }));
                });
                Ok(id)
            }
        }
    }
}

/// Starts applying a reviewed wiki plan (CONTRACTS C4). Its events go to `on_event`; `scan-changed` follows a
/// task that ended `done`, because files and the settings may have changed.
#[tauri::command]
pub fn start_project_wiki_apply(
    req: WorkflowProjectReq<WikiApplyReq>,
    on_event: Channel<TaskEvent<WikiApplyDto>>,
    app: AppHandle,
    shell: State<'_, Shell>,
) -> Result<TaskId, ErrorDto> {
    let listener: Listener<WikiApplyDto> = Arc::new(move |event: &TaskEvent<WikiApplyDto>| {
        let _ = on_event.send(event.clone());
    });
    shell.spawn_wiki_apply(req, listener, move |state| {
        if state == TaskState::Done {
            emit_scan_changed(&app);
        }
    })
}

#[cfg(all(test, any(target_os = "macos", windows)))]
mod tests {
    use super::*;
    use oah_core::dto::{WikiSettingsState, WikiTargetState};
    use oah_core::error::codes;
    use oah_core::paths::Env;
    use std::sync::mpsc;
    use std::time::Duration;
    use tauri::Manager;

    /// The real handlers, called with Tauri's own managed state, work on the session's open project: the overview
    /// and the plan read it, the apply task writes it and reports through its listener, and another project's id
    /// is refused by each.
    #[test]
    fn the_wiki_handlers_use_the_shells_session_and_refuse_another_project() {
        let base = oah_core::paths::canonical_folder(&std::env::temp_dir())
            .unwrap()
            .join(format!("oah-wiki-handlers-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let (home, project) = (base.join("home"), base.join("project"));
        std::fs::create_dir_all(&home).unwrap();
        std::fs::create_dir_all(project.join("notes").join(".obsidian")).unwrap();
        std::fs::write(
            project.join("notes").join("idea.md"),
            "# Idea\n\nfindable\n",
        )
        .unwrap();
        let mut env = Env::new(&base).unwrap();
        for name in ["PATH", "SystemRoot", "windir", "ComSpec", "PATHEXT"] {
            if let Some(value) = std::env::var_os(name) {
                env.set(name, value);
            }
        }
        env.set("HOME", &home);
        env.set("USERPROFILE", &home);
        env.set("GIT_CEILING_DIRECTORIES", &base);
        let app = tauri::test::mock_app();
        app.manage(Shell {
            session: Arc::new(oah_core::app::Session::new(env)),
        });
        let shell = || app.state::<Shell>();
        let id = shell()
            .call(|ctx| app::project_open(ctx, &project))
            .unwrap()
            .id;
        fn req<T>(id: &str, input: T) -> WorkflowProjectReq<T> {
            WorkflowProjectReq {
                project_id: id.to_string(),
                input,
            }
        }
        fn wait<F: std::future::Future>(task: F) -> F::Output {
            tauri::async_runtime::block_on(task)
        }

        let late = wait(project_wiki_overview(req("another", Empty {}), shell())).unwrap_err();
        assert_eq!(late.code, codes::PROJECT_CHANGED);
        let overview = wait(project_wiki_overview(req(&id, Empty {}), shell())).unwrap();
        assert_eq!(overview.project_id, id);
        assert_eq!(overview.target.state, WikiTargetState::Incomplete);
        assert_eq!(overview.target.dir.as_deref(), Some("notes"));

        let setup = WikiActionReq::Setup {
            selected: None,
            today: Some(String::from("2026-01-02")),
        };
        let plan = wait(project_wiki_plan(req(&id, setup.clone()), shell())).unwrap();
        assert_eq!((plan.dir.as_str(), plan.writes), ("notes", 5));
        assert!(!project.join(".oah").exists(), "a preview wrote");

        let (sent, events) = mpsc::channel();
        let listener: Listener<WikiApplyDto> = Arc::new(move |event: &TaskEvent<WikiApplyDto>| {
            let _ = sent.send(event.clone());
        });
        let (ended, end) = mpsc::channel();
        let apply = |plan_id: &str| WikiApplyReq {
            request: setup.clone(),
            plan_id: plan_id.to_string(),
            approve_upgrade: None,
        };
        shell()
            .spawn_wiki_apply(req(&id, apply(&plan.plan_id)), listener, move |state| {
                let _ = ended.send(state);
            })
            .unwrap();
        assert_eq!(
            end.recv_timeout(Duration::from_secs(30)).unwrap(),
            TaskState::Done
        );
        let done = events
            .try_iter()
            .find_map(|event| match event {
                TaskEvent::Done(done) => Some(done),
                _ => None,
            })
            .expect("the done event");
        assert!(done.complete);
        assert_eq!(done.settings.state, WikiSettingsState::Saved);
        assert!(
            project
                .join("notes")
                .join("templates")
                .join("note.md")
                .is_file()
        );

        // The approval is used up, and a task that fails says so through its channel.
        let (sent, events) = mpsc::channel();
        let listener: Listener<WikiApplyDto> = Arc::new(move |event: &TaskEvent<WikiApplyDto>| {
            let _ = sent.send(event.clone());
        });
        let (ended, end) = mpsc::channel();
        shell()
            .spawn_wiki_apply(req(&id, apply(&plan.plan_id)), listener, move |state| {
                let _ = ended.send(state);
            })
            .unwrap();
        assert_eq!(
            end.recv_timeout(Duration::from_secs(30)).unwrap(),
            TaskState::Failed
        );
        let failed = events.try_iter().find_map(|event| match event {
            TaskEvent::Failed(error) => Some(error.code),
            _ => None,
        });
        assert_eq!(failed.as_deref(), Some(codes::PLAN_STALE));

        let search = WikiSearchReq {
            dir: None,
            query: String::from("findable"),
            max_hits: None,
        };
        let found = wait(project_wiki_search(req(&id, search.clone()), shell())).unwrap();
        assert_eq!(
            (found.hits[0].file.as_str(), found.hits[0].line),
            ("idea.md", 3)
        );
        let late = wait(project_wiki_search(req("another", search), shell())).unwrap_err();
        assert_eq!(late.code, codes::PROJECT_CHANGED);
        std::fs::remove_dir_all(&base).unwrap();
    }
}
