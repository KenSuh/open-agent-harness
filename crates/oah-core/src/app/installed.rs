use super::skill_apply::stored_binding;
use super::{Ctx, lock};
use crate::config::AgentId::{self, Antigravity, Claude, Codex, Copilot, Cursor, Gemini, Opencode};
use crate::dto::{
    AgentIdDto, Empty, InstalledSkillDto, ManagedSkillDto, SkillScopeDto, display_path,
};
use crate::error::Error;
use crate::fsx::skill_publish::{Engine, ProjectRoot, SkillLeaf};
use crate::fsx::{FsxError, FsxErrorKind};
use crate::paths::{user_home, user_state_dir};
use crate::review::Store;
use std::ffi::OsString;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

type SkillLocation = (&'static str, AgentId, &'static [AgentId]);

const PROJECT_LOCATIONS: &[SkillLocation] = &[
    (".claude", Claude, &[Opencode, Copilot, Cursor]),
    (
        ".agents",
        Codex,
        &[Opencode, Antigravity, Gemini, Copilot, Cursor],
    ),
    (".opencode", Opencode, &[]),
    (".agent", Antigravity, &[]),
    (".gemini", Gemini, &[]),
    (".github", Copilot, &[]),
    (".cursor", Cursor, &[]),
    (".codex", Codex, &[Cursor]),
];

const DEFAULT_OPENCODE_CONFIG: &str = ".config/opencode";
const USER_LOCATIONS: &[SkillLocation] = &[
    (".claude", Claude, &[Opencode, Cursor]),
    (".agents", Codex, &[Opencode, Gemini, Copilot, Cursor]),
    (".codex", Codex, &[Cursor]),
    (DEFAULT_OPENCODE_CONFIG, Opencode, &[]),
    (".gemini/config", Antigravity, &[]),
    (".gemini/antigravity-cli", Antigravity, &[]),
    (".gemini/antigravity", Antigravity, &[]),
    (".gemini", Gemini, &[Antigravity]),
    (".copilot", Copilot, &[]),
    (".cursor", Cursor, &[]),
];

/// Lists local project and user skills from directory entries and `SKILL.md` metadata.
pub fn skills_installed(ctx: &Ctx<'_>, _req: Empty) -> Result<Vec<InstalledSkillDto>, Error> {
    let selected = lock(&ctx.session.project).held.as_ref().map(|held| {
        (
            held.opened.project.root.clone(),
            held.skill_root.as_ref().ok().cloned(),
        )
    });
    let home = user_home(ctx.env)?;
    let xdg_config_home = ctx
        .env
        .var("XDG_CONFIG_HOME")
        .map(Path::new)
        .filter(|path| path.is_absolute());
    let store = Store::new(&user_state_dir(ctx.env)?);
    let project_scope = if let Some((root, pin)) = &selected
        && let Some(boundary) = resolve(root)?
        && resolve(&home)?.as_ref() != Some(&boundary)
    {
        Some((
            root.as_path(),
            SkillScopeDto::Project,
            Some(boundary),
            pin.as_ref(),
            PROJECT_LOCATIONS,
        ))
    } else {
        None
    };
    let mut skills = Vec::new();
    let user_scope = (
        home.as_path(),
        SkillScopeDto::User,
        None,
        None,
        USER_LOCATIONS,
    );
    for (base, scope, boundary, pin, locations) in project_scope.into_iter().chain([user_scope]) {
        for &(relative, engine, compatible_agents) in locations {
            let managed_target = pin.zip(match relative {
                ".claude" => Some(Engine::Claude),
                ".agents" => Some(Engine::Codex),
                _ => None,
            });
            let (base, relative) = if let Some(xdg) = xdg_config_home
                && relative == DEFAULT_OPENCODE_CONFIG
            {
                (xdg, "opencode")
            } else {
                (base, relative)
            };
            let mut config_dir = base.to_path_buf();
            config_dir.extend(relative.split('/'));
            for (name, folder) in skill_folders(&config_dir, boundary.as_deref())? {
                skills.push(InstalledSkillDto {
                    name: name.to_string_lossy().into_owned(),
                    engine: AgentIdDto(engine),
                    compatible_agents: compatible_agents.iter().copied().map(AgentIdDto).collect(),
                    scope,
                    location: display_path(ctx.env, &folder),
                    managed: managed_target.and_then(|(root, engine)| {
                        managed_skill(&store, root, engine, name.to_str()?)
                    }),
                });
            }
        }
    }
    Ok(skills)
}

