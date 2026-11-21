// 에이전트 작업: 세션에 에이전트마다 마지막 상태를 두고, 고른 에이전트를 나란히 확인하며, 포커스 확인의 로그인은 에이전트마다 60초에 한 번만 한다.
//! `agents_list` (read) and `start_agents_probe` (probe) of CONTRACTS C15. Each agent is probed on its own thread
//! with its own deadlines, so one agent's failure is that agent's status only. The session keeps the newest
//! status of every probed agent and when its sign-in was last checked; that 60-second limit lives here, not in
//! the UI (AC6).

use super::{Ctx, lock};
use crate::agents::{
    AgentError, Check, Detection, Failure, Os, Probe, SignInCheck, Status, SystemProbe,
    check_sign_in, detect,
};
use crate::config::AgentId;
use crate::dto::{AgentIdDto, AgentStatusDto, AgentsProbeReq, Empty};
use crate::error::Error;
use crate::tasks::{Progress, TaskKey};
use std::collections::BTreeMap;
use std::collections::btree_map::Entry;
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime};

/// The task kind of an agents probe.
pub const AGENTS_PROBE: &str = "agents-probe";

/// After a focus event, an agent's sign-in is checked again only when its last check is this old (AC6).
pub const SIGN_IN_INTERVAL: Duration = Duration::from_secs(60);

const PHASE: &str = "probe-agents";

/// The newest status of each probed agent.
#[derive(Debug, Default)]
pub(crate) struct AgentCache {
    issued: u64,
    held: BTreeMap<AgentId, Held>,
    focus_checks: BTreeMap<AgentId, u64>,
}

#[derive(Debug)]
struct Held {
    detection_stamp: u64,
    check: Check,
    os: Os,
    found_at: SystemTime,
    sign_in_stamp: Option<u64>,
    /// Latest start time among accepted sign-in checks for this agent.
    last_sign_in_at: Option<Instant>,
}

impl AgentCache {
    fn publish(&mut self, id: AgentId, next: Held) {
        let Some(current) = self.held.get_mut(&id) else {
            self.held.insert(id, next);
            return;
        };
        let compatible = same_detection(&current.check.detection, &next.check.detection);
        let newer_detection = next.detection_stamp > current.detection_stamp;
        if newer_detection {
            current.detection_stamp = next.detection_stamp;
            current.check.detection = next.check.detection;
            current.os = next.os;
            current.found_at = next.found_at;
            if !compatible {
                current.check.sign_in = SignInCheck::NotChecked;
                current.sign_in_stamp = None;
            }
        }
        if (newer_detection || compatible) && next.sign_in_stamp > current.sign_in_stamp {
            current.check.sign_in = next.check.sign_in;
            current.sign_in_stamp = next.sign_in_stamp;
            current.last_sign_in_at = current.last_sign_in_at.max(next.last_sign_in_at);
        }
    }
}

struct ProbeRun<'a> {
    cache: &'a Mutex<AgentCache>,
    stamp: u64,
    released: bool,
}

impl<'a> ProbeRun<'a> {
    fn reserve_focus(&self, id: AgentId, now: Instant) -> bool {
        let mut cache = lock(self.cache);
        let last = cache.held.get(&id).and_then(|held| held.last_sign_in_at);
        if !sign_in_due(last, now) {
            return false;
        }
        match cache.focus_checks.entry(id) {
            Entry::Occupied(_) => false,
            Entry::Vacant(reservation) => {
                reservation.insert(self.stamp);
                true
            }
        }
    }

    fn finish(mut self) -> MutexGuard<'a, AgentCache> {
        let mut cache = lock(self.cache);
        self.release(&mut cache);
        cache
    }

    fn release(&mut self, cache: &mut AgentCache) {
        cache.focus_checks.retain(|_, stamp| *stamp != self.stamp);
        self.released = true;
    }
}

impl Drop for ProbeRun<'_> {
    fn drop(&mut self) {
        if !self.released {
            self.release(&mut lock(self.cache));
        }
    }
}

/// Whether a focus probe checks sign-in again: never checked, or checked [`SIGN_IN_INTERVAL`] ago or longer.
pub fn sign_in_due(last: Option<Instant>, now: Instant) -> bool {
    last.is_none_or(|last| now.saturating_duration_since(last) >= SIGN_IN_INTERVAL)
}

