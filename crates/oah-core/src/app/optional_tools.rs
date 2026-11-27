use super::{Ctx, lock};
use crate::Error;
use crate::config::{ToolsBinding, UserSettings, plan_tools_save};
use crate::dto::{JevInput, JevReply, WikiIndexInput, WikiIndexReply, WorkflowProjectReq};
use crate::project_tools::runner::ProjectTools;
use crate::tasks::TaskKey;
use crate::workflow::WorkflowError;
use std::path::PathBuf;

fn root(ctx: &Ctx<'_>, project_id: &str) -> Result<PathBuf, Error> {
    let (folder, pin) = {
        let selected = lock(&ctx.session.project);
        let held = selected.held.as_ref().ok_or(WorkflowError::NoProject)?;
        if held.opened.id != project_id {
            return Err(WorkflowError::ProjectChanged.into());
        }
        (held.opened.project.root.clone(), held.skill_root.clone()?)
    };
    pin.verify_selection()?;
    Ok(folder)
}

pub fn project_jev_action(
    ctx: &Ctx<'_>,
    request: WorkflowProjectReq<JevInput>,
    runner: PathBuf,
) -> Result<JevReply, Error> {
    ctx.cancel.check()?;
    let folder = root(ctx, &request.project_id)?;
    let runner = ProjectTools::new(runner);
    let result = runner.jev(ctx, &folder, &request.input)?;
    Ok(JevReply {
        project_id: request.project_id,
        action: request.input.action(),
        result,
    })
}

pub fn wiki_index_key(
    ctx: &Ctx<'_>,
    request: &WorkflowProjectReq<WikiIndexInput>,
) -> Result<TaskKey, Error> {
    let open = ctx.session.project().ok_or(WorkflowError::NoProject)?;
    if open.id != request.project_id {
        return Err(WorkflowError::ProjectChanged.into());
    }
    Ok(TaskKey::exclusive("wiki-index", request.project_id.clone()))
}

pub fn project_wiki_index(
    ctx: &Ctx<'_>,
    request: WorkflowProjectReq<WikiIndexInput>,
    runner: PathBuf,
) -> Result<WikiIndexReply, Error> {
    ctx.cancel.check()?;
    let folder = root(ctx, &request.project_id)?;
    let binding = ToolsBinding::new(request.project_id.clone(), folder.clone());
    let plan = plan_tools_save(&binding, &[])?;
    let machine = UserSettings::read(&crate::paths::settings_file(ctx.env)?)?;
    let runner = ProjectTools::new(runner);
    let result = runner.wiki_index(
        ctx,
        &folder,
        plan.tools(),
        &machine.values().wiki_index,
        &request.input,
    )?;
    Ok(WikiIndexReply {
        project_id: request.project_id,
        action: request.input.action(),
        result,
    })
}
