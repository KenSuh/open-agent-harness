// 에이전트 작업: 세션에 에이전트마다 마지막 상태를 두고, 고른 에이전트를 나란히 확인하며, 포커스 확인의 로그인은 에이전트마다 60초에 한 번만 한다.
//! `agents_list` (read) and `start_agents_probe` (probe) of CONTRACTS C15. Each agent is probed on its own thread
//! with its own deadlines, so one agent's failure is that agent's status only. The session keeps the newest
//! status of every probed agent and when its sign-in was last checked; that 60-second limit lives here, not in
//! the UI (AC6).

use super::{Ctx, lock};
use crate::agents::{
    AgentError, Check, Detection, Failure, Os, Probe, SignInCheck, Status, SystemProbe, detect,
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
                current.check.opencode = None;
                current.sign_in_stamp = None;
            }
        }
        if (newer_detection || compatible) && next.sign_in_stamp > current.sign_in_stamp {
            current.check.sign_in = next.check.sign_in;
            current.check.opencode = next.check.opencode;
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
        "{};sign_in={};focus={};opencode_details={}",
        agents.join(","),
        req.sign_in,
        req.focus,
        !req.focus && req.opencode_details == Some(true)
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
        opencode_details: None,
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
/// Kept `pub(super)` so only the explicit terminal `oah doctor --use-project-commands` entry can supply
/// project-configured commands; default `doctor` and every automatic app, focus and refresh probe enter
/// through [`agents_probe_with`] with an empty override map.
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
    let mut check = Check {
        detection: detect(id, probe, command),
        sign_in: SignInCheck::NotChecked,
        opencode: None,
    };
    let now = Instant::now();
    let read_details = if id == AgentId::Opencode {
        !req.focus && req.opencode_details == Some(true) && check.supports_opencode_details()
    } else {
        req.sign_in
            && check.detection.status == Status::Ok
            && (!req.focus || run.reserve_focus(id, now))
    };
    if read_details {
        check.read_details(probe, command);
    }
    (check, read_details.then_some(now))
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
        opencode: None,
    }
}
