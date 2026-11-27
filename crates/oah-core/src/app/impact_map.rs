use super::{Ctx, lock};
use crate::Error;
use crate::dto::{
    ImpactGraphState, ImpactMapAction, ImpactMapRunDto, ImpactMapRunReq, WorkflowProjectReq,
};
use crate::paths::ProjectKind;
use crate::project_tools::runner::{
    GraphState, ImpactAction, ImpactRequest, Performed, ProjectTools,
};
use crate::tasks::TaskKey;
use crate::workflow::WorkflowError;
use std::path::PathBuf;

pub fn impact_map_key(
    ctx: &Ctx<'_>,
    req: &WorkflowProjectReq<ImpactMapRunReq>,
) -> Result<TaskKey, Error> {
    let open = ctx.session.project().ok_or(WorkflowError::NoProject)?;
    if open.id != req.project_id {
        return Err(WorkflowError::ProjectChanged.into());
    }
    Ok(TaskKey::exclusive("impact-map", req.project_id.clone()))
}

pub fn run_impact_map(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq<ImpactMapRunReq>,
    runner: PathBuf,
) -> Result<ImpactMapRunDto, Error> {
    ctx.cancel.check()?;
    let (root, pin) = {
        let selected = lock(&ctx.session.project);
        let held = selected.held.as_ref().ok_or(WorkflowError::NoProject)?;
        if held.opened.id != req.project_id {
            return Err(WorkflowError::ProjectChanged.into());
        }
        if !matches!(held.opened.project.kind, ProjectKind::Git { .. }) {
            return Ok(ImpactMapRunDto {
                project_id: req.project_id,
                action: req.input.action,
                changed: false,
                graph: None,
                refusal: Some("OAH_PROJECT_TOOLS_GIT_LAYOUT".to_owned()),
            });
        }
        (held.opened.project.root.clone(), held.skill_root.clone()?)
    };
    pin.verify_selection()?;
    let action = match req.input.action {
        ImpactMapAction::Status => ImpactAction::Status,
        ImpactMapAction::Refresh => ImpactAction::Refresh,
    };
    let runner = ProjectTools::new(runner);
    let result = runner.impact(
        ctx,
        &ImpactRequest {
            action,
            root: &root,
            git: None,
        },
    )?;
    Ok(ImpactMapRunDto {
        project_id: req.project_id,
        action: req.input.action,
        changed: matches!(result.performed, Performed::Built | Performed::Refreshed),
        graph: result.graph.map(|graph| match graph.state {
            GraphState::Absent => ImpactGraphState::Absent,
            GraphState::Available => ImpactGraphState::Available,
            GraphState::Stale => ImpactGraphState::Stale,
            GraphState::Unproven => ImpactGraphState::Unproven,
        }),
        refusal: result.refusal,
    })
}
