// 진단 작업: 일곱 에이전트를 로그인까지 확인해 앱과 같은 상태를 돌려주고, 프로젝트 단계가 쓰는 에이전트가 모두 쓸 수 있을 때만 0 으로 끝낸다.
//! `oah doctor [--json]` (CONTRACTS C15, AC8): the probe of `start_agents_probe` with sign-in, so the CLI and the
//! app show the same statuses, and the exit code. v0.4.0 counted Claude Code and Codex as required because the
//! default stages use them (v0.4:src/oah/doctor.py:159-160); here the required agents are the ones the project's
//! effective `[stages]` assign.

use super::Ctx;
use super::agents::agents_probe_with;
use crate::agents::{Probe, Status, SystemProbe};
use crate::config::{AgentId, BUILTIN_SECTIONS, ProjectConfig, default_agent};
use crate::dto::{AgentStatusDto, AgentsProbeReq};
use crate::error::Error;

/// What `oah doctor` prints and how it ends.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Doctor {
    /// Every agent, in display order.
    pub agents: Vec<AgentStatusDto>,
    /// `0` when every agent the stages use is usable, else `1`.
    pub exit: i32,
}

/// `oah doctor` on this machine.
pub fn doctor(ctx: &Ctx<'_>) -> Result<Doctor, Error> {
    doctor_with(ctx, &SystemProbe::new(ctx.env))
}

/// `oah doctor` through `probe`.
pub fn doctor_with(ctx: &Ctx<'_>, probe: &dyn Probe) -> Result<Doctor, Error> {
    let req = AgentsProbeReq {
        agents: None,
        sign_in: true,
        focus: false,
    };
    let agents = agents_probe_with(ctx, req, probe)?;
    let exit = doctor_exit(&agents, &stage_agents(ctx));
    Ok(Doctor { agents, exit })
}

/// `0` when every agent in `required` is usable, else `1`. Usable is `ok`, or `sign-in-unknown` for an agent
/// whose sign-in cannot be read yet; missing, too old, signed out, a refused shim or an error is not.
pub fn doctor_exit(agents: &[AgentStatusDto], required: &[AgentId]) -> i32 {
    let usable = |id: &AgentId| {
        let status = agents
            .iter()
            .find(|agent| agent.id.0 == *id)
            .map(|agent| agent.status);
        matches!(status, Some(Status::Ok | Status::SignInUnknown))
    };
    i32::from(!required.iter().all(usable))
}

/// The agents the open project's effective `[stages]` assign, each once; the built-in defaults without a
/// project or a readable config.
fn stage_agents(ctx: &Ctx<'_>) -> Vec<AgentId> {
    let config = ctx
        .session
        .project()
        .and_then(|open| ProjectConfig::read(&open.project.config_file()).ok());
    let assigned: Vec<AgentId> = match &config {
        Some(config) => config
            .effective()
            .stages
            .iter()
            .map(|assignment| assignment.agent)
            .collect(),
        None => BUILTIN_SECTIONS
            .into_iter()
            .filter_map(default_agent)
            .collect(),
    };
    AgentId::ALL
        .into_iter()
        .filter(|id| assigned.contains(id))
        .collect()
}
