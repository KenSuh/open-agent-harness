// 새로고침 작업: 연 프로젝트를 다시 읽어 세션을 갱신하고 실제 개수를 돌려준다(이 조각에서는 아무것도 쓰지 않는다).
use super::Ctx;
use crate::dto::{Empty, RefreshDto};
use crate::error::Error;
use crate::project;
use crate::tasks::{Progress, TaskKey};

/// The task kind of the full refresh.
pub const REFRESH: &str = "refresh";

const PHASE: &str = "read-project";

/// The full refresh's task key. It is joinable: a second start while one runs joins it.
pub fn refresh_key() -> TaskKey {
    TaskKey::joinable(REFRESH, "")
}

/// The full refresh: re-reads the open project, if any, and replaces the session's copy. It writes nothing in
/// this slice; later slices add steps. Cancellation is checked before each step.
pub fn refresh(ctx: &Ctx<'_>, _req: Empty) -> Result<RefreshDto, Error> {
    ctx.cancel.check()?;
    let Some(current) = ctx.session.project() else {
        ctx.progress.progress(Progress {
            phase: PHASE,
            done: 0,
            total: Some(0),
        });
        return Ok(RefreshDto {
            projects: 0,
            notices: 0,
        });
    };
    ctx.progress.progress(Progress {
        phase: PHASE,
        done: 0,
        total: Some(1),
    });
    let reopened = project::open(ctx.env, &current.project.root)?;
    ctx.cancel.check()?;
    let notices = u32::try_from(reopened.notices.len()).unwrap_or(u32::MAX);
    ctx.session.set_project(reopened);
    ctx.progress.progress(Progress {
        phase: PHASE,
        done: 1,
        total: Some(1),
    });
    Ok(RefreshDto {
        projects: 1,
        notices,
    })
}
