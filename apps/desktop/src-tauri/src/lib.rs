// 데스크톱 셸: 단일 인스턴스, 네이티브 폴더 선택, 명령 등록만 하고 모든 판단은 oah-core 에 맡긴다.
//! The OAH desktop shell (CONTRACTS C1, C7). It registers the single-instance plugin first, keeps the dialog
//! plugin on the Rust side only (the webview has no dialog permission), resolves the bundled CLI path, and
//! registers exactly the M1-S1 commands of CONTRACTS C15. Every decision lives in `oah-core`.

mod bundled;
mod commands;

use tauri::Manager;

/// The label of the one window; `capabilities/default.json` grants it `core:default` only.
pub const MAIN_WINDOW: &str = "main";

pub fn run() {
    tauri::Builder::default()
        // The single-instance plugin must be the first plugin registered.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            focus_main_window(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let env = oah_core::paths::Env::from_process()?;
            let bundled = bundled::cli_source(&env);
            app.manage(commands::Shell::start(env, &bundled));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::app_info,
            commands::project_open_dialog,
            commands::project_current,
            commands::start_refresh,
            commands::task_cancel,
            commands::tasks_list,
            commands::settings_get,
            commands::settings_set,
        ])
        .run(tauri::generate_context!())
        .expect("the OAH desktop app could not start");
}

/// A second launch hands over to the first instance, which brings its window to the front.
fn focus_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}
