use super::locate;
use crate::app::Ctx;
use crate::config::{
    Backup, BackupPlan, ConfigError, ToolsApproval, ToolsBinding, ToolsPlan, apply_tools_save,
    plan_tools_save,
};
use crate::dto::{
    NoticeDto, ProjectToolsApplyDto, ProjectToolsApplyReq, ProjectToolsPlanDto,
    ProjectToolsPlanReq, ToolsBackupDto, ToolsBackupStateDto, ToolsKeptDto, ToolsKeyChangeDto,
    ToolsUpgradeDto, WorkflowProjectReq,
};
use crate::error::Error;
use crate::fsx::{Fingerprint, LockOptions};
use crate::paths::{Env, folder_id};
use crate::workflow::WorkflowError;
use std::path::Path;

const CONFIG_PATH: &str = ".oah/config.toml";
const BACKUP_PATH: &str = ".oah/config.toml.v0.4.bak";

pub fn project_tools_plan(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq<ProjectToolsPlanReq>,
) -> Result<ProjectToolsPlanDto, Error> {
    let binding = selected(ctx, &req.project_id)?;
    let plan = plan_tools_save(&binding, &req.input.edits)?;
    Ok(plan_dto(ctx.env, &plan)?)
}

pub fn project_tools_apply(
    ctx: &Ctx<'_>,
    req: WorkflowProjectReq<ProjectToolsApplyReq>,
) -> Result<ProjectToolsApplyDto, Error> {
    let binding = selected(ctx, &req.project_id)?;
    apply(ctx, &binding, req.input)
}

pub fn project_tools_plan_in(
    ctx: &Ctx<'_>,
    folder: &Path,
    req: ProjectToolsPlanReq,
) -> Result<ProjectToolsPlanDto, Error> {
    let (root, _) = locate::discover(ctx.env, folder)?;
    let binding = ToolsBinding::new(folder_id(&root), root);
    let plan = plan_tools_save(&binding, &req.edits)?;
    Ok(plan_dto(ctx.env, &plan)?)
}

pub fn project_tools_apply_in(
    ctx: &Ctx<'_>,
    folder: &Path,
    req: ProjectToolsApplyReq,
) -> Result<ProjectToolsApplyDto, Error> {
    let (root, _) = locate::discover(ctx.env, folder)?;
    let binding = ToolsBinding::new(folder_id(&root), root);
    apply(ctx, &binding, req)
}

fn selected(ctx: &Ctx<'_>, id: &str) -> Result<ToolsBinding, Error> {
    let open = ctx.session.project().ok_or(WorkflowError::NoProject)?;
    if open.id != id {
        return Err(WorkflowError::ProjectChanged.into());
    }
    Ok(ToolsBinding::new(open.id, open.project.root))
}

fn apply(
    ctx: &Ctx<'_>,
    binding: &ToolsBinding,
    req: ProjectToolsApplyReq,
) -> Result<ProjectToolsApplyDto, Error> {
    let upgrade = req
        .approve_upgrade
        .as_deref()
        .map(|value| {
            Fingerprint::from_hex(value)
                .ok_or_else(|| ConfigError::invalid("use the upgrade fingerprint from the preview"))
        })
        .transpose()?;
    let approval = ToolsApproval::new(&req.plan_id, upgrade);
    let saved = apply_tools_save(binding, &req.edits, &approval, LockOptions::default())?;
    if let Some(Backup::Written(receipt)) = &saved.backup {
        ctx.session.self_writes().record(receipt);
    }
    if let Some(receipt) = &saved.written {
        ctx.session.self_writes().record(receipt);
    }
    Ok(ProjectToolsApplyDto {
        project_id: binding.project_id().to_owned(),
        plan_id: req.plan_id,
        saved: saved.written.is_some(),
        upgraded: saved.upgraded,
        backup: saved.backup.map(|_| BACKUP_PATH),
    })
}

fn plan_dto(env: &Env, plan: &ToolsPlan) -> Result<ProjectToolsPlanDto, ConfigError> {
    let settings = plan.tools().clone();
    let notices = settings
        .notices
        .iter()
        .map(|notice| NoticeDto::from_config(env, notice))
        .collect();
    let changes = plan
        .changes()
        .iter()
        .map(|change| {
            Ok(ToolsKeyChangeDto {
                key: change.key.clone(),
                before: change.before.as_ref().map(TryInto::try_into).transpose()?,
                after: change.after.as_ref().map(TryInto::try_into).transpose()?,
            })
        })
        .collect::<Result<_, ConfigError>>()?;
    let kept = plan
        .kept()
        .iter()
        .map(|entry| {
            Ok(ToolsKeptDto {
                key: entry.key,
                value: (&entry.value).try_into()?,
                reason: entry.reason.as_str(),
            })
        })
        .collect::<Result<_, ConfigError>>()?;
    let upgrade = plan
        .upgrade_warning()
        .zip(plan.before())
        .map(|(warning, fingerprint)| ToolsUpgradeDto {
            message: warning.message(),
            fingerprint: fingerprint.to_hex(),
        });
    let backup = plan.backup().map(|backup| ToolsBackupDto {
        state: match backup {
            BackupPlan::Write(_) => ToolsBackupStateDto::Write,
            BackupPlan::Reuse(_) => ToolsBackupStateDto::Reuse,
            BackupPlan::Conflict(_) => ToolsBackupStateDto::Conflict,
        },
        path: BACKUP_PATH,
    });
    Ok(ProjectToolsPlanDto {
        project_id: plan.project_id().to_owned(),
        plan_id: plan.id().to_owned(),
        config_path: CONFIG_PATH,
        settings,
        optional: Vec::new(),
        changes,
        kept,
        upgrade,
        backup,
        notices,
    })
}
