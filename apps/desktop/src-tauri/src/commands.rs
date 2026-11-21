// IPC 명령: 각 명령은 인자 변환, oah-core 호출 하나, 결과 변환만 하고 아무것도 판단하지 않는다.
//! The M1-S1, M1-S2 and M2-S1 commands of CONTRACTS C15, exactly: `app_info`, `project_open_dialog`,
//! `project_current`, `start_refresh`, `task_cancel`, `tasks_list`, `settings_get`, `settings_set`, `agents_list`,
//! `start_agents_probe`, `sections_show`, `stages_set`, `preset_apply`, `workflow_status`, `rules_check` and
//! `rules_sync`. Each takes one argument named `req`; the `start_*` commands also take the task channel
//! `on_event` (JS key `onEvent`, CONTRACTS C4). A command body converts arguments, makes one `oah-core` call and
//! converts the result. The shell's own part is transport only: the native folder picker, the task channel, the
//! blocking thread a task runs on, the `scan-changed` and `agents-changed` events, and starting the agents probe
//! when the window regains focus. Whether a focus probe checks sign-in again is decided in core (60 s per agent).

use std::path::Path;
use std::sync::Arc;

use oah_core::app::{self, Ctx, Session};
use oah_core::dto::{
    AgentStatusDto, AgentsChangedDto, AgentsProbeReq, AppInfoDto, BannerDto, Empty, PresetApplyReq,
    ProjectDto, RefreshDto, RulesStateDto, RulesSyncDto, SettingsDto, SettingsSetReq, StagesSetDto,
    StagesSetReq, TaskCancelReq, TaskSnapshotDto, WorkflowDto,
};
use oah_core::error::codes;
use oah_core::paths::Env;
use oah_core::tasks::{
    Begin, CancelToken, Listener, NoProgress, TaskEvent, TaskHandle, TaskId, TaskState,
};
use oah_core::{Error, ErrorDto, hint_for};
use serde::Serialize;
use tauri::ipc::Channel;
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_dialog::DialogExt;

/// The name of the invalidation event of CONTRACTS C4.
pub const SCAN_CHANGED: &str = "scan-changed";

/// The name of the agents invalidation event of CONTRACTS C4; its payload is core's [`AgentsChangedDto`].
pub const AGENTS_CHANGED: &str = "agents-changed";

/// The result type of an agents probe task: what the session then holds for the probed agents.
type Statuses = Vec<AgentStatusDto>;

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

    /// Registers an agents probe task under core's key for `req` (CONTRACTS C4): a second start of the same request
    /// while it runs is refused with `OAH_TASK_BUSY`; a different request (a rescan beside a focus probe) runs.
    fn begin_agents_probe(
        &self,
        req: &AgentsProbeReq,
        listener: Listener<Statuses>,
    ) -> Result<Begin<Statuses>, Error> {
        self.session
            .tasks()
            .begin(app::agents_probe_key(req), listener)
    }

    /// Starts `start_agents_probe`'s work for `req` on Tauri's blocking pool and returns its task id at once. When
    /// the task ends `done`, `agents-changed` names the probed agents.
    fn spawn_agents_probe(
        &self,
        req: AgentsProbeReq,
        listener: Listener<Statuses>,
        app: AppHandle,
    ) -> Result<TaskId, ErrorDto> {
        match self
            .begin_agents_probe(&req, listener)
            .map_err(|error| self.to_dto(&error))?
        {
            Begin::Joined(id) => Ok(id),
            Begin::Started(handle) => {
                let id = handle.id().clone();
                let session = Arc::clone(&self.session);
                tauri::async_runtime::spawn_blocking(move || {
                    let changed =
                        run_agents_probe(&session, handle, |ctx| app::agents_probe(ctx, req));
                    if let Some(changed) = changed {
                        let _ = app.emit(AGENTS_CHANGED, changed);
                    }
                });
                Ok(id)
            }
        }
    }
}

