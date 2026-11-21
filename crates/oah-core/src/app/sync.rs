// 동기화 파이프라인: C11 의 다섯 단계 id 를 한 순서로 두고, 등록된 단계만 계획·적용하며 등록된 단계만 보고한다.
//! The sync pipeline (CONTRACTS C11, C18), in the minimal form its first consumer to land ships: M2-S1 found no
//! `app/sync.rs` at its base, so it creates the step enum with all five ids, the registry with its own row
//! (step 3, the rules block and thin files), [`sync_plan`], [`sync_apply`] over the C4 apply engine and
//! [`SyncReport`]. Every later slice adds only its own row and never reorders or removes another.
//!
//! A step whose slice has not landed has no row, so it is never planned and never reported as done: the report
//! lists registered steps only. [`sync_plan`] takes the registry as a parameter (production passes [`REGISTRY`]),
//! so tests supply their own planners without a shipped stub. Triggers (manual refresh, `oah sync`, project open)
//! belong to later slices; an assignment change does not run this pipeline, `workflow::set_stages` calls the rules
//! writer itself (C11 trigger table).

use super::{Ctx, agents_list};
use crate::config::{AgentId, ProjectConfig};
use crate::dto::{Empty, NoticeDto};
use crate::error::Error;
use crate::fsx::LockOptions;
use crate::plan::{self, ApplyReport, Basis, ChangePlan, ItemResult, Outcome, PlanItem, Target};
use crate::rules::{RulesSnapshot, SyncInput, plan_sync, write_file};
use std::path::{Path, PathBuf};

/// The five sync steps, in their one order (C11).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum StepId {
    /// 1. Link repair, including local Claude Code project links (M1-S5).
    LinkRepair,
    /// 2. Native switches for installed, reviewed skills on a newly detected agent (M1-S5).
    NativeSwitches,
    /// 3. Rules block and thin files re-sync after a config change (M2-S1).
    Rules,
    /// 4. Model reference rewrite for retired generations in the project config (M2-S4).
    ModelRewrite,
    /// 5. Hook entry refresh after an app update (M2-S5).
    HookRefresh,
}

impl StepId {
    /// Every step id, in order.
    pub const ALL: [StepId; 5] = [
        StepId::LinkRepair,
        StepId::NativeSwitches,
        StepId::Rules,
        StepId::ModelRewrite,
        StepId::HookRefresh,
    ];

    /// The step's number in C11's table.
    pub fn number(self) -> u8 {
        match self {
            StepId::LinkRepair => 1,
            StepId::NativeSwitches => 2,
            StepId::Rules => 3,
            StepId::ModelRewrite => 4,
            StepId::HookRefresh => 5,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            StepId::LinkRepair => "link-repair",
            StepId::NativeSwitches => "native-switches",
            StepId::Rules => "rules",
            StepId::ModelRewrite => "model-rewrite",
            StepId::HookRefresh => "hook-refresh",
        }
    }
}

/// When an item may run (§8.3): always, or only under `auto-safe`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Row {
    Always,
    AutoSafe,
}

/// The project a sync runs for, read once.
#[derive(Debug, Clone)]
pub struct SyncProject {
    pub root: PathBuf,
    pub config: ProjectConfig,
}

impl SyncProject {
    pub fn read(root: &Path) -> Result<SyncProject, Error> {
        Ok(SyncProject {
            root: root.to_path_buf(),
            config: ProjectConfig::read(&root.join(".oah").join("config.toml"))?,
        })
    }
}

/// One planned piece of sync work. `id` is derived from (step, target, action), so the same work has the same id
/// in a plan and in its apply; `content` is what the step's executor writes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncItem {
    pub id: String,
    pub step: StepId,
    pub item: PlanItem,
    pub basis: Basis,
    pub row: Row,
    pub content: Vec<u8>,
}

/// What one step's planner found: its items, and notices such as a refusal that planned nothing.
#[derive(Debug, Default)]
pub struct StepPlan {
    pub items: Vec<SyncItem>,
    pub notices: Vec<NoticeDto>,
    pub source_basis: Vec<Basis>,
}

/// A step's pure planner over a snapshot.
pub type Planner = fn(&Ctx<'_>, &SyncProject) -> Result<StepPlan, Error>;
/// A step's executor for one target and its accepted items; run by the C4 apply engine.
pub type Executor = fn(&Ctx<'_>, &SyncProject, &Target<'_>, &[&SyncItem]) -> Outcome;

/// One registered step.
#[derive(Clone, Copy)]
pub struct StepRow {
    pub step: StepId,
    pub plan: Planner,
    pub execute: Executor,
}

/// The production registry: one row per landed step, in step order. M2-S1 registers step 3.
pub static REGISTRY: &[StepRow] = &[StepRow {
    step: StepId::Rules,
    plan: rules_plan,
    execute: rules_execute,
}];

/// A sync plan: the C4 change plan over every item, the items with their steps, and the steps that ran.
#[derive(Debug, Clone)]
pub struct SyncPlan {
    pub plan: ChangePlan,
    pub items: Vec<SyncItem>,
    pub steps: Vec<StepId>,
}

/// One step's share of an apply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StepReport {
    pub step: StepId,
    pub items: Vec<ItemResult>,
}

