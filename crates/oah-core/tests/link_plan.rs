// 링크 계획 시험: 공개 link_plan 함수를 합성 폴더·능력 근거로 불러 전역·프로젝트 배치의 동작을 확인한다.
use oah_core::agents::{Basis, Capability, Detection, Evidence, Os, Status, installed};
use oah_core::config::AgentId;
use oah_core::fsx::links::TargetStyle;
use oah_core::skills::{
    AgentView, LinkAction, LinkPlanRequest, Scope, SkillDirs, SwitchOp, link_plan,
};
use std::path::{Path, PathBuf};

const HOME: &str = "/synthetic/home";
const REPO: &str = "/synthetic/repo";
const NOT_VERIFIED: &str = "not verified on this system";

fn evidence(basis: Basis, os: &'static [Os]) -> Evidence {
    Evidence {
        basis,
        source: "synthetic",
        verified_on: None,
        os,
    }
}

fn verified(basis: Basis) -> Capability<()> {
    Capability::Verified {
        value: (),
        evidence: evidence(basis, &[Os::Macos]),
    }
}

fn paths(root: &str, names: &[&str]) -> Vec<PathBuf> {
    names
        .iter()
        .map(|name| Path::new(root).join(name))
        .collect()
}

fn dirs(root: &str, primary: &str, also: &[&str]) -> SkillDirs {
    SkillDirs {
        primary: Path::new(root).join(primary),
        also_reads: paths(root, also),
    }
}

/// An installed agent whose link and switch are measured on macOS.
fn view(id: AgentId, dirs: SkillDirs) -> AgentView {
    AgentView {
        id,
        installed: true,
        dirs,
        link: verified(Basis::Measured),
        switch: verified(Basis::Measured),
    }
}

/// The seven agents of DESIGN-C2 4.3 at user level.
fn global_views() -> Vec<AgentView> {
    let h = HOME;
    vec![
        view(AgentId::Claude, dirs(h, ".claude/skills", &[])),
        view(
            AgentId::Codex,
            dirs(h, ".agents/skills", &[".codex/skills"]),
        ),
        view(
            AgentId::Gemini,
            dirs(h, ".gemini/skills", &[".agents/skills"]),
        ),
        view(
            AgentId::Copilot,
            dirs(h, ".copilot/skills", &[".agents/skills"]),
        ),
        view(
            AgentId::Opencode,
            dirs(
                h,
                ".config/opencode/skills",
                &[".claude/skills", ".agents/skills"],
            ),
        ),
        view(
            AgentId::Antigravity,
            dirs(
                h,
                ".gemini/config/skills",
                &[".gemini/antigravity-cli/skills", ".gemini/skills"],
            ),
        ),
        view(
            AgentId::Cursor,
            dirs(
                h,
                ".cursor/skills",
                &[".agents/skills", ".claude/skills", ".codex/skills"],
            ),
        ),
    ]
}

/// The seven agents of DESIGN-C2 4.3 at project level.
fn project_views() -> Vec<AgentView> {
    let r = REPO;
    vec![
        view(AgentId::Claude, dirs(r, ".claude/skills", &[])),
        view(AgentId::Codex, dirs(r, ".agents/skills", &[])),
        view(
            AgentId::Gemini,
            dirs(r, ".gemini/skills", &[".agents/skills"]),
        ),
        view(
            AgentId::Copilot,
            dirs(r, ".github/skills", &[".claude/skills", ".agents/skills"]),
        ),
        view(
            AgentId::Opencode,
            dirs(r, ".opencode/skills", &[".claude/skills", ".agents/skills"]),
        ),
        view(AgentId::Antigravity, dirs(r, ".agents/skills", &[])),
        view(
            AgentId::Cursor,
            dirs(
                r,
                ".cursor/skills",
                &[".agents/skills", ".claude/skills", ".codex/skills"],
            ),
        ),
    ]
}

fn global() -> Scope {
    Scope::Global { home: HOME.into() }
}

fn project() -> Scope {
    Scope::Project { repo: REPO.into() }
}