/// The probe's task key, one per distinct request (CONTRACTS C4): a second start of the same request while it
/// runs is refused with `OAH_TASK_BUSY`; different requests run side by side and the newest publishes.
pub fn agents_probe_key(req: &AgentsProbeReq) -> TaskKey {
    let agents: Vec<&str> = requested(req).iter().map(|id| id.as_str()).collect();
    let subject = format!(
        "{};sign_in={};focus={}",
        agents.join(","),
        req.sign_in,
        req.focus
    );
    TaskKey::exclusive(AGENTS_PROBE, subject)
}

/// `agents_list`: the last status of every probed agent, in display order; empty before the first probe.
pub fn agents_list(ctx: &Ctx<'_>, _req: Empty) -> Result<Vec<AgentStatusDto>, Error> {
    let cache = lock(&ctx.session.agents);
    Ok(cache.held.values().map(|held| dto(ctx, held)).collect())
}

/// The refresh step (§2.5, manual refresh: all probes): re-probes every agent the session holds, with sign-in.
/// Before the first probe the session holds none, so a refresh then starts no agent program.
pub(crate) fn reprobe_held(ctx: &Ctx<'_>, probe: &dyn Probe) -> Result<(), Error> {
    let held: Vec<AgentIdDto> = lock(&ctx.session.agents)
        .held
        .keys()
        .map(|id| AgentIdDto(*id))
        .collect();
    if held.is_empty() {
        return Ok(());
    }
    let req = AgentsProbeReq {
        agents: Some(held),
        sign_in: true,
        focus: false,
    };
    agents_probe_with(ctx, req, probe).map(drop)
}

/// `start_agents_probe`'s work on this machine.
pub fn agents_probe(ctx: &Ctx<'_>, req: AgentsProbeReq) -> Result<Vec<AgentStatusDto>, Error> {
    agents_probe_with(ctx, req, &SystemProbe::new(ctx.env))
}

/// Probes the requested agents through `probe` using installed machine lookups only (ignoring any open project's
/// `[agents.<id>] command`) and returns what the session then holds for them. The session keeps the newest
/// detection and newest compatible sign-in result independently. A cancelled probe publishes nothing.
pub fn agents_probe_with(
    ctx: &Ctx<'_>,
    req: AgentsProbeReq,
    probe: &dyn Probe,
) -> Result<Vec<AgentStatusDto>, Error> {
    let no_commands = BTreeMap::new();
    probe_with_commands(ctx, req, probe, &no_commands)
}

/// Probes the requested agents through `probe` with explicit caller-supplied command overrides.
///
/// Kept `pub(super)` so only the explicit terminal `doctor` entry can supply project-configured commands;
/// automatic app, focus and refresh probes enter through [`agents_probe_with`] with an empty override map.
pub(super) fn probe_with_commands(
    ctx: &Ctx<'_>,
    req: AgentsProbeReq,
    probe: &dyn Probe,
    commands: &BTreeMap<AgentId, Vec<String>>,
) -> Result<Vec<AgentStatusDto>, Error> {
    ctx.cancel.check()?;
    let ids = requested(&req);
    let stamp = {
        let mut cache = lock(&ctx.session.agents);
        cache.issued += 1;
        cache.issued
    };
    let run = ProbeRun {
        cache: &ctx.session.agents,
        stamp,
        released: false,
    };
    let total = u32::try_from(ids.len()).unwrap_or(u32::MAX);
    ctx.progress.progress(Progress {
        phase: PHASE,
        done: 0,
        total: Some(total),
    });
    let results = std::thread::scope(|scope| {
        let running: Vec<_> = ids
            .iter()
            .map(|&id| {
                let (req, run, command) = (&req, &run, commands.get(&id).map(Vec::as_slice));
                (
                    id,
                    scope.spawn(move || probe_one(id, probe, command, req, run)),
                )
            })
            .collect();
        let mut results = Vec::with_capacity(running.len());
        for (index, (id, thread)) in running.into_iter().enumerate() {
            let result = thread.join().unwrap_or_else(|_| (stopped(id), None));
            let done = u32::try_from(index + 1).unwrap_or(u32::MAX);
            ctx.progress.progress(Progress {
                phase: PHASE,
                done,
                total: Some(total),
            });
            results.push((id, result));
        }
        results
    });
    ctx.cancel.check()?;
    let (os, found_at) = (probe.os(), SystemTime::now());
    let mut cache = run.finish();
    ctx.cancel.check()?;
    for (id, (check, signed_at)) in results {
        cache.publish(
            id,
            Held {
                detection_stamp: stamp,
                check,
                os,
                found_at,
                sign_in_stamp: signed_at.map(|_| stamp),
                last_sign_in_at: signed_at,
            },
        );
    }
    Ok(ids
        .iter()
        .filter_map(|id| cache.held.get(id))
        .map(|held| dto(ctx, held))
        .collect())
}

