// 작업 DTO: 작업 채널 이벤트·실행 중 작업 목록의 모양을 내보내고 취소 요청 {id} 를 받는다.
use serde::{Deserialize, Serialize};

pub use crate::tasks::{Progress, TaskEvent, TaskId, TaskSnapshot as TaskSnapshotDto, TaskState};

/// The request of `task_cancel`: `{ "id": "task-1" }`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskCancelReq {
    pub id: String,
}
