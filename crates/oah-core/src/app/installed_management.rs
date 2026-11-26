use super::installed::{InstalledLocation, resolve, scan};
use super::{Ctx, ProjectSelection, lock, review::license_allow};
use crate::config::{AgentId, ProjectConfig};
use crate::dto::{
    AgentIdDto, InstalledSkillManagementDto, InstalledSkillTarget, LinkedSkillSourceDto,
    RegisterSkillSourceReq, RemoveInstalledSkillReq, RemovedSkillDto, SkillScopeDto, display_path,
};
use crate::error::{Error, ProjectSkillError};
use crate::fsx::skill_publish::{Engine, LocalSkill, PublishError, ReviewBinding, SkillLeaf};
use crate::fsx::{FileLock, Fingerprint, LockOptions, open_regular, write_atomic};
use crate::paths::user_state_dir;
use crate::review::delivery::{GitHubSkillSource, ReviewApproval, authorize};
use crate::review::{Store, record::ReviewStatus};
use std::io::{self, Read};
use std::path::{Path, PathBuf};

struct SelectedSkill {
    row: InstalledLocation,
    folder: LocalSkill,
    source_file: PathBuf,
    store: Store,
    view: InstalledSkillManagementDto,
}

fn review_source(store: &Store, id: &str, managed: bool) -> Result<LinkedSkillSourceDto, Error> {
    let review = store.review(id)?.ok_or(PublishError::InvalidReceipt)?;
    if review.status != ReviewStatus::Reviewed {
        return Err(PublishError::InvalidReceipt.into());
    }
    let source = GitHubSkillSource::from_record(&review)?;
    Ok(LinkedSkillSourceDto {
        repo: source.repo().to_owned(),
        path: source.path().to_owned(),
        commit: review.source.commit.ok_or(PublishError::InvalidReceipt)?,
        updates_in_oah: managed,
    })
}

fn linked_id(path: &Path) -> Option<String> {
    let (file, _) = open_regular(path).ok()?;
    let mut id = String::new();
    file.take(72).read_to_string(&mut id).ok()?;
    (id.len() == 71 && id.starts_with("sha256:")).then_some(id)
}

fn select(
    ctx: &Ctx<'_>,
    project: Option<&ProjectSelection>,
    target: &InstalledSkillTarget,
) -> Result<SelectedSkill, Error> {
    if target.project.as_deref() != project.map(|held| held.opened.id.as_str()) {
        return Err(ProjectSkillError::ProjectChanged.into());
    }
    let rows = scan(ctx, project)?;
    let mut matching = rows
        .iter()
        .filter(|row| row.dto.location == target.location);
    let requested = matching.next().ok_or(PublishError::StaleRevision)?;
    if matching.next().is_some() {
        return Err(PublishError::UnsafePath.into());
    }
    let absolute = resolve(&requested.folder)?.ok_or(PublishError::StaleRevision)?;
    let mut group = Vec::new();
    for row in rows {
        if resolve(&row.folder)?.as_ref() == Some(&absolute) {
            group.push(row);
        }
    }
    let agents: Vec<_> = AgentId::ALL
        .into_iter()
        .filter(|agent| {
            group.iter().any(|row| {
                row.dto.engine.0 == *agent
                    || row
                        .dto
                        .compatible_agents
                        .iter()
                        .any(|other| other.0 == *agent)
            })
        })
        .map(AgentIdDto)
        .collect();

    let (row, folder) = group
        .into_iter()
        .filter(|row| {
            row.dto.scope != SkillScopeDto::Project
                || project.is_some_and(|held| held.skill_root.is_ok())
        })
        .find_map(|row| {
            LocalSkill::open(&row.folder)
                .ok()
                .map(|folder| (row, folder))
        })
        .ok_or(PublishError::UnsafePath)?;
    let identity = folder.stamp()?;
    let state = user_state_dir(ctx.env)?;
    let source_file = state.join("installed-skill-sources-v1").join(&identity);
    let store = Store::new(&state);
    let (managed, review_id) = match &row.dto.managed {
        Some(receipt) => (true, Some(receipt.review_id.clone())),
        None => (false, linked_id(&source_file)),
    };
    let source = review_id
        .as_deref()
        .and_then(|id| review_source(&store, id, managed).ok());
    let generation = project.map(|held| held.stamp.selection);
    let state = serde_json::to_vec(&(identity, generation, &agents, review_id))
        .map_err(|_| PublishError::InvalidReceipt)?;
    let revision = Fingerprint::of(&state).to_hex();
    let location = display_path(ctx.env, folder.path());
    Ok(SelectedSkill {
        row,
        folder,
        source_file,
        store,
        view: InstalledSkillManagementDto {
            revision,
            location,
            agents,
            source,
        },
    })
}