fn dto(ctx: &Ctx<'_>, held: &Held) -> AgentStatusDto {
    AgentStatusDto::new(ctx.env, &held.check, held.os, held.found_at)
}

/// The requested agents in display order, each once.
fn requested(req: &AgentsProbeReq) -> Vec<AgentId> {
    AgentId::ALL
        .into_iter()
        .filter(|id| {
            req.agents
                .as_ref()
                .is_none_or(|agents| agents.contains(&AgentIdDto(*id)))
        })
        .collect()
}

fn probe_one(
    id: AgentId,
    probe: &dyn Probe,
    command: Option<&[String]>,
    req: &AgentsProbeReq,
    run: &ProbeRun<'_>,
) -> (Check, Option<Instant>) {
    let detection = detect(id, probe, command);
    let now = Instant::now();
    let sign_in = if req.sign_in
        && detection.status == Status::Ok
        && (!req.focus || run.reserve_focus(id, now))
    {
        check_sign_in(id, probe, command, &detection)
    } else {
        SignInCheck::NotChecked
    };
    let signed_at = (sign_in != SignInCheck::NotChecked).then_some(now);
    (Check { detection, sign_in }, signed_at)
}

fn same_detection(left: &Detection, right: &Detection) -> bool {
    (left.status, &left.version, &left.source) == (right.status, &right.version, &right.source)
}

