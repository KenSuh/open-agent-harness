// 프로젝트 DTO: 연 폴더의 id·표시 경로·종류·설정 상태·알림을 나타내며, 잘못된 설정은 오류를 함께 싣는다.
use super::NoticeDto;
use crate::error::ErrorDto;
use serde::Serialize;

/// An open project. `id` is `paths::folder_id` of the canonical root; `root` is a display path.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProjectDto {
    pub id: String,
    pub root: String,
    pub kind: ProjectKindDto,
    pub config: ConfigStateDto,
    pub notices: Vec<NoticeDto>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ProjectKindDto {
    Git,
    Folder,
}

/// `.oah/config.toml` as read: `{"state": "missing" | "legacy" | "current"}`, or `{"state": "invalid", "error":
/// {...}}`. An invalid file does not stop the project from opening.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "kebab-case")]
pub enum ConfigStateDto {
    Missing,
    Legacy,
    Current,
    Invalid { error: ErrorDto },
}
