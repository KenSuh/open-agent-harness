// 동기화 등록부 시험: 다섯 단계 id 의 순서, 이 기준에서 등록된 단계는 3 뿐임, 보고는 등록된 단계만 담음, 실제 규칙 단계가 쓰고 다시 읽음을 본다.
use oah_core::Coded;
use oah_core::app::Ctx;
use oah_core::app::Session;
use oah_core::app::sync::{
    REGISTRY, Row, StepId, StepPlan, StepRow, SyncProject, sync_apply, sync_plan,
};
use oah_core::error::Error;
use oah_core::paths::{self, Env};
use oah_core::plan::{Action, ItemStatus, Outcome, PlanError, Target};
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
    assert_eq!(
        planned
            .plan
            .items
            .iter()
            .map(|item| item.target.as_path())
            .collect::<Vec<_>>(),
        [root.join("AGENTS.md"), root.join("CLAUDE.md")]
    );
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
fn changing_a_rules_source_refuses_the_accepted_plan() {
    for (relative, edit) in [
        (
            ".oah/config.toml",
            b"[stages]\nbuild = \"gemini\"\n".as_slice(),
        ),
        (".oah/rules.local.md", b"Fresh local guidance.\n".as_slice()),
    ] {
        let (_dir, root, session) = project();
        fs::create_dir(root.join(".oah")).unwrap();
        fs::write(
            root.join(".oah/config.toml"),
            b"[stages]\nbuild = \"claude\"\n",
        )
        .unwrap();
        fs::write(
            root.join(".oah/rules.local.md"),
            b"Original local guidance.\n",
        )
        .unwrap();
        fs::write(root.join("CLAUDE.md"), b"user notes\n").unwrap();
        let cancel = CancelToken::new();
        let ctx = session.ctx(&cancel, &NoProgress);
        let sync_project = SyncProject::read(&root).unwrap();
        let plan = sync_plan(&ctx, &sync_project, REGISTRY).unwrap();
        let agents = plan
            .items
            .iter()
            .find(|item| item.item.target == root.join("AGENTS.md"))
            .unwrap();

        let source = root.join(relative);
        fs::write(&source, edit).unwrap();
        let result = sync_apply(&ctx, &sync_project, &plan, &[&agents.id], REGISTRY);
        assert!(
            matches!(result, Err(Error::Plan(PlanError::Stale { target })) if target == source),
            "{relative}"
        );
        assert!(!root.join("AGENTS.md").exists(), "{relative}");
        assert_eq!(fs::read(root.join("CLAUDE.md")).unwrap(), b"user notes\n");
        assert_eq!(fs::read(source).unwrap(), edit);
    }
}

#[test]
fn an_ineligible_cursor_path_leaves_the_other_registry_rules_available() {
    let (_dir, root, session) = project();
    let cursor = root.join(".cursor/rules/oah.mdc");
    fs::create_dir_all(&cursor).unwrap();
    fs::write(root.join("GEMINI.md"), b"Gemini notes\n").unwrap();
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let sync_project = SyncProject::read(&root).unwrap();
    let plan = sync_plan(&ctx, &sync_project, REGISTRY).unwrap();
    assert_eq!(
        plan.items
            .iter()
            .map(|item| item.item.target.clone())
            .collect::<Vec<_>>(),
        [
            root.join("AGENTS.md"),
            root.join("CLAUDE.md"),
            root.join("GEMINI.md")
        ]
    );
    let ids: Vec<_> = plan.items.iter().map(|item| &item.id).collect();
    let report = sync_apply(&ctx, &sync_project, &plan, &ids, REGISTRY).unwrap();
    assert!(report.apply.stop.is_none());
    assert_eq!(report.apply.receipts.len(), 3);
    assert_eq!(
        fs::read(root.join("GEMINI.md")).unwrap(),
        b"Gemini notes\n@AGENTS.md\n"
    );
    assert!(cursor.is_dir());
}

