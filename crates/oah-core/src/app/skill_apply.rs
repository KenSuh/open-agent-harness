use super::{Ctx, lock, review::license_allow};
use crate::config::{AgentId, ProjectConfig};
use crate::dto::{ApplyProjectSkillDto, ApplyProjectSkillReq, ApplyProjectSkillStatus};
use crate::error::{Error, ProjectSkillError};
use crate::fsx::skill_publish::{Engine, PublishError, PublishStatus, ReviewBinding, SkillLeaf};
use crate::paths::{self, Env};
use crate::review::delivery::{GitHubSkillSource, ReviewApproval, authorize};
use crate::review::{Store, record::ReviewStatus};
use std::io;
use std::path::Path;

pub(super) fn agent_id(engine: Engine) -> AgentId {
    match engine {
        Engine::Claude => AgentId::Claude,
        Engine::Codex => AgentId::Codex,
    }
}

pub(super) fn refuse_home(env: &Env, root: &Path) -> Result<(), ProjectSkillError> {
    let home = paths::user_home(env).map_err(|_| ProjectSkillError::HomeUnverified)?;
    if same_directory(root, &home).map_err(|_| ProjectSkillError::HomeUnverified)? {
        return Err(ProjectSkillError::HomeScope);
    }
    Ok(())
}

#[cfg(unix)]
fn same_directory(root: &Path, home: &Path) -> io::Result<bool> {
    use std::os::unix::fs::MetadataExt;

    let (root, home) = (std::fs::metadata(root)?, std::fs::metadata(home)?);
    Ok(root.dev() == home.dev() && root.ino() == home.ino())
}

#[cfg(windows)]
fn same_directory(root: &Path, home: &Path) -> io::Result<bool> {
    use winapi_util::{Handle, file::information};

    let (root, home) = (Handle::from_path_any(root)?, Handle::from_path_any(home)?);
    let (root, home) = (information(root.as_file())?, information(home.as_file())?);
    Ok(root.volume_serial_number() == home.volume_serial_number()
        && root.file_index() == home.file_index())
}

#[cfg(not(any(unix, windows)))]
fn same_directory(root: &Path, home: &Path) -> io::Result<bool> {
    Ok(std::fs::canonicalize(root)? == std::fs::canonicalize(home)?)
}

pub(super) fn stored_binding(
    store: &Store,
    review_id: &str,
    name: &str,
) -> Result<ReviewBinding, Error> {
    let review = store
        .review(review_id)?
        .ok_or(PublishError::InvalidReceipt)?;
    if review.status != ReviewStatus::Reviewed || review.frontmatter.name.as_deref() != Some(name) {
        return Err(PublishError::InvalidReceipt.into());
    }
    let source = GitHubSkillSource::from_record(&review)?;
    Ok(ReviewBinding {
        repo: source.repo().to_owned(),
        path: source.path().to_owned(),
        review_id: review.id,
        snapshot_id: review.source.snapshot,
    })
}

/// Applies one confirmed, locally stored review to the currently selected project.
pub fn apply_project_skill(
    ctx: &Ctx<'_>,
    req: &ApplyProjectSkillReq,
) -> Result<ApplyProjectSkillDto, Error> {
    ctx.cancel.check()?;
    let (selection, project, root) = {
        let slot = lock(&ctx.session.project);
        let held = slot.held.as_ref().ok_or(ProjectSkillError::NoProject)?;
        if held.opened.id != req.project {
            return Err(ProjectSkillError::ProjectChanged.into());
        }
        (
            held.stamp.selection,
            held.opened.project.clone(),
            held.skill_root.clone()?,
        )
    };

    let config = ProjectConfig::read(&project.config_file())?;
    let allow = license_allow(&config.effective().skills.license_allow);
    let store = Store::new(&paths::user_state_dir(ctx.env)?);
    let payload = authorize(
        &store,
        &allow,
        agent_id(req.engine),
        ReviewApproval {
            review_id: &req.review_id,
            acknowledge_warnings: req.acknowledge_warnings,
        },
    )?;
    let leaf = SkillLeaf::new(payload.name())?;
    let binding = ReviewBinding {
        repo: payload.source().repo().to_owned(),
        path: payload.source().path().to_owned(),
        review_id: payload.review().id.clone(),
        snapshot_id: payload.snapshot().id.clone(),
    };

    let previous = req
        .expected_review_id
        .as_deref()
        .map(|id| stored_binding(&store, id, payload.name()))
        .transpose()?;
    if previous
        .as_ref()
        .is_some_and(|previous| previous.repo != binding.repo || previous.path != binding.path)
    {
        return Err(PublishError::SourceMismatch.into());
    }
    let files = payload
        .files()
        .map(|(path, bytes)| (path.to_owned(), bytes.to_vec()))
        .collect();

    let slot = lock(&ctx.session.project);
    if !slot.held.as_ref().is_some_and(|held| {
        held.stamp.selection == selection
            && held.opened.id == req.project
            && held.skill_root.is_ok()
    }) {
        return Err(ProjectSkillError::ProjectChanged.into());
    }
    ctx.cancel.check()?;
    refuse_home(ctx.env, &project.root)?;
    if let Some(previous) = previous {
        match root.installed(req.engine, &leaf) {
            Ok(Some(installed))
                if installed.binding.review_id == previous.review_id
                    && installed.binding != previous =>
            {
                return Err(PublishError::InvalidReceipt.into());
            }
            // On recovery the old tree may already be staged. The writer verifies it and the original intent.
            Ok(_) | Err(PublishError::Recovery { .. }) => {}
            Err(error) => return Err(error.into()),
        }
    }
    let status = root.apply(
        req.engine,
        &leaf,
        &binding,
        &files,
        req.expected_review_id.as_deref(),
    )?;
    let (status, review_id) = match status {
        PublishStatus::Installed => (ApplyProjectSkillStatus::Installed, binding.review_id),
        PublishStatus::Updated => (ApplyProjectSkillStatus::Updated, binding.review_id),
        PublishStatus::Unchanged => {
            let installed = root
                .installed(req.engine, &leaf)?
                .ok_or(PublishError::Changed)?;
            (
                ApplyProjectSkillStatus::Unchanged,
                installed.binding.review_id,
            )
        }
    };
    drop(slot);
    Ok(ApplyProjectSkillDto { status, review_id })
}