/// The result of an agent whose probe thread stopped by a defect; it is that agent's error only.
fn stopped(id: AgentId) -> Check {
    let failure = Failure::Launch(String::from("the check stopped unexpectedly"));
    let problem = AgentError::ProbeFailed {
        command: format!("{id} probe"),
        failure,
    };
    Check {
        detection: Detection::failed(id, None, None, problem),
        sign_in: SignInCheck::NotChecked,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agents::{Resolution, Resolved, RunError, RunOutput};
    use crate::app::{Session, project_open};
    use crate::paths::{self, Env};
    use crate::tasks::{CancelToken, NoProgress};
    use std::ffi::OsString;
    use std::fs;
    use std::path::{Path, PathBuf};

    const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };

    struct RecordingProbe {
        env: Env,
        asked: Mutex<Vec<String>>,
    }

    impl RecordingProbe {
        fn new(env: Env) -> RecordingProbe {
            RecordingProbe {
                env,
                asked: Mutex::default(),
            }
        }

        fn asked(&self) -> Vec<String> {
            lock(&self.asked).clone()
        }

        fn note(&self, entry: String) {
            lock(&self.asked).push(entry);
        }
    }

    impl Probe for RecordingProbe {
        fn env(&self) -> &Env {
            &self.env
        }

        fn os(&self) -> Os {
            Os::Macos
        }

        fn resolve(&self, command: &str) -> Resolution {
            self.note(format!("resolve {command}"));
            match command {
                "claude" => Resolution::Found(Resolved {
                    argv: vec![OsString::from("/bin/claude")],
                    source: PathBuf::from("/bin/claude"),
                }),
                "node" => Resolution::Found(Resolved {
                    argv: vec![OsString::from("/bin/node")],
                    source: PathBuf::from("/bin/node"),
                }),
                _ => Resolution::Missing,
            }
        }

        fn resolve_in(&self, command: &str, folder: &Path) -> Resolution {
            self.note(format!("resolve {command} in {}", folder.display()));
            Resolution::Missing
        }

        fn run(
            &self,
            program: &Resolved,
            args: &[&str],
            _deadline: Duration,
        ) -> Result<RunOutput, RunError> {
            let mut parts: Vec<String> = program
                .argv
                .iter()
                .map(|part| part.to_string_lossy().into_owned())
                .collect();
            parts.extend(args.iter().map(|arg| (*arg).to_string()));
            let line = parts.join(" ");
            self.note(format!("run {line}"));
            match line.as_str() {
                "/bin/claude --version" => Ok(RunOutput {
                    exit: Some(0),
                    stdout: String::from("2.1.318 (Claude Code)\n"),
                    stderr: String::new(),
                }),
                "/bin/claude auth status" => Ok(RunOutput {
                    exit: Some(0),
                    stdout: String::from("{\"loggedIn\": true}"),
                    stderr: String::new(),
                }),
                _ => Err(RunError::Launch(String::from("unexpected command"))),
            }
        }

        fn exists(&self, path: &Path) -> bool {
            self.note(format!("exists {}", path.display()));
            false
        }
    }

    fn opened(config: &str) -> (tempfile::TempDir, Session) {
        let dir = tempfile::tempdir().unwrap();
        let root = paths::canonical_folder(&fs::canonicalize(dir.path()).unwrap()).unwrap();
        let (home, project) = (root.join("home"), root.join("project"));
        fs::create_dir_all(project.join(".oah")).unwrap();
        fs::create_dir_all(&home).unwrap();
        fs::write(project.join(".oah").join("config.toml"), config).unwrap();
        let mut env = Env::new(&root).unwrap();
        for name in [
            "PATH",
            "SystemRoot",
            "windir",
            "ComSpec",
            "PATHEXT",
            "TEMP",
            "TMP",
        ] {
            if let Some(value) = std::env::var_os(name) {
                env.set(name, value);
            }
        }
        env.set(HOME_VAR, &home);
        env.set("GIT_CONFIG_NOSYSTEM", "1");
        env.set("GIT_CEILING_DIRECTORIES", &root);
        let session = Session::new(env);
        let cancel = CancelToken::new();
        project_open(&session.ctx(&cancel, &NoProgress), &project).unwrap();
        (dir, session)
    }

    #[test]
    fn reprobe_held_ignores_the_open_projects_configured_commands() {
        let (_dir, session) = opened(
            "[agents.claude]\ncommand = [\"node\", \"/opt/claude/cli.js\"]\n\
             [agents.codex]\ncommand = [\"./bin/synthetic-codex\"]\n",
        );
        let cancel = CancelToken::new();
        let ctx = session.ctx(&cancel, &NoProgress);

        let initial = RecordingProbe::new(session.env().clone());
        let seed = AgentsProbeReq {
            agents: Some(vec![
                AgentIdDto(AgentId::Claude),
                AgentIdDto(AgentId::Codex),
            ]),
            sign_in: false,
            focus: false,
        };
        agents_probe_with(&ctx, seed, &initial).unwrap();

        let refresh = RecordingProbe::new(session.env().clone());
        reprobe_held(&ctx, &refresh).unwrap();

        let asked = refresh.asked();
        assert!(asked.contains(&String::from("resolve claude")), "{asked:?}");
        assert!(asked.contains(&String::from("resolve codex")), "{asked:?}");
        assert!(
            asked.contains(&String::from("run /bin/claude --version")),
            "{asked:?}"
        );
        assert!(
            asked.contains(&String::from("run /bin/claude auth status")),
            "{asked:?}"
        );
        assert!(
            asked.iter().all(|entry| !entry.contains("node")
                && !entry.contains("/opt/claude/cli.js")
                && !entry.contains("synthetic-codex")),
            "held-agent refresh must never ask for configured project commands: {asked:?}"
        );

        let listed = agents_list(&ctx, Empty {}).unwrap();
        assert_eq!(listed.len(), 2);
        assert_eq!(listed[0].id.0, AgentId::Claude);
        assert_eq!(listed[0].status, Status::Ok);
        assert_eq!(listed[0].source.as_deref(), Some("/bin/claude"));
        assert_eq!(listed[1].id.0, AgentId::Codex);
        assert_eq!(listed[1].status, Status::Missing);
    }
}