fn plan(scope: Scope, targets: &[AgentId], agents: &[AgentView]) -> Vec<LinkAction> {
    link_plan(&LinkPlanRequest {
        skill: "pdf-tools",
        scope,
        targets,
        agents,
        os: Os::Macos,
    })
}

fn action_of(actions: &[LinkAction], agent: AgentId) -> Option<&LinkAction> {
    actions.iter().find(|action| action.agent() == agent)
}

fn without(mut views: Vec<AgentView>, ids: &[AgentId]) -> Vec<AgentView> {
    views.retain(|view| !ids.contains(&view.id));
    views
}

fn store(root: &str) -> PathBuf {
    Path::new(root).join(".agents/skills/pdf-tools")
}

fn link(agent: AgentId, root: &str, primary: &str, style: TargetStyle) -> LinkAction {
    LinkAction::Link {
        agent,
        from: store(root),
        to: Path::new(root).join(primary).join("pdf-tools"),
        style,
    }
}

fn skipped(agent: AgentId, why: &'static str) -> LinkAction {
    LinkAction::Skipped { agent, why }
}

#[test]
fn global_plan_links_claude_reads_codex_natively_and_flags_cursor() {
    // The design example: targets Claude Code and Codex; Cursor installed and not a target; the rest absent.
    let views = without(
        global_views(),
        &[
            AgentId::Gemini,
            AgentId::Copilot,
            AgentId::Opencode,
            AgentId::Antigravity,
        ],
    );
    let mut views = views;
    views
        .iter_mut()
        .for_each(|view| view.switch = Capability::None);

    let actions = plan(global(), &[AgentId::Claude, AgentId::Codex], &views);

    assert_eq!(
        actions,
        vec![
            link(
                AgentId::Claude,
                HOME,
                ".claude/skills",
                TargetStyle::Absolute
            ),
            LinkAction::Native {
                agent: AgentId::Codex
            },
            LinkAction::VisibleAnyway {
                agent: AgentId::Cursor
            },
        ]
    );
}

#[test]
fn direct_readers_need_nothing_and_only_claude_and_antigravity_need_links() {
    let targets = AgentId::ALL;
    let actions = plan(global(), &targets, &global_views());

    assert_eq!(
        actions,
        vec![
            link(
                AgentId::Claude,
                HOME,
                ".claude/skills",
                TargetStyle::Absolute
            ),
            LinkAction::Native {
                agent: AgentId::Codex
            },
            LinkAction::Native {
                agent: AgentId::Gemini
            },
            LinkAction::Native {
                agent: AgentId::Copilot
            },
            LinkAction::Native {
                agent: AgentId::Opencode
            },
            link(
                AgentId::Antigravity,
                HOME,
                ".gemini/config/skills",
                TargetStyle::Absolute
            ),
            LinkAction::Native {
                agent: AgentId::Cursor
            },
        ]
    );
}

#[test]
fn a_non_target_reading_the_store_gets_its_switch_or_stays_visible() {
    // Only Codex is a target. Gemini, Copilot, OpenCode and Cursor read the store folder too.
    let mut views = global_views();
    for view in &mut views {
        match view.id {
            AgentId::Opencode => {
                view.switch = Capability::Unknown { why: "open item" };
            }
            AgentId::Cursor => view.switch = Capability::None,
            _ => {}
        }
    }

    let actions = plan(global(), &[AgentId::Codex], &views);

    assert_eq!(
        actions,
        vec![
            LinkAction::Native {
                agent: AgentId::Codex
            },
            LinkAction::Switch {
                agent: AgentId::Gemini,
                op: SwitchOp::Disable
            },
            LinkAction::Switch {
                agent: AgentId::Copilot,
                op: SwitchOp::Disable
            },
            LinkAction::VisibleAnyway {
                agent: AgentId::Opencode
            },
            LinkAction::VisibleAnyway {
                agent: AgentId::Cursor
            },
        ]
    );
    // Claude Code and Antigravity read no folder that holds the skill: nothing is planned for them.
    assert_eq!(action_of(&actions, AgentId::Claude), None);
    assert_eq!(action_of(&actions, AgentId::Antigravity), None);
}