fn managed_skill(
    store: &Store,
    root: &ProjectRoot,
    engine: Engine,
    name: &str,
) -> Option<ManagedSkillDto> {
    let leaf = SkillLeaf::new(name).ok()?;
    let installed = root.installed(engine, &leaf).ok()??;
    let binding = installed.binding;
    if stored_binding(store, &binding.review_id, name).ok()? != binding {
        return None;
    }
    Some(ManagedSkillDto {
        review_id: binding.review_id,
        snapshot_id: binding.snapshot_id,
        repo: binding.repo,
        path: binding.path,
    })
}

fn skill_folders(
    config_dir: &Path,
    project: Option<&Path>,
) -> Result<Vec<(OsString, PathBuf)>, FsxError> {
    if skill_directory(config_dir, project)?.is_none() {
        return Ok(Vec::new());
    }
    let root = config_dir.join("skills");
    let Some(resolved_root) = skill_directory(&root, project)? else {
        return Ok(Vec::new());
    };

    let read_error = |error| {
        FsxError::io(
            FsxErrorKind::Io,
            &root,
            "cannot list installed skills",
            error,
        )
    };
    let mut folders = Vec::new();
    for entry in fs::read_dir(resolved_root).map_err(read_error)? {
        let entry = entry.map_err(read_error)?;
        let name = entry.file_name();
        if name.as_encoded_bytes().starts_with(b".") {
            continue;
        }
        let path = entry.path();
        if skill_metadata(&path, project)?.is_some_and(|meta| meta.is_dir())
            && skill_metadata(&path.join("SKILL.md"), project)?.is_some_and(|meta| meta.is_file())
        {
            let visible = root.join(&name);
            folders.push((name, visible));
        }
    }
    folders.sort_unstable();
    Ok(folders)
}

fn skill_directory(path: &Path, project: Option<&Path>) -> Result<Option<PathBuf>, FsxError> {
    let Some(resolved) = resolve(path)? else {
        return Ok(None);
    };
    if project.is_some_and(|root| !resolved.starts_with(root)) {
        return Ok(None);
    }
    if !present(path, fs::symlink_metadata(&resolved))?.is_some_and(|meta| meta.is_dir()) {
        return Err(FsxError::new(
            FsxErrorKind::Io,
            path,
            "expected a local skills directory",
        ));
    }
    Ok(Some(resolved))
}

fn skill_metadata(path: &Path, project: Option<&Path>) -> Result<Option<fs::Metadata>, FsxError> {
    match project {
        Some(root) => match resolve(path)? {
            Some(target) if target.starts_with(root) => present(path, fs::symlink_metadata(target)),
            _ => Ok(None),
        },
        None => present(path, fs::metadata(path)),
    }
}

fn is_link_loop(error: &io::Error) -> bool {
    #[cfg(unix)]
    {
        rustix::io::Errno::from_io_error(error) == Some(rustix::io::Errno::LOOP)
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::ERROR_CANT_RESOLVE_FILENAME;

        error
            .raw_os_error()
            .is_some_and(|code| u32::try_from(code) == Ok(ERROR_CANT_RESOLVE_FILENAME))
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = error;
        false
    }
}

fn resolve(path: &Path) -> Result<Option<PathBuf>, FsxError> {
    present(path, fs::canonicalize(path))
}

fn present<T>(path: &Path, result: io::Result<T>) -> Result<Option<T>, FsxError> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.kind() == io::ErrorKind::NotFound || is_link_loop(&error) => Ok(None),
        Err(error) => Err(FsxError::io(
            FsxErrorKind::Io,
            path,
            "cannot inspect",
            error,
        )),
    }
}
