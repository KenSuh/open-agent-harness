// 동기화 등록부 시험: 다섯 단계 id 의 순서, 이 기준에서 등록된 단계는 3 뿐임, 보고는 등록된 단계만 담음, 실제 규칙 단계가 쓰고 다시 읽음을 본다.
use oah_core::app::Ctx;
use oah_core::app::Session;
use oah_core::app::sync::{
    REGISTRY, Row, StepId, StepPlan, StepRow, SyncProject, sync_apply, sync_plan,
};
use oah_core::error::Error;
use oah_core::paths::{self, Env};
use oah_core::plan::{ItemStatus, Outcome, Target};
use oah_core::tasks::{CancelToken, NoProgress};
use std::fs;
use std::path::PathBuf;

fn project() -> (tempfile::TempDir, PathBuf, Session) {
    let dir = tempfile::tempdir().unwrap();
    let base = paths::canonical_folder(&fs::canonicalize(dir.path()).unwrap()).unwrap();
    let root = base.join("project");
    fs::create_dir_all(&root).unwrap();
    let home_var = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    let env = Env::new(&root).unwrap().with(home_var, base.join("home"));
    (dir, root, Session::new(env))
}

#[test]
fn the_five_step_ids_are_in_order_and_only_step_three_is_registered_at_this_base() {
    let numbers: Vec<u8> = StepId::ALL.iter().map(|step| step.number()).collect();
    assert_eq!(numbers, [1, 2, 3, 4, 5]);
    let mut sorted = StepId::ALL;
    sorted.sort();
    assert_eq!(sorted, StepId::ALL, "the enum order is the id order");
    let registered: Vec<StepId> = REGISTRY.iter().map(|row| row.step).collect();
    assert!(
        registered.windows(2).all(|pair| pair[0] < pair[1]),
        "the registry is in id order"
    );
    assert_eq!(registered, [StepId::Rules]);
    assert_eq!(StepId::Rules.as_str(), "rules");
}

fn nothing(_: &Ctx<'_>, _: &SyncProject) -> Result<StepPlan, Error> {
    Ok(StepPlan::default())
}

fn never(
    _: &Ctx<'_>,
    _: &SyncProject,
    _: &Target<'_>,
    _: &[&oah_core::app::sync::SyncItem],
) -> Outcome {
    Outcome::Done(Vec::new())
}

#[test]
fn a_report_lists_registered_steps_only_and_never_an_absent_step() {
    let (_dir, root, session) = project();
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let sync_project = SyncProject::read(&root).unwrap();
    let registry = [StepRow {
        step: StepId::ModelRewrite,
        plan: nothing,
        execute: never,
    }];
    let planned = sync_plan(&ctx, &sync_project, &registry).unwrap();
    assert_eq!(planned.steps, [StepId::ModelRewrite]);
    assert!(planned.items.is_empty());
    let report = sync_apply(&ctx, &sync_project, &planned, &[] as &[&str], &registry).unwrap();
    assert_eq!(report.steps.len(), 1);
    assert_eq!(report.steps[0].step, StepId::ModelRewrite);
    assert!(
        fs::read_dir(&root).unwrap().next().is_none(),
        "nothing was written"
    );
}

#[test]
fn the_real_rules_step_plans_always_items_and_writes_them_through_the_apply_engine() {
    let (_dir, root, session) = project();
    fs::write(root.join("CLAUDE.md"), b"notes\n").unwrap();
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let sync_project = SyncProject::read(&root).unwrap();
    let planned = sync_plan(&ctx, &sync_project, REGISTRY).unwrap();
    assert_eq!(planned.steps, [StepId::Rules]);
    let ids: Vec<&str> = planned.items.iter().map(|item| item.id.as_str()).collect();
    assert_eq!(ids, ["rules:AGENTS.md", "rules:CLAUDE.md"]);
    assert!(
        planned
            .items
            .iter()
            .all(|item| item.row == Row::Always && item.step == StepId::Rules)
    );
    assert!(
        fs::read(root.join("AGENTS.md")).is_err(),
        "planning wrote nothing"
    );

    let report = sync_apply(&ctx, &sync_project, &planned, &ids, REGISTRY).unwrap();
    assert!(report.apply.stop.is_none(), "{:?}", report.apply.stop);
    assert_eq!(report.steps.len(), 1);
    assert!(
        report.steps[0]
            .items
            .iter()
            .all(|item| item.status == ItemStatus::Done)
    );
    assert_eq!(
        fs::read(root.join("CLAUDE.md")).unwrap(),
        b"notes\n@AGENTS.md\n"
    );
    assert!(
        fs::read_to_string(root.join("AGENTS.md"))
            .unwrap()
            .contains("## Team workflow (managed by OAH)")
    );
    let again = sync_plan(&ctx, &SyncProject::read(&root).unwrap(), REGISTRY).unwrap();
    assert!(again.items.is_empty(), "a synced project plans nothing");
}

#[test]
fn a_rules_refusal_plans_nothing_and_becomes_a_warning() {
    let (_dir, root, session) = project();
    fs::write(
        root.join("AGENTS.md"),
        b"<!-- OAH:RULES:BEGIN v1 -->\nno end\n",
    )
    .unwrap();
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let planned = sync_plan(&ctx, &SyncProject::read(&root).unwrap(), REGISTRY).unwrap();
    assert!(planned.items.is_empty());
    assert_eq!(planned.plan.warnings.len(), 1);
    assert_eq!(planned.plan.warnings[0].code, "OAH_RULES_MARKER_BROKEN");
}