#[test]
fn a_new_link_destination_is_visible_to_other_installed_readers() {
    // Claude Code is linked into ~/.claude/skills. This OpenCode reads only that folder, not the store, yet it
    // will see the new link, so it is not left out of the plan.
    let mut views = global_views();
    for view in &mut views {
        if view.id == AgentId::Opencode {
            view.dirs = dirs(HOME, ".config/opencode/skills", &[".claude/skills"]);
        }
    }

    let actions = plan(global(), &[AgentId::Claude], &views);

    assert_eq!(
        action_of(&actions, AgentId::Claude),
        Some(&link(
            AgentId::Claude,
            HOME,
            ".claude/skills",
            TargetStyle::Absolute
        ))
    );
    assert_eq!(
        action_of(&actions, AgentId::Opencode),
        Some(&LinkAction::Switch {
            agent: AgentId::Opencode,
            op: SwitchOp::Disable
        })
    );
    // Without that link the same OpenCode view gets nothing.
    let none = plan(global(), &[AgentId::Codex], &views);
    assert_eq!(action_of(&none, AgentId::Opencode), None);
}

#[test]
fn a_target_that_sees_an_earlier_link_gets_no_second_copy() {
    // Gemini CLI is linked into ~/.gemini/skills; the Antigravity CLI reads that folder.
    let actions = plan(
        global(),
        &[AgentId::Gemini, AgentId::Antigravity],
        &without(global_views(), &[]),
    );

    // Gemini CLI reads the store natively, so it needs no link and Antigravity gets its own.
    assert_eq!(
        action_of(&actions, AgentId::Gemini),
        Some(&LinkAction::Native {
            agent: AgentId::Gemini
        })
    );
    assert_eq!(
        action_of(&actions, AgentId::Antigravity),
        Some(&link(
            AgentId::Antigravity,
            HOME,
            ".gemini/config/skills",
            TargetStyle::Absolute
        ))
    );

    // A Gemini CLI that does not read the store is linked, and then Antigravity needs no copy of its own.
    let mut views = global_views();
    for view in &mut views {
        if view.id == AgentId::Gemini {
            view.dirs = dirs(HOME, ".gemini/skills", &[]);
        }
    }
    let actions = plan(global(), &[AgentId::Gemini, AgentId::Antigravity], &views);
    assert_eq!(
        action_of(&actions, AgentId::Gemini),
        Some(&link(
            AgentId::Gemini,
            HOME,
            ".gemini/skills",
            TargetStyle::Absolute
        ))
    );
    assert_eq!(
        action_of(&actions, AgentId::Antigravity),
        Some(&LinkAction::Native {
            agent: AgentId::Antigravity
        })
    );
}

#[test]
fn project_plan_links_claude_relatively_and_the_rest_read_the_project_store() {
    let actions = plan(
        project(),
        &[AgentId::Claude, AgentId::Codex],
        &project_views(),
    );

    assert_eq!(
        actions,
        vec![
            link(
                AgentId::Claude,
                REPO,
                ".claude/skills",
                TargetStyle::Relative
            ),
            LinkAction::Native {
                agent: AgentId::Codex
            },
            LinkAction::Switch {
                agent: AgentId::Gemini,
                op: SwitchOp::Disable
            },
            LinkAction::Switch {
                agent: AgentId::Copilot,
                op: SwitchOp::Disable
            },
            LinkAction::Switch {
                agent: AgentId::Opencode,
                op: SwitchOp::Disable
            },
            LinkAction::Switch {
                agent: AgentId::Antigravity,
                op: SwitchOp::Disable
            },
            LinkAction::Switch {
                agent: AgentId::Cursor,
                op: SwitchOp::Disable
            },
        ]
    );
}