/// Runs a started agents probe with `work` and finishes its task. Returns the `agents-changed` payload only when
/// the task ended `done` with at least one agent; a cancelled or failed probe published nothing.
fn run_agents_probe(
    session: &Session,
    handle: TaskHandle<Statuses>,
    work: impl FnOnce(&Ctx<'_>) -> Result<Statuses, Error>,
) -> Option<AgentsChangedDto> {
    let mut changed = None;
    let state = app::run_task(session, handle, |ctx| {
        let result = work(ctx);
        if let Ok(statuses) = &result {
            changed = Some(changed_of(statuses));
        }
        result
    });
    changed.filter(|changed| state == TaskState::Done && !changed.agents.is_empty())
}

fn changed_of(statuses: &[AgentStatusDto]) -> AgentsChangedDto {
    AgentsChangedDto {
        agents: statuses.iter().map(|status| status.id).collect(),
    }
}

/// The agents the session holds after a refresh, which re-probes them (M1-S2); `None` when it holds none.
fn held_agents(session: &Session) -> Option<AgentsChangedDto> {
    let cancel = CancelToken::new();
    let held = app::agents_list(&session.ctx(&cancel, &NoProgress), Empty {}).ok()?;
    Some(changed_of(&held)).filter(|changed| !changed.agents.is_empty())
}

/// The request a regained window focus sends (§2.5 tier 2): every agent is detected again at once, and core
/// checks an agent's sign-in again only when its last check is 60 seconds old or older.
pub fn focus_probe_req() -> AgentsProbeReq {
    AgentsProbeReq {
        agents: None,
        sign_in: true,
        focus: true,
    }
}

/// `WindowEvent::Focused(true)` of the main window: starts the focus probe. Nobody follows its channel; the UI
/// learns the result from `agents-changed`. A focus probe that is still running refuses the next one
/// (`OAH_TASK_BUSY`), which is not an error here. A focus event before `setup` managed the shell is ignored.
pub fn probe_on_focus(app: &AppHandle) {
    let Some(shell) = app.try_state::<Shell>() else {
        return;
    };
    let outcome = shell.spawn_agents_probe(focus_probe_req(), Arc::new(|_| {}), app.clone());
    // U-17 evidence in debug builds: the focus event arrived and what the probe start did. No agent output.
    #[cfg(debug_assertions)]
    eprintln!(
        "oah-desktop: window focus: agents probe {}",
        match &outcome {
            Ok(id) => format!("started {}", id.as_str()),
            Err(error) => format!("not started ({})", error.code),
        }
    );
    #[cfg(not(debug_assertions))]
    drop(outcome);
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
                    if let Some(changed) = held_agents(&session) {
                        let _ = app.emit(AGENTS_CHANGED, changed);
                    }
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

/// The last status of every probed agent from the session cache; empty before the first probe.
#[tauri::command]
pub fn agents_list(req: Empty, shell: State<'_, Shell>) -> Result<Vec<AgentStatusDto>, ErrorDto> {
    shell.call(|ctx| app::agents_list(ctx, req))
}

/// Starts an agents probe task (CONTRACTS C4, C15). Its events go to `on_event`; the work runs on Tauri's
/// blocking pool, and a `done` end also emits `agents-changed`.
#[tauri::command]
pub fn start_agents_probe(
    req: AgentsProbeReq,
    on_event: Channel<TaskEvent<Statuses>>,
    app: AppHandle,
    shell: State<'_, Shell>,
) -> Result<TaskId, ErrorDto> {
    let listener: Listener<Statuses> = Arc::new(move |event: &TaskEvent<Statuses>| {
        let _ = on_event.send(event.clone());
    });
    shell.spawn_agents_probe(req, listener, app)
}

/// The Team Workflow page of the open project (M2-S1, read).
#[tauri::command]
pub fn sections_show(req: Empty, shell: State<'_, Shell>) -> Result<WorkflowDto, ErrorDto> {
    shell.call(|ctx| app::sections_show(ctx, req))
}

/// One assignment: config save, then the rules re-sync (M2-S1, write-own).
#[tauri::command]
pub fn stages_set(req: StagesSetReq, shell: State<'_, Shell>) -> Result<StagesSetDto, ErrorDto> {
    shell.call(|ctx| app::stages_set(ctx, req))
}

/// A preset's assignments as one `stages_set` (M2-S1, write-own).
#[tauri::command]
pub fn preset_apply(
    req: PresetApplyReq,
    shell: State<'_, Shell>,
) -> Result<StagesSetDto, ErrorDto> {
    shell.call(|ctx| app::preset_apply(ctx, req))
}

/// The one banner of the open project (M2-S1, read).
#[tauri::command]
pub fn workflow_status(req: Empty, shell: State<'_, Shell>) -> Result<BannerDto, ErrorDto> {
    shell.call(|ctx| app::workflow_status(ctx, req))
}

/// Rules drift without writing (M2-S1, read).
#[tauri::command]
pub fn rules_check(req: Empty, shell: State<'_, Shell>) -> Result<RulesStateDto, ErrorDto> {
    shell.call(|ctx| app::rules_check(ctx, req))
}

/// *Sync now*: the rules block and thin files written from the config (M2-S1, write-own).
#[tauri::command]
pub fn rules_sync(req: Empty, shell: State<'_, Shell>) -> Result<RulesSyncDto, ErrorDto> {
    shell.call(|ctx| app::rules_sync(ctx, req))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_changed_is_the_c4_payload() {
        let json = serde_json::to_string(&ScanChanged::all()).unwrap();
        assert_eq!(json, r#"{"scope":"all","keys":[]}"#);
    }

    use oah_core::agents::{Os, Probe, Resolution, Resolved, RunError, RunOutput};
    use oah_core::config::AgentId;
    use oah_core::dto::AgentIdDto;
    use std::path::PathBuf;
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    /// A synthetic machine with only Codex installed and signed out. It answers from memory: it starts no process
    /// and reads no disk, and it counts the sign-in runs.
    struct CodexOnly {
        env: Env,
        sign_in_runs: AtomicUsize,
    }

    impl CodexOnly {
        fn new(env: &Env) -> CodexOnly {
            CodexOnly {
                env: env.clone(),
                sign_in_runs: AtomicUsize::new(0),
            }
        }
    }

    impl Probe for CodexOnly {
        fn env(&self) -> &Env {
            &self.env
        }
        fn os(&self) -> Os {
            Os::host()
        }
        fn resolve(&self, command: &str) -> Resolution {
            if command != "codex" {
                return Resolution::Missing;
            }
            Resolution::Found(Resolved {
                argv: vec!["codex".into()],
                source: PathBuf::from("/synthetic/bin/codex"),
            })
        }
        fn resolve_in(&self, _command: &str, _folder: &std::path::Path) -> Resolution {
            Resolution::Missing
        }
        fn run(
            &self,
            _program: &Resolved,
            args: &[&str],
            _deadline: Duration,
        ) -> Result<RunOutput, RunError> {
            if args.first() == Some(&"--version") {
                return Ok(output(0, "codex-cli 0.163.0\n"));
            }
            self.sign_in_runs.fetch_add(1, Ordering::SeqCst);
            Ok(output(1, ""))
        }
        fn exists(&self, _path: &std::path::Path) -> bool {
            false
        }
    }

    fn output(exit: i32, stdout: &str) -> RunOutput {
        RunOutput {
            exit: Some(exit),
            stdout: stdout.to_string(),
            stderr: String::new(),
        }
    }

    /// A shell over a session with an environment that has no variables: nothing it runs here can find a
    /// real program or home folder.
    fn shell() -> Shell {
        let env = Env::new(std::env::temp_dir()).unwrap();
        Shell {
            session: Arc::new(Session::new(env)),
        }
    }

    type Events = Arc<Mutex<Vec<String>>>;

    fn recorder() -> (Events, Listener<Statuses>) {
        let events: Events = Arc::default();
        let sink = Arc::clone(&events);
        let listener: Listener<Statuses> = Arc::new(move |event: &TaskEvent<Statuses>| {
            let name = serde_json::to_value(event).unwrap()["event"]
                .as_str()
                .unwrap()
                .to_string();
            sink.lock().unwrap().push(name);
        });
        (events, listener)
    }

    fn started(begun: Result<Begin<Statuses>, Error>) -> TaskHandle<Statuses> {
        match begun {
            Ok(Begin::Started(handle)) => handle,
            Ok(Begin::Joined(id)) => panic!("an agents probe never joins, joined {}", id.as_str()),
            Err(error) => panic!("refused: {}", error.to_dto(&Env::new("/").unwrap()).code),
        }
    }

    /// Runs one probe of `req` through the shell's begin and run steps with `probe`.
    fn probe(
        shell: &Shell,
        req: AgentsProbeReq,
        probe: &CodexOnly,
    ) -> (Option<AgentsChangedDto>, Vec<String>) {
        let (events, listener) = recorder();
        let handle = started(shell.begin_agents_probe(&req, listener));
        let changed = run_agents_probe(&shell.session, handle, |ctx| {
            app::agents_probe_with(ctx, req, probe)
        });
        let events = events.lock().unwrap().clone();
        (changed, events)
    }

    fn list(shell: &Shell) -> Vec<AgentStatusDto> {
        shell.call(|ctx| app::agents_list(ctx, Empty {})).unwrap()
    }

    #[test]
    fn a_done_probe_fills_the_cache_and_names_every_probed_agent_in_agents_changed() {
        let shell = shell();
        assert!(
            list(&shell).is_empty(),
            "nothing is cached before the first probe"
        );
        let machine = CodexOnly::new(shell.session.env());
        let req = AgentsProbeReq {
            agents: None,
            sign_in: true,
            focus: false,
        };
        let (changed, events) = probe(&shell, req, &machine);

        let all: Vec<AgentIdDto> = AgentId::ALL.into_iter().map(AgentIdDto).collect();
        assert_eq!(changed, Some(AgentsChangedDto { agents: all }));
        assert_eq!(events.first().map(String::as_str), Some("progress"));
        assert_eq!(events.last().map(String::as_str), Some("done"));
        let cached = list(&shell);
        assert_eq!(cached.len(), AgentId::ALL.len());
        let codex = cached.iter().find(|s| s.id.0 == AgentId::Codex).unwrap();
        let json = serde_json::to_value(codex).unwrap();
        assert_eq!(json["status"], "not-signed-in");
        assert_eq!(json["sign_in"], "signed-out");
        let claude = cached.iter().find(|s| s.id.0 == AgentId::Claude).unwrap();
        assert!(!claude.installed);
        assert_eq!(serde_json::to_value(claude).unwrap()["status"], "missing");
    }

    #[test]
    fn agents_changed_is_core_dto_in_the_shared_sample_shape() {
        let sample = include_str!(
            "../../../../crates/oah-core/tests/dto_samples/agents/agents-changed.json"
        );
        let payload = AgentsChangedDto {
            agents: vec![AgentIdDto(AgentId::Claude), AgentIdDto(AgentId::Codex)],
        };
        assert_eq!(
            serde_json::to_value(payload).unwrap(),
            serde_json::from_str::<serde_json::Value>(sample).unwrap()
        );
    }

    #[test]
    fn the_focus_request_is_every_agent_with_core_throttled_sign_in() {
        let json = serde_json::to_value(focus_probe_req()).unwrap();
        assert_eq!(
            json,
            serde_json::json!({ "agents": null, "sign_in": true, "focus": true })
        );
    }

    #[test]
    fn a_second_focus_inside_the_interval_redetects_without_a_second_sign_in_run() {
        let shell = shell();
        let machine = CodexOnly::new(shell.session.env());
        let (first, _) = probe(&shell, focus_probe_req(), &machine);
        assert!(first.is_some());
        assert_eq!(machine.sign_in_runs.load(Ordering::SeqCst), 1);
        let before = list(&shell);

        let (second, events) = probe(&shell, focus_probe_req(), &machine);
        assert!(
            second.is_some(),
            "detection ran again and the agents changed event follows"
        );
        assert_eq!(events.last().map(String::as_str), Some("done"));
        assert_eq!(
            machine.sign_in_runs.load(Ordering::SeqCst),
            1,
            "core keeps the 60-second sign-in limit"
        );
        let codex = |all: &[AgentStatusDto]| {
            serde_json::to_value(all.iter().find(|s| s.id.0 == AgentId::Codex).unwrap()).unwrap()
                ["sign_in"]
                .clone()
        };
        assert_eq!(
            codex(&list(&shell)),
            codex(&before),
            "the held sign-in is kept"
        );

        let rescan = AgentsProbeReq {
            agents: None,
            sign_in: true,
            focus: false,
        };
        probe(&shell, rescan, &machine);
        assert_eq!(
            machine.sign_in_runs.load(Ordering::SeqCst),
            2,
            "a rescan always checks sign-in"
        );
    }

    #[test]
    fn a_running_focus_probe_refuses_another_focus_start_but_not_a_rescan() {
        let shell = shell();
        let (_events, listener) = recorder();
        let running = started(shell.begin_agents_probe(&focus_probe_req(), listener));

        let (_events, listener) = recorder();
        let again = shell.begin_agents_probe(&focus_probe_req(), listener);
        let refused = again.err().map(|error| shell.to_dto(&error).code);
        assert_eq!(refused.as_deref(), Some(codes::TASK_BUSY));

        let rescan = AgentsProbeReq {
            agents: None,
            sign_in: true,
            focus: false,
        };
        let (_events, listener) = recorder();
        let beside = started(shell.begin_agents_probe(&rescan, listener));
        let kinds: Vec<_> = shell
            .session
            .tasks()
            .list()
            .into_iter()
            .map(|t| t.kind)
            .collect();
        assert_eq!(kinds, [app::AGENTS_PROBE, app::AGENTS_PROBE]);
        drop((running, beside));
        assert!(shell.session.tasks().list().is_empty());
    }

    #[test]
    fn a_cancelled_probe_ends_cancelled_and_emits_no_agents_changed() {
        let shell = shell();
        let machine = CodexOnly::new(shell.session.env());
        let req = AgentsProbeReq::default();
        let (events, listener) = recorder();
        let handle = started(shell.begin_agents_probe(&req, listener));
        assert!(shell.session.tasks().cancel(handle.id().as_str()));
        let changed = run_agents_probe(&shell.session, handle, |ctx| {
            app::agents_probe_with(ctx, req, &machine)
        });
        assert_eq!(changed, None);
        assert_eq!(
            events.lock().unwrap().last().map(String::as_str),
            Some("cancelled")
        );
        assert!(
            list(&shell).is_empty(),
            "a cancelled probe publishes nothing"
        );
    }

    #[test]
    fn after_a_refresh_agents_changed_names_the_held_agents_only() {
        let shell = shell();
        assert_eq!(
            held_agents(&shell.session),
            None,
            "no agent is held before a probe"
        );
        let machine = CodexOnly::new(shell.session.env());
        let req = AgentsProbeReq {
            agents: Some(vec![AgentIdDto(AgentId::Codex)]),
            sign_in: false,
            focus: false,
        };
        probe(&shell, req, &machine);
        assert_eq!(
            held_agents(&shell.session),
            Some(AgentsChangedDto {
                agents: vec![AgentIdDto(AgentId::Codex)]
            })
        );
    }
}
