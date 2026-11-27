// 데스크톱 셸: 단일 인스턴스, 네이티브 폴더 선택, 명령 등록만 하고 모든 판단은 oah-core 에 맡긴다.
//! The OAH desktop shell (CONTRACTS C1, C7). It registers the single-instance plugin first, keeps the dialog
//! plugin on the Rust side only (the webview has no dialog permission), enforces the native navigation policy for
//! the seven canonical agent install links without adding IPC or capabilities, resolves the bundled CLI path,
//! registers the core operation commands, and starts the agents focus probe on
//! `WindowEvent::Focused(true)` of the main window (§2.5 tier 2). Every decision lives in `oah-core`.

mod bundled;
mod commands;
mod install_links;
mod updates;

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
        .plugin(install_links::plugin())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(updates::PendingUpdate::default())
        .setup(|app| {
            let env = oah_core::paths::Env::from_process()?;
            #[cfg(target_os = "macos")]
            let env = macos_cli_env(env);
            let bundled = bundled::cli_source(&env);
            app.manage(commands::Shell::start(env, &bundled));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Focused(true) = event
                && window.label() == MAIN_WINDOW
            {
                commands::probe_on_focus(window.app_handle());
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::app_info,
            updates::check_app_update,
            updates::install_app_update,
            commands::project_open_dialog,
            commands::project_current,
            commands::start_refresh,
            commands::task_cancel,
            commands::tasks_list,
            commands::settings_get,
            commands::settings_set,
            commands::agents_list,
            commands::start_agents_probe,
            commands::sections_show,
            commands::stages_set,
            commands::preset_apply,
            commands::workflow_status,
            commands::rules_check,
            commands::rules_sync,
            commands::project_tools_status,
            commands::project_tools_plan,
            commands::project_tools_apply,
            commands::start_review,
            commands::skills_installed,
            commands::inspect_installed_skill,
            commands::register_skill_source,
            commands::remove_installed_skill,
            commands::apply_project_skill,
        ])
        .run(tauri::generate_context!())
        .expect("the OAH desktop app could not start");
}

#[cfg(target_os = "macos")]
fn macos_cli_env(env: oah_core::paths::Env) -> oah_core::paths::Env {
    use std::path::PathBuf;

    let mut search: Vec<_> = env
        .var("PATH")
        .into_iter()
        .flat_map(std::env::split_paths)
        .collect();
    let user_folders = oah_core::paths::user_home(&env)
        .into_iter()
        .flat_map(|home| [home.join(".local/bin"), home.join(".opencode/bin")]);
    let shared_folders = [
        PathBuf::from("/opt/homebrew/bin"),
        PathBuf::from("/usr/local/bin"),
    ];
    for folder in user_folders.chain(shared_folders) {
        if !search.contains(&folder) {
            search.push(folder);
        }
    }
    match std::env::join_paths(search) {
        Ok(path) => env.with("PATH", path),
        Err(_) => env,
    }
}

#[cfg(all(test, target_os = "macos"))]
#[test]
fn macos_cli_env_keeps_existing_search_order_and_adds_standard_install_paths() {
    use oah_core::paths::Env;
    use std::ffi::OsStr;

    let env = Env::new("/")
        .unwrap()
        .with("HOME", "/oah-example-home")
        .with("PATH", "/opt/homebrew/bin:/usr/bin:/bin");
    assert_eq!(
        macos_cli_env(env).var("PATH"),
        Some(OsStr::new(
            "/opt/homebrew/bin:/usr/bin:/bin:/oah-example-home/.local/bin:/oah-example-home/.opencode/bin:/usr/local/bin"
        )),
    );
}

/// A second launch hands over to the first instance, which brings its window to the front.
fn focus_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}