pub fn inspect_installed_skill(
    ctx: &Ctx<'_>,
    req: &InstalledSkillTarget,
) -> Result<InstalledSkillManagementDto, Error> {
    let slot = lock(&ctx.session.project);
    Ok(select(ctx, slot.held.as_ref(), req)?.view)
}

fn check_revision(skill: &SelectedSkill, expected: &str) -> Result<(), PublishError> {
    if skill.view.revision != expected {
        return Err(PublishError::StaleRevision);
    }
    Ok(())
}

pub fn remove_installed_skill(
    ctx: &Ctx<'_>,
    req: &RemoveInstalledSkillReq,
) -> Result<RemovedSkillDto, Error> {
    let slot = lock(&ctx.session.project);
    let skill = select(ctx, slot.held.as_ref(), &req.target)?;
    check_revision(&skill, &req.revision)?;
    if skill.view.agents.len() > 1 && !req.confirm_shared {
        return Err(PublishError::SharedConfirmationRequired.into());
    }
    ctx.cancel.check()?;
    let archive = skill.folder.archive()?;
    Ok(RemovedSkillDto {
        archive: display_path(ctx.env, &archive),
    })
}

fn try_adopt(
    project: Option<&ProjectSelection>,
    skill: &SelectedSkill,
    req: &RegisterSkillSourceReq,
) -> Result<bool, Error> {
    let Some(project) = project.filter(|_| skill.row.dto.scope == SkillScopeDto::Project) else {
        return Ok(false);
    };
    let path = skill.folder.path();
    let engine = match path
        .parent()
        .and_then(|p| p.parent())
        .and_then(|p| p.file_name())
        .and_then(std::ffi::OsStr::to_str)
    {
        Some(".claude") => Engine::Claude,
        Some(".agents") => Engine::Codex,
        _ => return Ok(false),
    };
    let config = ProjectConfig::read(&project.opened.project.config_file())?;
    let allow = license_allow(&config.effective().skills.license_allow);
    let payload = authorize(
        &skill.store,
        &allow,
        super::skill_apply::agent_id(engine),
        ReviewApproval {
            review_id: &req.review_id,
            acknowledge_warnings: req.acknowledge_warnings,
        },
    )?;
    if skill.row.dto.name != payload.name() {
        return Ok(false);
    }
    let binding = ReviewBinding {
        repo: payload.source().repo().to_owned(),
        path: payload.source().path().to_owned(),
        review_id: payload.review().id.clone(),
        snapshot_id: payload.snapshot().id.clone(),
    };
    let files = payload
        .files()
        .map(|(path, bytes)| (path.to_owned(), bytes.to_vec()))
        .collect();
    let result = project
        .skill_root
        .as_ref()
        .map_err(Clone::clone)?
        .adopt_existing(
            engine,
            &SkillLeaf::new(payload.name())?,
            &skill.folder,
            &binding,
            &files,
        );
    match result {
        Ok(()) => Ok(true),
        Err(PublishError::Changed | PublishError::Occupied | PublishError::UnsafePath) => Ok(false),
        Err(PublishError::Io(err)) if err.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(err) => Err(err.into()),
    }
}

pub fn register_skill_source(
    ctx: &Ctx<'_>,
    req: &RegisterSkillSourceReq,
) -> Result<LinkedSkillSourceDto, Error> {
    let slot = lock(&ctx.session.project);
    let project = slot.held.as_ref();
    let first = select(ctx, project, &req.target)?;
    check_revision(&first, &req.revision)?;
    let parent = first.source_file.parent().ok_or(PublishError::UnsafePath)?;
    std::fs::create_dir_all(parent).map_err(PublishError::from)?;
    let _source_lock = FileLock::acquire(&first.source_file, LockOptions::default())?;
    let skill = select(ctx, project, &req.target)?;
    check_revision(&skill, &req.revision)?;
    if skill
        .view
        .source
        .as_ref()
        .is_some_and(|source| source.updates_in_oah)
    {
        return Err(PublishError::Occupied.into());
    }
    let mut source = review_source(&skill.store, &req.review_id, false)?;
    ctx.cancel.check()?;
    source.updates_in_oah = try_adopt(project, &skill, req)?;
    if !source.updates_in_oah {
        skill.folder.stamp()?;
        write_atomic(&skill.source_file, req.review_id.as_bytes())?;
    }
    Ok(source)
}
