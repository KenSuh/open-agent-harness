// IPC 명령: 각 명령은 인자 변환, oah-core 호출 하나, 결과 변환만 하고 아무것도 판단하지 않는다.
//! The M1-S1 commands of CONTRACTS C15, exactly: `app_info`, `project_open_dialog`, `project_current`,
//! `start_refresh`, `task_cancel`, `tasks_list`, `settings_get`, `settings_set`. Each takes one argument named
//! `req`; `start_refresh` also takes the task channel `on_event` (JS key `onEvent`, CONTRACTS C4). A command body
//! converts arguments, makes one `oah-core` call and converts the result. The shell's own part is transport only:
//! the native folder picker, the task channel, the blocking thread a task runs on, and the `scan-changed` event.

use std::path::Path;
use std::sync::Arc;

use oah_core::app::{self, Ctx, Session};
use oah_core::dto::{
    AppInfoDto, Empty, ProjectDto, RefreshDto, SettingsDto, SettingsSetReq, TaskCancelReq,
    TaskSnapshotDto,
};
use oah_core::error::codes;
use oah_core::paths::Env;
use oah_core::tasks::{Begin, CancelToken, Listener, NoProgress, TaskEvent, TaskId, TaskState};
use oah_core::{Error, ErrorDto, hint_for};
use serde::Serialize;
use tauri::ipc::Channel;
use tauri::{AppHandle, Emitter, State};
use tauri_plugin_dialog::DialogExt;

/// The name of the invalidation event of CONTRACTS C4.
pub const SCAN_CHANGED: &str = "scan-changed";

/// The `scan-changed` payload of CONTRACTS C4. Core has no emitter and no type for it; M1-S1 only ever sends
/// `{ scope: "all", keys: [] }`, after a project opened or a refresh ended `done`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ScanChanged {
    pub scope: &'static str,
    pub keys: Vec<String>,
}

impl ScanChanged {
    pub fn all() -> ScanChanged {
        ScanChanged {
            scope: "all",
            keys: Vec::new(),
        }
    }
}

/// The shell's managed state: the one core session of this process (CONTRACTS C2).
pub struct Shell {
    session: Arc<Session>,
}

impl Shell {
    /// Creates the session and runs the start-time CLI copy (CONTRACTS C7) before any command can be served.
    /// The copy never fails; its state and its notice stay in the session, and `app_info` reports both, so a
    /// window that loads later still sees a copy failure.
    pub fn start(env: Env, bundled: &Path) -> Shell {
        let session = Arc::new(Session::new(env));
        let cancel = CancelToken::new();
        app::cli_copy(&session.ctx(&cancel, &NoProgress), bundled);
        Shell { session }
    }

    /// Runs one core operation with a context that cannot be cancelled and reports no progress.
    fn call<R>(&self, op: impl FnOnce(&Ctx<'_>) -> Result<R, Error>) -> Result<R, ErrorDto> {
        let cancel = CancelToken::new();
        op(&self.session.ctx(&cancel, &NoProgress)).map_err(|error| self.to_dto(&error))
    }

    fn to_dto(&self, error: &Error) -> ErrorDto {
        error.to_dto(self.session.env())
    }
}

fn emit_scan_changed(app: &AppHandle) {
    let _ = app.emit(SCAN_CHANGED, ScanChanged::all());
}

#[tauri::command]
pub fn app_info(req: Empty, shell: State<'_, Shell>) -> Result<AppInfoDto, ErrorDto> {
    shell.call(|ctx| app::app_info(ctx, req))
}

/// Opens the native folder picker from Rust (the webview has no dialog permission) and opens the chosen folder
/// read-only. A cancelled picker returns `null` without calling core.
#[tauri::command]
pub async fn project_open_dialog(
    req: Empty,
    app: AppHandle,
    shell: State<'_, Shell>,
) -> Result<Option<ProjectDto>, ErrorDto> {
    let Empty {} = req;
    // Async commands run off the main thread, where the blocking picker is allowed.
    let Some(picked) = app.dialog().file().blocking_pick_folder() else {
        return Ok(None);
    };
    let folder = picked.into_path().map_err(|_| not_a_local_folder())?;
    let project = shell.call(|ctx| app::project_open(ctx, &folder))?;
    emit_scan_changed(&app);
    Ok(Some(project))
}

/// The picker answered with something that is not a local path (possible only on mobile platforms).
fn not_a_local_folder() -> ErrorDto {
    ErrorDto {
        code: codes::PROJECT_NOT_FOLDER.to_string(),
        message: "the chosen item is not a local folder".to_string(),
        hint: hint_for(codes::PROJECT_NOT_FOLDER).map(str::to_string),
    }
}

#[tauri::command]
pub fn project_current(
    req: Empty,
    shell: State<'_, Shell>,
) -> Result<Option<ProjectDto>, ErrorDto> {
    shell.call(|ctx| app::project_current(ctx, req))
}

/// Starts the refresh task, or joins the running one (CONTRACTS C4). Its events go to `on_event`; the work runs
/// on Tauri's blocking pool.
#[tauri::command]
pub fn start_refresh(
    req: Empty,
    on_event: Channel<TaskEvent<RefreshDto>>,
    app: AppHandle,
    shell: State<'_, Shell>,
) -> Result<TaskId, ErrorDto> {
    let listener: Listener<RefreshDto> = Arc::new(move |event: &TaskEvent<RefreshDto>| {
        let _ = on_event.send(event.clone());
    });
    let begun = shell
        .session
        .tasks()
        .begin(app::refresh_key(), listener)
        .map_err(|error| shell.to_dto(&error))?;
    match begun {
        Begin::Joined(id) => Ok(id),
        Begin::Started(handle) => {
            let id = handle.id().clone();
            let session = Arc::clone(&shell.session);
            tauri::async_runtime::spawn_blocking(move || {
                let state = app::run_task(&session, handle, |ctx| app::refresh(ctx, req));
                if state == TaskState::Done {
                    emit_scan_changed(&app);
                }
            });
            Ok(id)
        }
    }
}

#[tauri::command]
pub fn task_cancel(req: TaskCancelReq, shell: State<'_, Shell>) -> Result<Empty, ErrorDto> {
    shell.call(|ctx| app::task_cancel(ctx, req))
}

#[tauri::command]
pub fn tasks_list(req: Empty, shell: State<'_, Shell>) -> Result<Vec<TaskSnapshotDto>, ErrorDto> {
    shell.call(|ctx| app::tasks_list(ctx, req))
}

#[tauri::command]
pub fn settings_get(req: Empty, shell: State<'_, Shell>) -> Result<SettingsDto, ErrorDto> {
    shell.call(|ctx| app::settings_get(ctx, req))
}

#[tauri::command]
pub fn settings_set(req: SettingsSetReq, shell: State<'_, Shell>) -> Result<SettingsDto, ErrorDto> {
    shell.call(|ctx| app::settings_set(ctx, req))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_changed_is_the_c4_payload() {
        let json = serde_json::to_string(&ScanChanged::all()).unwrap();
        assert_eq!(json, r#"{"scope":"all","keys":[]}"#);
    }
}
