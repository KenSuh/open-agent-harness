use super::Ctx;
use crate::config::AgentId;
use crate::dto::{AgentIdDto, Empty, InstalledSkillDto, SkillScopeDto, display_path};
use crate::error::Error;
use crate::fsx::links::{Entry, inspect};
use crate::fsx::{FsxError, FsxErrorKind};
use crate::paths::user_home;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

// The full CLI's install targets (src/oah/toolkit/install.py), at each scope.
const ENGINE_FOLDERS: [(AgentId, &str); 2] =
    [(AgentId::Claude, ".claude"), (AgentId::Codex, ".agents")];

/// Lists local skill folders for the selected project (if any) and the user, without opening any SKILL.md.
pub fn skills_installed(ctx: &Ctx<'_>, _req: Empty) -> Result<Vec<InstalledSkillDto>, Error> {
    let project = ctx.session.project();
    let home = user_home(ctx.env)?;
    let project_scope = project
        .as_ref()
        .map(|opened| (&opened.project.root, SkillScopeDto::Project));
    let mut skills = Vec::new();
    for (base, scope) in project_scope
        .into_iter()
        .chain([(&home, SkillScopeDto::User)])
    {
        for (engine, engine_dir) in ENGINE_FOLDERS {
            for (name, folder) in skill_folders(base, engine_dir)? {
                skills.push(InstalledSkillDto {
                    name: name.to_string_lossy().into_owned(),
                    engine: AgentIdDto(engine),
                    scope,
                    location: display_path(ctx.env, &folder),
                });
            }
        }
    }
    Ok(skills)
}

fn skill_folders(base: &Path, engine_dir: &str) -> Result<Vec<(OsString, PathBuf)>, FsxError> {
    let engine_dir = base.join(engine_dir);
    let root = engine_dir.join("skills");
    for dir in [&engine_dir, &root] {
        match inspect(dir)? {
            Entry::Missing => return Ok(Vec::new()),
            Entry::Directory => {}
            _ => {
                return Err(FsxError::new(
                    FsxErrorKind::Io,
                    dir,
                    "expected a local skills directory",
                ));
            }
        }
    }

    let read_error = |error| {
        FsxError::io(
            FsxErrorKind::Io,
            &root,
            "cannot list installed skills",
            error,
        )
    };
    let mut folders = Vec::new();
    for entry in fs::read_dir(&root).map_err(read_error)? {
        let entry = entry.map_err(read_error)?;
        let name = entry.file_name();
        if name.as_encoded_bytes().starts_with(b".") {
            continue;
        }
        let path = entry.path();
        if matches!(inspect(&path)?, Entry::Directory)
            && matches!(inspect(&path.join("SKILL.md"))?, Entry::File)
        {
            folders.push((name, path));
        }
    }
    folders.sort_unstable();
    Ok(folders)
}
