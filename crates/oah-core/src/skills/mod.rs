// 스킬 폴더 사실: 모은 스킬 파일들의 공개 computedHash 를 계산하고, 설치된 에이전트별 링크 계획을 세운다.
//! Facts about a skill folder. [`hash`] computes the public `computedHash` of a skill's files; [`link_plan`] plans
//! which installed agents read a store directly, need a link or a switch, or stay visible.

pub mod hash;
mod link_plan;

pub use hash::computed_hash;
pub use link_plan::{
    AgentView, LinkAction, LinkPlanRequest, Scope, SkillDirs, SwitchOp, link_plan,
};
