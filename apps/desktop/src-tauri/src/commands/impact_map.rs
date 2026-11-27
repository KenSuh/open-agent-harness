use super::Shell;
use crate::bundled;
use oah_core::ErrorDto;
use oah_core::app;
use oah_core::dto::{ImpactMapRunDto, ImpactMapRunReq, WorkflowProjectReq};
use oah_core::tasks::{Begin, Listener, TaskEvent, TaskId};
use std::sync::Arc;
use tauri::State;
use tauri::ipc::Channel;

#[tauri::command]
pub fn start_impact_map(
    req: WorkflowProjectReq<ImpactMapRunReq>,
    on_event: Channel<TaskEvent<ImpactMapRunDto>>,
    shell: State<'_, Shell>,
) -> Result<TaskId, ErrorDto> {
    let key = shell.call(|ctx| app::impact_map_key(ctx, &req))?;
    let listener: Listener<ImpactMapRunDto> = Arc::new(move |event| {
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
                    app::run_impact_map(ctx, req, bundled::project_tools_source())
                });
            });
            Ok(id)
        }
    }
}
