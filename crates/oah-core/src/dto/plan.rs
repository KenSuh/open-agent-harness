// 계획 DTO: 화면에 보일 변경 계획·근거·적용 결과의 모양이며, 경로는 ~ 로 쓴 표시 문자열이다.
use super::NoticeDto;
use crate::error::ErrorDto;
use crate::plan::{Action, ItemStatus, Owner};
use serde::Serialize;

/// A change plan as the UI shows it (CONTRACTS C4).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ChangePlanDto {
    pub id: String,
    pub items: Vec<PlanItemDto>,
    pub warnings: Vec<NoticeDto>,
    pub basis: Vec<BasisDto>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PlanItemDto {
    pub id: String,
    /// A display path.
    pub target: String,
    pub owner: Owner,
    pub action: Action,
    pub summary: String,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BasisDto {
    pub target: String,
    /// SHA-256 in lowercase hex; `null` when the file did not exist.
    pub fingerprint: Option<String>,
}

/// The result of an apply: every accepted item with its class, and why it stopped, if it did.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ApplyReportDto {
    pub items: Vec<ItemResultDto>,
    pub error: Option<ErrorDto>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ItemResultDto {
    pub id: String,
    pub target: String,
    pub status: ItemStatus,
}