#[test]
fn rules_ids_follow_the_target_and_action_and_match_the_apply_report() {
    let (_dir, root, session) = project();
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let sync_project = SyncProject::read(&root).unwrap();
    let path = root.join("AGENTS.md");
    let read_plan = || sync_plan(&ctx, &sync_project, REGISTRY).unwrap();
    let agents_id = |plan: &oah_core::app::sync::SyncPlan, action: Action| {
        let item = plan
            .plan
            .items
            .iter()
            .find(|item| item.target == path)
            .unwrap();
        assert_eq!(item.action, action);
        item.id.clone()
    };
    let create = agents_id(&read_plan(), Action::Create);
    assert_eq!(create, agents_id(&read_plan(), Action::Create));
    let other_root = root.with_file_name("other-project");
    fs::create_dir(&other_root).unwrap();
    let other = SyncProject::read(&other_root).unwrap();
    let other_plan = sync_plan(&ctx, &other, REGISTRY).unwrap();
    let other_agents = other_plan
        .plan
        .items
        .iter()
        .find(|item| item.target == other_root.join("AGENTS.md"))
        .unwrap();
    assert_eq!(other_agents.action, Action::Create);
    assert_ne!(create, other_agents.id);

    fs::write(&path, b"user text\n").unwrap();
    let plan = read_plan();
    let append = agents_id(&plan, Action::Append);
    assert_ne!(create, append);
    assert_eq!(append, agents_id(&read_plan(), Action::Append));
    let ignored = sync_apply(&ctx, &sync_project, &plan, &[create.as_str()], REGISTRY).unwrap();
    assert!(ignored.apply.items.is_empty());
    assert_eq!(fs::read(&path).unwrap(), b"user text\n");
    let report = sync_apply(&ctx, &sync_project, &plan, &[append.as_str()], REGISTRY).unwrap();
    assert_eq!(report.apply.status_of(&append), Some(ItemStatus::Done));
    assert_eq!(report.steps[0].items, report.apply.items);
    assert_eq!(report.apply.items.len(), 1);
    assert!(!root.join("CLAUDE.md").exists());

    fs::create_dir(root.join(".oah")).unwrap();
    fs::write(root.join(".oah/rules.local.md"), b"New local guidance.\n").unwrap();
    let plan = read_plan();
    let replace = agents_id(&plan, Action::ReplaceBlock);
    assert_ne!(replace, create);
    assert_ne!(replace, append);
    assert_eq!(replace, agents_id(&read_plan(), Action::ReplaceBlock));
    let report = sync_apply(&ctx, &sync_project, &plan, &[replace.as_str()], REGISTRY).unwrap();
    assert_eq!(report.apply.status_of(&replace), Some(ItemStatus::Done));
    assert_eq!(report.steps[0].items, report.apply.items);
    let text = fs::read_to_string(&path).unwrap();
    assert!(text.starts_with("user text\n"));
    assert!(text.contains("New local guidance."));
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

#[cfg(unix)]
#[test]
fn linked_local_rules_is_refused_at_sync_plan_and_sync_apply() {
    use std::os::unix::fs::symlink;

    // 1. Leaf symlink at sync_plan time becomes an ordinary Rules refusal warning.
    let (_dir, root, session) = project();
    let inert = root.join("inert.md");
    fs::write(&inert, b"inert fixture\n").unwrap();
    fs::create_dir(root.join(".oah")).unwrap();
    symlink(&inert, root.join(".oah/rules.local.md")).unwrap();
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let planned = sync_plan(&ctx, &SyncProject::read(&root).unwrap(), REGISTRY).unwrap();
    assert!(planned.items.is_empty());
    assert_eq!(planned.plan.warnings.len(), 1);
    assert_eq!(planned.plan.warnings[0].code, "OAH_RULES_LINK");
    assert!(!root.join("AGENTS.md").exists());

    // 2. Traversed `.oah` ancestor symlink at sync_plan time becomes an ordinary Rules refusal warning.
    let (_dir, root, session) = project();
    let inert_dir = root.join("inert-dir");
    fs::create_dir(&inert_dir).unwrap();
    symlink(&inert_dir, root.join(".oah")).unwrap();
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let planned = sync_plan(&ctx, &SyncProject::read(&root).unwrap(), REGISTRY).unwrap();
    assert!(planned.items.is_empty());
    assert_eq!(planned.plan.warnings.len(), 1);
    assert_eq!(planned.plan.warnings[0].code, "OAH_RULES_LINK");
    assert!(!root.join("AGENTS.md").exists());

    // 3. Regular local rules replaced by an in-project symlink (even with identical bytes) before sync_apply.
    let (_dir, root, session) = project();
    let same_bytes = b"Original local guidance.\n";
    let inert = root.join("inert.md");
    fs::write(&inert, same_bytes).unwrap();
    fs::create_dir(root.join(".oah")).unwrap();
    fs::write(root.join(".oah/rules.local.md"), same_bytes).unwrap();
    let cancel = CancelToken::new();
    let ctx = session.ctx(&cancel, &NoProgress);
    let sync_project = SyncProject::read(&root).unwrap();
    let planned = sync_plan(&ctx, &sync_project, REGISTRY).unwrap();
    let ids: Vec<&str> = planned.items.iter().map(|item| item.id.as_str()).collect();
    assert!(!ids.is_empty());
    fs::remove_file(root.join(".oah/rules.local.md")).unwrap();
    symlink(&inert, root.join(".oah/rules.local.md")).unwrap();
    let error = sync_apply(&ctx, &sync_project, &planned, &ids, REGISTRY).unwrap_err();
    assert_eq!(error.code(), "OAH_RULES_LINK");
    assert!(!root.join("AGENTS.md").exists());
    assert!(!root.join("CLAUDE.md").exists());
}