/// What a sync apply did: per registered step, its items' result classes; and the engine's report.
#[derive(Debug)]
pub struct SyncReport {
    pub steps: Vec<StepReport>,
    pub apply: ApplyReport,
}

/// Plans every step of `registry` in its order; writes nothing.
pub fn sync_plan(
    ctx: &Ctx<'_>,
    project: &SyncProject,
    registry: &[StepRow],
) -> Result<SyncPlan, Error> {
    let mut items: Vec<SyncItem> = Vec::new();
    let mut warnings: Vec<NoticeDto> = Vec::new();
    let mut steps = Vec::new();
    let mut basis: Vec<Basis> = Vec::new();
    for row in registry {
        ctx.cancel.check()?;
        let planned = (row.plan)(ctx, project)?;
        for snapshot in planned
            .source_basis
            .into_iter()
            .chain(planned.items.iter().map(|item| item.basis.clone()))
        {
            if !basis.contains(&snapshot) {
                basis.push(snapshot);
            }
        }
        items.extend(planned.items);
        warnings.extend(planned.notices);
        steps.push(row.step);
    }
    Ok(SyncPlan {
        plan: ChangePlan {
            id: String::from("sync"),
            items: items.iter().map(|item| item.item.clone()).collect(),
            warnings,
            basis,
        },
        items,
        steps,
    })
}

/// Applies the accepted items of `plan` through the C4 engine, each target by its step's executor, and records
/// every verified write in the session. Ids outside the plan are ignored.
pub fn sync_apply<S: AsRef<str>>(
    ctx: &Ctx<'_>,
    project: &SyncProject,
    plan: &SyncPlan,
    accepted: &[S],
    registry: &[StepRow],
) -> Result<SyncReport, Error> {
    let chosen = plan.plan.accept(accepted);
    let apply = plan::apply(&chosen, ctx.cancel, |target| {
        let items: Vec<&SyncItem> = plan
            .items
            .iter()
            .filter(|item| target.items.iter().any(|accepted| accepted.id == item.id))
            .collect();
        let Some(row) = items
            .first()
            .and_then(|first| registry.iter().find(|row| row.step == first.step))
        else {
            return Outcome::Failed(
                plan::PlanError::MissingBasis {
                    target: target.path.to_path_buf(),
                }
                .into(),
            );
        };
        (row.execute)(ctx, project, target, &items)
    })?;
    for receipt in &apply.receipts {
        ctx.session.self_writes().record(receipt);
    }
    let steps = plan
        .steps
        .iter()
        .map(|step| StepReport {
            step: *step,
            items: apply
                .items
                .iter()
                .filter(|result| {
                    plan.items
                        .iter()
                        .any(|item| item.id == result.id && item.step == *step)
                })
                .cloned()
                .collect(),
        })
        .collect();
    Ok(SyncReport { steps, apply })
}

/// Step 3's planner: the rules writer's plan from the project config and the session's agent detection; a refusal
/// plans nothing and becomes a warning with its reason.
fn rules_plan(ctx: &Ctx<'_>, project: &SyncProject) -> Result<StepPlan, Error> {
    let agents = agents_list(ctx, Empty {})?;
    let detected: Vec<AgentId> = agents
        .iter()
        .filter(|agent| agent.installed)
        .map(|agent| agent.id.0)
        .collect();
    let snapshot = RulesSnapshot::read(&project.root, &detected)?;
    let input = SyncInput {
        config: &project.config,
        always: &[],
        lang: None,
    };
    let planned = match plan_sync(&snapshot, &input) {
        Ok(planned) => planned,
        Err(refusal) => {
            let message = refusal.message.clone();
            return Ok(StepPlan {
                notices: vec![NoticeDto::warning(
                    refusal.code(),
                    StepId::Rules.as_str(),
                    message,
                )],
                ..StepPlan::default()
            });
        }
    };
    let items = planned
        .writes()
        .map(|file| {
            let item = file.plan_item(&project.root);
            SyncItem {
                id: item.id.clone(),
                step: StepId::Rules,
                item,
                basis: file.basis(&project.root),
                row: Row::Always,
                content: file.after.clone(),
            }
        })
        .collect();
    Ok(StepPlan {
        items,
        notices: Vec::new(),
        source_basis: Vec::from(planned.sources),
    })
}

/// Step 3's executor: the rules writer for one file (folders after a link check, lock, basis re-check, atomic
/// write, read-back).
fn rules_execute(
    _ctx: &Ctx<'_>,
    project: &SyncProject,
    target: &Target<'_>,
    items: &[&SyncItem],
) -> Outcome {
    let Some(item) = items.last() else {
        return Outcome::Done(Vec::new());
    };
    write_file(&project.root, target, &item.content, LockOptions::default())
}
