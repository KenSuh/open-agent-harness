// 프로젝트 열기: 폴더를 읽기만 하여 Git 작업 트리나 일반 폴더로 확인하고 설정 파일 상태를 함께 보인다.
//! Opening a folder as a project, read-only (M1-S1).
//!
//! [`open`] locates the project with `paths::locate_project` and reads `.oah/config.toml` with
//! `config::ProjectConfig::read`. Neither writes: no byte changes in the folder, its Git folder or its state
//! folder. An unreadable or invalid config does not stop the project from opening; it is shown as `invalid`.

use crate::config::{ConfigError, Format, Notice, ProjectConfig};
use crate::dto::{ConfigStateDto, NoticeDto, ProjectDto, ProjectKindDto, display_path};
use crate::error::{Coded, Error, codes};
use crate::paths::{self, Env, Project, ProjectKind};
use std::path::{Path, PathBuf};

/// An opened project.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenProject {
    pub project: Project,
    /// `paths::folder_id` of the canonical root.
    pub id: String,
    pub config: ConfigState,
    /// The config's non-fatal notices; empty when it is invalid.
    pub notices: Vec<Notice>,
}

/// `.oah/config.toml` as read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigState {
    Missing,
    Legacy,
    Current,
    Invalid(ConfigError),
}

/// Opens `folder` (resolved against the invocation folder) read-only.
///
/// A path that exists but is not a folder is `OAH_PROJECT_NOT_FOLDER`; a missing one is `OAH_PROJECT_MISSING`;
/// Git failures are `paths` errors.
pub fn open(env: &Env, folder: &Path) -> Result<OpenProject, Error> {
    let absolute = if folder.is_absolute() {
        folder.to_path_buf()
    } else {
        env.cwd().join(folder)
    };
    if let Ok(metadata) = std::fs::metadata(&absolute)
        && !metadata.is_dir()
    {
        return Err(ProjectError::NotFolder { path: absolute }.into());
    }
    let project = paths::locate_project(env, &absolute)?;
    let id = paths::folder_id(&paths::canonical_folder(&project.root)?);
    let (config, notices) = match ProjectConfig::read(&project.config_file()) {
        Ok(config) => {
            let state = match config.format() {
                Format::Missing => ConfigState::Missing,
                Format::Legacy => ConfigState::Legacy,
                Format::Current => ConfigState::Current,
            };
            (state, config.notices().to_vec())
        }
        Err(error) => (ConfigState::Invalid(error), Vec::new()),
    };
    Ok(OpenProject {
        project,
        id,
        config,
        notices,
    })
}

impl OpenProject {
    /// The project as the UI shows it, with paths written with `~`.
    pub fn to_dto(&self, env: &Env) -> ProjectDto {
        ProjectDto {
            id: self.id.clone(),
            root: display_path(env, &self.project.root),
            kind: match self.project.kind {
                ProjectKind::Git { .. } => ProjectKindDto::Git,
                ProjectKind::Folder { .. } => ProjectKindDto::Folder,
            },
            config: match &self.config {
                ConfigState::Missing => ConfigStateDto::Missing,
                ConfigState::Legacy => ConfigStateDto::Legacy,
                ConfigState::Current => ConfigStateDto::Current,
                ConfigState::Invalid(error) => ConfigStateDto::Invalid {
                    error: Error::from(error.clone()).to_dto(env),
                },
            },
            notices: self
                .notices
                .iter()
                .map(|notice| NoticeDto::from_config(env, notice))
                .collect(),
        }
    }
}

/// A project failure that is not another module's error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProjectError {
    /// The chosen path exists and is not a folder.
    NotFolder { path: PathBuf },
}

impl Coded for ProjectError {
    fn code(&self) -> &'static str {
        match self {
            ProjectError::NotFolder { .. } => codes::PROJECT_NOT_FOLDER,
        }
    }

    fn message(&self) -> String {
        match self {
            ProjectError::NotFolder { path } => format!("{} is not a folder", path.display()),
        }
    }
}