#[test]
fn project_plan_for_claude_alone_still_reaches_agents_that_read_claude_skills() {
    let actions = plan(project(), &[AgentId::Claude], &project_views());

    assert_eq!(
        actions[0],
        link(
            AgentId::Claude,
            REPO,
            ".claude/skills",
            TargetStyle::Relative
        )
    );
    // Every other installed agent reads the project store or .claude/skills, so each has an outcome.
    assert_eq!(actions.len(), 7);
}

#[test]
fn unknown_link_capability_skips_and_never_links() {
    let mut views = global_views();
    for view in &mut views {
        if matches!(view.id, AgentId::Claude | AgentId::Antigravity) {
            view.link = Capability::Unknown { why: "open item" };
        }
    }

    let actions = plan(global(), &[AgentId::Claude, AgentId::Antigravity], &views);

    assert_eq!(
        action_of(&actions, AgentId::Claude),
        Some(&skipped(AgentId::Claude, NOT_VERIFIED))
    );
    assert_eq!(
        action_of(&actions, AgentId::Antigravity),
        Some(&skipped(AgentId::Antigravity, NOT_VERIFIED))
    );
    assert!(
        actions
            .iter()
            .all(|action| !matches!(action, LinkAction::Link { .. }))
    );
}

#[test]
fn documented_or_differently_scoped_evidence_cannot_authorize_a_link() {
    let cases = [
        ("docs only", verified(Basis::Docs)),
        (
            "another system",
            Capability::Verified {
                value: (),
                evidence: evidence(Basis::Measured, &[Os::Windows]),
            },
        ),
        (
            "carried on another system",
            Capability::Verified {
                value: (),
                evidence: evidence(Basis::Carried, &[Os::Linux]),
            },
        ),
        (
            "no system covered",
            Capability::Verified {
                value: (),
                evidence: evidence(Basis::Measured, &[]),
            },
        ),
    ];
    for (label, capability) in cases {
        let mut views = global_views();
        views[0].link = capability;
        let actions = plan(global(), &[AgentId::Claude], &views);
        assert_eq!(
            action_of(&actions, AgentId::Claude),
            Some(&skipped(AgentId::Claude, NOT_VERIFIED)),
            "{label}"
        );
    }

    // Carried and measured evidence for this system do authorize.
    for basis in [Basis::Carried, Basis::Measured] {
        let mut views = global_views();
        views[0].link = verified(basis);
        let actions = plan(global(), &[AgentId::Claude], &views);
        assert!(
            matches!(
                action_of(&actions, AgentId::Claude),
                Some(LinkAction::Link { .. })
            ),
            "{basis:?}"
        );
    }
}

#[test]
fn documented_or_unscoped_switch_evidence_leaves_the_agent_visible_not_switched() {
    let cases = [
        verified(Basis::Docs),
        Capability::Verified {
            value: (),
            evidence: evidence(Basis::Measured, &[Os::Windows]),
        },
        Capability::Unknown { why: "open item" },
        Capability::None,
    ];
    for capability in cases {
        let mut views = global_views();
        for view in &mut views {
            if view.id == AgentId::Gemini {
                view.switch = capability.clone();
            }
        }
        let actions = plan(global(), &[AgentId::Codex], &views);
        assert_eq!(
            action_of(&actions, AgentId::Gemini),
            Some(&LinkAction::VisibleAnyway {
                agent: AgentId::Gemini
            })
        );
    }
}

#[test]
fn a_target_that_cannot_link_by_design_is_skipped_with_its_own_reason() {
    let mut views = global_views();
    views[0].link = Capability::None;

    let actions = plan(global(), &[AgentId::Claude], &views);

    assert_eq!(
        action_of(&actions, AgentId::Claude),
        Some(&skipped(
            AgentId::Claude,
            "this agent cannot read linked skill folders"
        ))
    );
}

