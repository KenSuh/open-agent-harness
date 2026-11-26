use std::time::Duration;

use oah_core::{ErrorDto, dto::Empty};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State, async_runtime::Mutex};
use tauri_plugin_updater::{Update, UpdaterExt};

#[derive(Default)]
pub struct PendingUpdate(Mutex<Option<Update>>);

#[derive(Serialize)]
pub struct AvailableUpdate {
    version: String,
}

#[derive(Deserialize)]
pub struct InstallUpdateReq {
    version: String,
}

#[tauri::command]
pub async fn check_app_update(
    req: Empty,
    app: AppHandle,
    pending: State<'_, PendingUpdate>,
) -> Result<Option<AvailableUpdate>, ErrorDto> {
    let Empty {} = req;
    let mut pending = pending.0.lock().await;
    *pending = None;
    let updater = app
        .updater_builder()
        .timeout(Duration::from_secs(12))
        .build()
        .map_err(check_failed)?;
    let update = updater.check().await.map_err(check_failed)?;
    let available = update.as_ref().map(|update| AvailableUpdate {
        version: update.version.clone(),
    });
    *pending = update;
    Ok(available)
}

#[tauri::command]
pub async fn install_app_update(
    req: InstallUpdateReq,
    app: AppHandle,
    pending: State<'_, PendingUpdate>,
) -> Result<(), ErrorDto> {
    let mut pending = pending.0.lock().await;
    let Some(update) = pending
        .as_mut()
        .filter(|update| update.version == req.version)
    else {
        return Err(ErrorDto {
            code: "OAH_APP_UPDATE_CHANGED".to_owned(),
            message: "The available update changed.".to_owned(),
            hint: Some("Check for app updates again.".to_owned()),
        });
    };
    update.timeout = Some(Duration::from_secs(180));
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|_| ErrorDto {
            code: "OAH_APP_UPDATE_INSTALL_FAILED".to_owned(),
            message: "OAH could not install the update.".to_owned(),
            hint: Some("Retry or use the installer from GitHub Releases.".to_owned()),
        })?;
    *pending = None;
    if !cfg!(windows) {
        app.request_restart();
    }
    Ok(())
}

fn check_failed(_: tauri_plugin_updater::Error) -> ErrorDto {
    ErrorDto {
        code: "OAH_APP_UPDATE_CHECK_FAILED".to_owned(),
        message: "OAH could not check for updates.".to_owned(),
        hint: Some("Check your connection and try again.".to_owned()),
    }
}
