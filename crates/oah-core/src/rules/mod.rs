// 규칙 모듈: AGENTS.md 관리 블록·1.0 본문·얇은 파일을 순수 함수로 계획한다. 파일 쓰기는 이 모듈 밖의 다음 단계다.
//! Managed text in project files (design §3.4, CONTRACTS C12). Everything here is pure planning over bytes the
//! caller read; [`sync`] is the writer: it plans every file from the effective config, writes through the apply engine
//! and reads each write back.
//!
//! * [`plan`] is v0.4.0's block algorithm, byte for byte, with the body as an input; [`find_block`] and
//!   [`render_block`] are its two pinned helpers.
//! * [`check::drift`] reports drift without writing.
//! * [`thin`] plans `CLAUDE.md`'s and `GEMINI.md`'s import line and `.cursor/rules/oah.mdc` by the C12 table.
//! * [`input`] reads project files without following links, for callers that start from a folder.
//! * [`sync`] reads a project snapshot, plans the block and thin files from the effective config, reports drift and
//!   writes atomically with a read-back (`OAH_RULES_VERIFY`).

pub mod body;
pub mod check;
mod error;
pub mod input;
mod markers;
mod plan;
pub mod sync;
mod text;
pub mod thin;

pub use check::{Drift, check, drift};
pub use error::{RulesError, RulesErrorKind, codes};
pub use input::{AGENTS_MD, CLAUDE_MD, LOCAL_RULES, ProjectFile, RulesFiles};
pub use markers::{BEGIN, END, VERSION, find_block};
pub use plan::{
    AGENTS_BUDGET, BLOCK_LIMIT, BlockBodies, LANGS, Lang, RulesInput, RulesPlan, plan, render_block,
};
pub use sync::{
    DriftLine, FileWrite, RulesSnapshot, SyncInput, SyncPlan, SyncResult, apply_sync,
    apply_sync_with, check_sync, plan_sync, target_path, write_file,
};
pub use text::{Eol, split_lines};
pub use thin::{ThinFilePlan, ThinFileTrigger, ThinInput, plan_thin};