#[test]
fn missing_agents_get_no_link_switch_or_folder() {
    let views = without(global_views(), &[AgentId::Claude, AgentId::Cursor]);
    let mut views = views;
    // An agent present in the input but not installed behaves like an absent one.
    for view in &mut views {
        if view.id == AgentId::Gemini {
            view.installed = false;
        }
    }

    let actions = plan(
        global(),
        &[AgentId::Claude, AgentId::Gemini, AgentId::Codex],
        &views,
    );

    assert_eq!(
        action_of(&actions, AgentId::Claude),
        Some(&skipped(AgentId::Claude, "not installed"))
    );
    assert_eq!(
        action_of(&actions, AgentId::Gemini),
        Some(&skipped(AgentId::Gemini, "not installed"))
    );
    assert_eq!(
        action_of(&actions, AgentId::Codex),
        Some(&LinkAction::Native {
            agent: AgentId::Codex
        })
    );
    // A non-target that is missing is never switched, flagged or linked.
    assert_eq!(action_of(&actions, AgentId::Cursor), None);
    assert!(actions.iter().all(|action| match action {
        LinkAction::Skipped { .. } => true,
        other => other.agent() != AgentId::Claude && other.agent() != AgentId::Gemini,
    }));
}

#[test]
fn actions_follow_the_fixed_agent_order_whatever_the_input_order() {
    let mut views = global_views();
    views.reverse();
    let targets = [
        AgentId::Cursor,
        AgentId::Claude,
        AgentId::Codex,
        AgentId::Claude,
    ];

    let actions = plan(global(), &targets, &views);
    let order: Vec<AgentId> = actions.iter().map(LinkAction::agent).collect();

    let mut sorted = order.clone();
    sorted.sort_by_key(|id| AgentId::ALL.iter().position(|other| other == id));
    assert_eq!(order, sorted);
    assert_eq!(
        order.iter().filter(|id| **id == AgentId::Claude).count(),
        1,
        "a repeated target counts once"
    );
    assert_eq!(
        actions,
        plan(
            global(),
            &[AgentId::Claude, AgentId::Codex, AgentId::Cursor],
            &global_views()
        )
    );
}

#[test]
fn the_plan_is_pure_and_touches_neither_the_inputs_nor_the_file_system() {
    let views = global_views();
    let before = views.clone();

    let first = plan(global(), &[AgentId::Claude, AgentId::Antigravity], &views);
    let second = plan(global(), &[AgentId::Claude, AgentId::Antigravity], &views);

    assert_eq!(first, second);
    assert_eq!(views, before);
    assert!(
        !Path::new(HOME).exists(),
        "the synthetic home must not be created"
    );
    assert!(
        !Path::new(REPO).exists(),
        "the synthetic repo must not be created"
    );
}

#[test]
fn an_unsafe_skill_name_plans_nothing_but_skips() {
    for name in ["", ".", "..", "../escape", "a/b", "a\\b"] {
        let actions = link_plan(&LinkPlanRequest {
            skill: name,
            scope: global(),
            targets: &[AgentId::Claude, AgentId::Codex],
            agents: &global_views(),
            os: Os::Macos,
        });
        assert_eq!(
            actions,
            vec![
                skipped(AgentId::Claude, "not a safe skill folder name"),
                skipped(AgentId::Codex, "not a safe skill folder name"),
            ],
            "{name:?}"
        );
    }
}

#[test]
fn views_project_from_a_detection_witness_and_a_missing_agent() {
    let detection = Detection {
        agent: AgentId::Claude,
        status: Status::Ok,
        version: Some("2.1.0".into()),
        source: Some("/synthetic/bin/claude".into()),
        problem: None,
    };
    let witness = installed(&detection).expect("an ok detection is installed");
    let mut views = vec![AgentView::installed(
        &witness,
        dirs(HOME, ".claude/skills", &[]),
        verified(Basis::Measured),
        Capability::None,
    )];
    views.push(AgentView::missing(AgentId::Codex));

    let actions = plan(global(), &[AgentId::Claude, AgentId::Codex], &views);

    assert_eq!(
        actions,
        vec![
            link(
                AgentId::Claude,
                HOME,
                ".claude/skills",
                TargetStyle::Absolute
            ),
            skipped(AgentId::Codex, "not installed"),
        ]
    );
}
