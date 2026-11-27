use super::Shell;
use crate::bundled;
use oah_core::dto::{JevInput, JevReply, WikiIndexInput, WikiIndexReply, WorkflowProjectReq};
use oah_core::tasks::{Begin, Listener, TaskEvent, TaskId};
use oah_core::{ErrorDto, app};
use std::sync::Arc;
use tauri::{State, ipc::Channel};

#[tauri::command]
pub async fn project_jev_action(
    req: WorkflowProjectReq<JevInput>,
    shell: State<'_, Shell>,
) -> Result<JevReply, ErrorDto> {
    let shell = Shell {
        session: Arc::clone(&shell.session),
    };
    tauri::async_runtime::spawn_blocking(move || {
        shell.call(|ctx| app::project_jev_action(ctx, req, bundled::project_tools_source()))
    })
    .await
    .map_err(|_| ErrorDto {
        code: "OAH_PROJECT_TOOLS_INTERRUPTED".to_owned(),
        message: "The Jev action ended before reporting its result.".to_owned(),
        hint: Some("Reopen Manage Jev to check this device's current state.".to_owned()),
    })?
}

#[tauri::command]
pub fn start_wiki_index(
    req: WorkflowProjectReq<WikiIndexInput>,
    on_event: Channel<TaskEvent<WikiIndexReply>>,
    shell: State<'_, Shell>,
) -> Result<TaskId, ErrorDto> {
    let key = shell.call(|ctx| app::wiki_index_key(ctx, &req))?;
    let listener: Listener<WikiIndexReply> = Arc::new(move |event| {
        let _ = on_event.send(event.clone());
    });
    let begun = shell
        .session
        .tasks()
        .begin(key, listener)
        .map_err(|error| shell.to_dto(&error))?;
    match begun {
        Begin::Joined(id) => Ok(id),
        Begin::Started(handle) => {
            let id = handle.id().clone();
            let session = Arc::clone(&shell.session);
            tauri::async_runtime::spawn_blocking(move || {
                app::run_task(&session, handle, |ctx| {
                    app::project_wiki_index(ctx, req, bundled::project_tools_source())
                });
            });
            Ok(id)
        }
    }
}
