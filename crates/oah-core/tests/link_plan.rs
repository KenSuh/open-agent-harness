// 링크 계획 시험: 공개 link_plan 함수를 합성 폴더·능력 근거로 불러 전역·프로젝트 배치의 동작을 확인한다.
#[path = "support/fake_agents.rs"]
mod fake_agents;

use fake_agents::FakeProbe;
use oah_core::agents::{
    Basis, Capability, Detection, Evidence, InstalledAgent, Os, Resolution, RunError, detect,
    installed,
};
use oah_core::config::AgentId;
use oah_core::fsx::links::TargetStyle;
use oah_core::skills::{
    AgentView, LinkAction, LinkPlanRequest, PlannedLink, Scope, SkillDirs, SwitchOp, link_plan,
};
use std::path::{Component, Path, PathBuf};

const SKILL: &str = "pdf-tools";
const NOT_VERIFIED: &str = "not verified on this system";
const CANNOT_LINK: &str = "this agent cannot read linked skill folders";
const NOT_INSTALLED: &str = "not installed";
const UNSAFE_NAME: &str = "not a safe skill folder name";
const UNSAFE_ROOT: &str = "the install folder is not a plain absolute path";
const UNSAFE_DIRS: &str = "its skill folders are not plain absolute folders of this scope";
const AMBIGUOUS: &str = "two different views of this agent were given";

/// A synthetic absolute folder that is never created, in the host's own path form.
fn synthetic(name: &str) -> PathBuf {
    let base = if cfg!(windows) {
        r"C:\synthetic"
    } else {
        "/synthetic"
    };
    Path::new(base).join(name)
}

fn home() -> PathBuf {
    synthetic("home")
}

fn repo() -> PathBuf {
    synthetic("repo")
}

/// `relative`, a folder written with `/`, under `root`, joined part by part.
fn under(root: &Path, relative: &str) -> PathBuf {
    relative
        .split('/')
        .fold(root.to_path_buf(), |path, part| path.join(part))
}

fn dirs(root: &Path, primary: &str, also: &[&str]) -> SkillDirs {
    SkillDirs {
        primary: under(root, primary),
        also_reads: also.iter().map(|folder| under(root, folder)).collect(),
    }
}

/// Complete evidence on `basis` for `os`: a source, and the version a measurement saw.
fn evidence(basis: Basis, os: &'static [Os]) -> Evidence {
    Evidence {
        basis,
        source: "synthetic",
        verified_on: (basis == Basis::Measured).then_some("1.2.3"),
        os,
    }
}

fn verified(basis: Basis) -> Capability<()> {
    Capability::Verified {
        value: (),
        evidence: evidence(basis, &[Os::Macos]),
    }
}

fn measured() -> Capability<()> {
    verified(Basis::Measured)
}

fn unknown() -> Capability<()> {
    Capability::Unknown { why: "open item" }
}

fn all_measured(_: AgentId) -> (Capability<()>, Capability<()>) {
    (measured(), measured())
}

/// A fake machine on which `id` is installed, for the agent's own adapter to detect.
fn machine(id: AgentId) -> FakeProbe {
    let probe = FakeProbe::new(Os::Macos);
    match id {
        AgentId::Claude => probe.program("claude", "/bin/claude").run(
            "/bin/claude --version",
            Some(0),
            "2.1.318 (Claude Code)\n",
            "",
        ),
        AgentId::Codex => probe.program("codex", "/bin/codex").run(
            "/bin/codex --version",
            Some(0),
            "codex-cli 0.163.0\n",
            "",
        ),
        AgentId::Gemini => probe.program("gemini", "/bin/gemini").run(
            "/bin/gemini --version",
            Some(0),
            "1.2.3\n",
            "",
        ),
        AgentId::Copilot => probe.program("copilot", "/bin/copilot").run(
            "/bin/copilot version",
            Some(0),
            "1.2.3\n",
            "",
        ),
        AgentId::Opencode => probe.program("opencode", "/bin/opencode").run(
            "/bin/opencode --version",
            Some(0),
            "1.2.3\n",
            "",
        ),
        AgentId::Antigravity => probe.program("agy", "/bin/agy"),
        AgentId::Cursor => probe.bundle("/Applications/Cursor.app"),
    }
}

/// The installed-agent witness the detection adapter builds for `id` on its fake machine.
fn witness(id: AgentId) -> InstalledAgent {
    installed(&detect(id, &machine(id), None)).expect("the fake machine has the agent installed")
}

/// The view a detection gives: an installed one through its witness, else a missing one.
fn view_from(
    detection: &Detection,
    dirs: SkillDirs,
    link: Capability<()>,
    switch: Capability<()>,
) -> AgentView {
    match installed(detection) {
        Some(agent) => AgentView::installed(&agent, dirs, link, switch),
        None => AgentView::missing(detection.agent),
    }
}

fn view_with(
    id: AgentId,
    dirs: SkillDirs,
    link: Capability<()>,
    switch: Capability<()>,
) -> AgentView {
    AgentView::installed(&witness(id), dirs, link, switch)
}

/// An installed agent whose link and switch are measured on macOS.
fn view(id: AgentId, dirs: SkillDirs) -> AgentView {
    view_with(id, dirs, measured(), measured())
}

/// The user-level folders of DESIGN-C2 4.3.
fn global_dirs(id: AgentId) -> SkillDirs {
    let h = home();
    match id {
        AgentId::Claude => dirs(&h, ".claude/skills", &[]),
        AgentId::Codex => dirs(&h, ".agents/skills", &[".codex/skills"]),
        AgentId::Gemini => dirs(&h, ".gemini/skills", &[".agents/skills"]),
        AgentId::Copilot => dirs(&h, ".copilot/skills", &[".agents/skills"]),
        AgentId::Opencode => dirs(
            &h,
            ".config/opencode/skills",
            &[".claude/skills", ".agents/skills"],
        ),
        AgentId::Antigravity => dirs(
            &h,
            ".gemini/config/skills",
            &[".gemini/antigravity-cli/skills", ".gemini/skills"],
        ),
        AgentId::Cursor => dirs(
            &h,
            ".cursor/skills",
            &[".agents/skills", ".claude/skills", ".codex/skills"],
        ),
    }
}

/// The project folders of DESIGN-C2 4.3.
fn project_dirs(id: AgentId) -> SkillDirs {
    let r = repo();
    match id {
        AgentId::Claude => dirs(&r, ".claude/skills", &[]),
        AgentId::Codex => dirs(&r, ".agents/skills", &[]),
        AgentId::Gemini => dirs(&r, ".gemini/skills", &[".agents/skills"]),
        AgentId::Copilot => dirs(&r, ".github/skills", &[".claude/skills", ".agents/skills"]),
        AgentId::Opencode => dirs(
            &r,
            ".opencode/skills",
            &[".claude/skills", ".agents/skills"],
        ),
        AgentId::Antigravity => dirs(&r, ".agents/skills", &[]),
        AgentId::Cursor => dirs(
            &r,
            ".cursor/skills",
            &[".agents/skills", ".claude/skills", ".codex/skills"],
        ),
    }
}

/// Installed views of `ids`, each built from a detection witness, with the folders `layout` gives and the link
/// and switch capabilities `caps` gives.
fn installed_views(
    ids: &[AgentId],
    layout: impl Fn(AgentId) -> SkillDirs,
    caps: impl Fn(AgentId) -> (Capability<()>, Capability<()>),
) -> Vec<AgentView> {
    ids.iter()
        .map(|id| {
            let (link, switch) = caps(*id);
            view_with(*id, layout(*id), link, switch)
        })
        .collect()
}

/// The seven agents of DESIGN-C2 4.3 at user level.
fn global_views() -> Vec<AgentView> {
    installed_views(&AgentId::ALL, global_dirs, all_measured)
}

/// The seven agents of DESIGN-C2 4.3 at project level.
fn project_views() -> Vec<AgentView> {
    installed_views(&AgentId::ALL, project_dirs, all_measured)
}

fn global() -> Scope {
    Scope::Global { home: home() }
}

fn project() -> Scope {
    Scope::Project { repo: repo() }
}

fn request<'a>(
    skill: &'a str,
    scope: Scope,
    targets: &'a [AgentId],
    agents: &'a [AgentView],
) -> LinkPlanRequest<'a> {
    LinkPlanRequest {
        skill,
        scope,
        targets,
        agents,
        os: Os::Macos,
    }
}

/// The plan for `pdf-tools`, checked against what every plan keeps.
fn plan(scope: Scope, targets: &[AgentId], agents: &[AgentView]) -> Vec<LinkAction> {
    checked(&request(SKILL, scope, targets, agents))
}

fn position(id: AgentId) -> usize {
    AgentId::ALL.iter().position(|other| *other == id).unwrap()
}

fn reads(dirs: &SkillDirs, folder: &Path) -> bool {
    dirs.primary == folder || dirs.also_reads.iter().any(|read| read == folder)
}

fn plain(path: &Path) -> bool {
    path.is_absolute()
        && path
            .components()
            .all(|part| !matches!(part, Component::CurDir | Component::ParentDir))
}

/// Verified with complete measured or carried evidence that covers `os`: what an action may rely on (C8).
fn usable(capability: &Capability<()>, os: Os) -> bool {
    let Capability::Verified { evidence, .. } = capability else {
        return false;
    };
    let named = |text: &str| !text.trim().is_empty();
    let complete = named(evidence.source)
        && match evidence.basis {
            Basis::Measured => evidence.verified_on.is_some_and(named),
            Basis::Carried => true,
            Basis::Docs => false,
        };
    complete && evidence.os.contains(&os)
}

/// Runs the plan and checks what every plan keeps. Each agent has at most one action, in the fixed order, and every
/// target has one. Every link goes from the store to a plain absolute path in the agent's primary folder, inside the
/// project for a project plan. Every `via` names links of the same plan, in agent order. An action that relies on
/// the agent seeing the skill reads the store when its `via` is empty; otherwise it reads each link's folder and
/// not the store, and its link visibility is verified for this system. A non-target with one usable view that
/// reads the store always has an outcome without `via`.
fn checked(request: &LinkPlanRequest<'_>) -> Vec<LinkAction> {
    let actions = link_plan(request);
    let agents: Vec<AgentId> = actions.iter().map(LinkAction::agent).collect();
    assert!(
        agents
            .windows(2)
            .all(|pair| position(pair[0]) < position(pair[1])),
        "one action per agent, in agent order: {actions:?}"
    );
    assert!(
        request.targets.iter().all(|target| agents.contains(target)),
        "every target has an action: {actions:?}"
    );
    let (Scope::Global { home: root } | Scope::Project { repo: root }) = &request.scope;
    let Some(store_folder) = request.scope.store_folder() else {
        assert!(!plain(root), "a plain absolute root has a store folder");
        assert!(
            actions.iter().all(|action| matches!(
                action,
                LinkAction::Skipped { why, .. } if *why == UNSAFE_NAME || *why == UNSAFE_ROOT
            )),
            "nothing is planned on an unchecked root: {actions:?}"
        );
        return actions;
    };
    assert!(plain(root));
    assert_eq!(store_folder, under(root, ".agents/skills"));
    let the_view = |id: AgentId| {
        let mut views = request.agents.iter().filter(|view| view.id() == id);
        let view = views
            .next()
            .expect("an agent that sees the skill has a view");
        assert!(views.all(|other| other == view), "{id:?} has one view");
        assert!(view.is_installed(), "{id:?} is installed");
        view
    };
    let mut links = Vec::new();
    for action in &actions {
        let agent = action.agent();
        let targeted = request.targets.contains(&agent);
        let via = action.via();
        assert!(
            via.windows(2)
                .all(|pair| position(pair[0].agent) < position(pair[1].agent)),
            "{action:?}"
        );
        match action {
            LinkAction::Link {
                from, to, style, ..
            } => {
                let view = the_view(agent);
                assert!(targeted, "{action:?}");
                assert_eq!(from, &store_folder.join(request.skill));
                assert_eq!(to, &view.dirs().primary.join(request.skill));
                assert!(plain(from) && plain(to), "{action:?}");
                assert_eq!(*style, request.scope.link_style());
                if let Scope::Project { repo } = &request.scope {
                    assert!(to.starts_with(repo), "{action:?}");
                }
                links.push(PlannedLink {
                    agent,
                    to: to.clone(),
                });
            }
            LinkAction::Native { .. }
            | LinkAction::Switch { .. }
            | LinkAction::VisibleAnyway { .. } => {
                let view = the_view(agent);
                assert_eq!(
                    targeted,
                    matches!(action, LinkAction::Native { .. }),
                    "{action:?}"
                );
                if via.is_empty() {
                    assert!(
                        reads(view.dirs(), &store_folder),
                        "without via the agent reads the store: {action:?}"
                    );
                } else {
                    assert!(!reads(view.dirs(), &store_folder), "{action:?}");
                    assert!(
                        usable(view.link(), request.os),
                        "seeing a planned link needs verified link visibility: {action:?}"
                    );
                    for link in via {
                        assert!(reads(view.dirs(), link.to.parent().unwrap()), "{action:?}");
                    }
                }
            }
            LinkAction::Skipped { why, .. } => assert!(
                targeted || [NOT_VERIFIED, UNSAFE_DIRS, AMBIGUOUS].contains(why),
                "{action:?}"
            ),
        }
    }
    for action in &actions {
        for link in action.via() {
            assert!(
                links.contains(link),
                "via names a link of this plan: {action:?}"
            );
        }
    }
    let refused = actions
        .iter()
        .any(|action| matches!(action, LinkAction::Skipped { why, .. } if *why == UNSAFE_NAME));
    for id in AgentId::ALL {
        let mut views = request.agents.iter().filter(|view| view.id() == id);
        let Some(view) = views.next() else {
            continue;
        };
        let dirs = view.dirs();
        let admitted = match &request.scope {
            Scope::Global { .. } => true,
            Scope::Project { repo } => dirs.primary.starts_with(repo),
        };
        let direct = !refused
            && !request.targets.contains(&id)
            && views.all(|other| other == view)
            && view.is_installed()
            && plain(&dirs.primary)
            && dirs.also_reads.iter().all(|read| plain(read))
            && admitted
            && reads(dirs, &store_folder);
        if direct {
            assert!(
                matches!(
                    actions.iter().find(|action| action.agent() == id),
                    Some(LinkAction::Switch { via, .. } | LinkAction::VisibleAnyway { via, .. })
                        if via.is_empty()
                ),
                "a non-target that reads the store is never left out: {id:?} {actions:?}"
            );
        }
    }
    actions
}

fn action_of(actions: &[LinkAction], agent: AgentId) -> Option<&LinkAction> {
    actions.iter().find(|action| action.agent() == agent)
}

fn store(root: &Path) -> PathBuf {
    under(root, ".agents/skills").join(SKILL)
}

fn link(agent: AgentId, root: &Path, primary: &str, style: TargetStyle) -> LinkAction {
    LinkAction::Link {
        agent,
        from: store(root),
        to: under(root, primary).join(SKILL),
        style,
    }
}

fn planned(agent: AgentId, root: &Path, primary: &str) -> PlannedLink {
    PlannedLink {
        agent,
        to: under(root, primary).join(SKILL),
    }
}

fn native(agent: AgentId) -> LinkAction {
    LinkAction::Native {
        agent,
        via: Vec::new(),
    }
}

fn native_via(agent: AgentId, via: Vec<PlannedLink>) -> LinkAction {
    LinkAction::Native { agent, via }
}

fn switch(agent: AgentId) -> LinkAction {
    switch_via(agent, Vec::new())
}

fn switch_via(agent: AgentId, via: Vec<PlannedLink>) -> LinkAction {
    LinkAction::Switch {
        agent,
        op: SwitchOp::Disable,
        via,
    }
}

fn visible(agent: AgentId) -> LinkAction {
    visible_via(agent, Vec::new())
}

fn visible_via(agent: AgentId, via: Vec<PlannedLink>) -> LinkAction {
    LinkAction::VisibleAnyway { agent, via }
}

fn skipped(agent: AgentId, why: &'static str) -> LinkAction {
    LinkAction::Skipped { agent, why }
}

#[test]
fn global_plan_links_claude_reads_codex_natively_and_flags_cursor() {
    // The design example: targets Claude Code and Codex; Cursor installed and not a target; the rest absent.
    let views = installed_views(
        &[AgentId::Claude, AgentId::Codex, AgentId::Cursor],
        global_dirs,
        |_| (measured(), Capability::None),
    );

    let actions = plan(global(), &[AgentId::Claude, AgentId::Codex], &views);

    assert_eq!(
        actions,
        vec![
            link(
                AgentId::Claude,
                &home(),
                ".claude/skills",
                TargetStyle::Absolute
            ),
            native(AgentId::Codex),
            visible(AgentId::Cursor),
        ]
    );
}

#[test]
fn direct_readers_need_nothing_and_only_claude_and_antigravity_need_links() {
    let actions = plan(global(), &AgentId::ALL, &global_views());

    assert_eq!(
        actions,
        vec![
            link(
                AgentId::Claude,
                &home(),
                ".claude/skills",
                TargetStyle::Absolute
            ),
            native(AgentId::Codex),
            native(AgentId::Gemini),
            native(AgentId::Copilot),
            native(AgentId::Opencode),
            link(
                AgentId::Antigravity,
                &home(),
                ".gemini/config/skills",
                TargetStyle::Absolute
            ),
            native(AgentId::Cursor),
        ]
    );
}

#[test]
fn a_non_target_reading_the_store_gets_its_switch_or_stays_visible() {
    // Only Codex is a target. Gemini, Copilot, OpenCode and Cursor read the store folder too.
    let views = installed_views(&AgentId::ALL, global_dirs, |id| match id {
        AgentId::Opencode => (measured(), unknown()),
        AgentId::Cursor => (measured(), Capability::None),
        _ => all_measured(id),
    });

    let actions = plan(global(), &[AgentId::Codex], &views);

    assert_eq!(
        actions,
        vec![
            native(AgentId::Codex),
            switch(AgentId::Gemini),
            switch(AgentId::Copilot),
            visible(AgentId::Opencode),
            visible(AgentId::Cursor),
        ]
    );
    // Claude Code and Antigravity read no folder that holds the skill: nothing is planned for them.
    assert_eq!(action_of(&actions, AgentId::Claude), None);
    assert_eq!(action_of(&actions, AgentId::Antigravity), None);
}

#[test]
fn a_new_link_destination_is_visible_to_other_installed_readers() {
    // Claude Code is linked into ~/.claude/skills. This OpenCode reads only that folder, not the store, yet it
    // will see the new link, so it is not left out of the plan, and its switch names the link it depends on.
    let layout = |id| match id {
        AgentId::Opencode => dirs(&home(), ".config/opencode/skills", &[".claude/skills"]),
        _ => global_dirs(id),
    };
    let views = installed_views(&AgentId::ALL, layout, all_measured);

    let actions = plan(global(), &[AgentId::Claude], &views);

    assert_eq!(
        action_of(&actions, AgentId::Claude),
        Some(&link(
            AgentId::Claude,
            &home(),
            ".claude/skills",
            TargetStyle::Absolute
        ))
    );
    assert_eq!(
        action_of(&actions, AgentId::Opencode),
        Some(&switch_via(
            AgentId::Opencode,
            vec![planned(AgentId::Claude, &home(), ".claude/skills")]
        ))
    );
    // Without that link the same OpenCode view gets nothing.
    let none = plan(global(), &[AgentId::Codex], &views);
    assert_eq!(action_of(&none, AgentId::Opencode), None);
}

#[test]
fn a_target_that_sees_an_earlier_link_gets_no_second_copy() {
    // Gemini CLI reads the store natively, so it needs no link and Antigravity gets its own.
    let actions = plan(
        global(),
        &[AgentId::Gemini, AgentId::Antigravity],
        &global_views(),
    );
    assert_eq!(
        action_of(&actions, AgentId::Gemini),
        Some(&native(AgentId::Gemini))
    );
    assert_eq!(
        action_of(&actions, AgentId::Antigravity),
        Some(&link(
            AgentId::Antigravity,
            &home(),
            ".gemini/config/skills",
            TargetStyle::Absolute
        ))
    );

    // A Gemini CLI that does not read the store is linked into ~/.gemini/skills; the Antigravity CLI reads that
    // folder, so it needs no copy of its own and is reached through Gemini's link.
    let layout = |id| match id {
        AgentId::Gemini => dirs(&home(), ".gemini/skills", &[]),
        _ => global_dirs(id),
    };
    let views = installed_views(&AgentId::ALL, layout, all_measured);
    let actions = plan(global(), &[AgentId::Gemini, AgentId::Antigravity], &views);
    assert_eq!(
        action_of(&actions, AgentId::Gemini),
        Some(&link(
            AgentId::Gemini,
            &home(),
            ".gemini/skills",
            TargetStyle::Absolute
        ))
    );
    assert_eq!(
        action_of(&actions, AgentId::Antigravity),
        Some(&native_via(
            AgentId::Antigravity,
            vec![planned(AgentId::Gemini, &home(), ".gemini/skills")]
        ))
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
                &repo(),
                ".claude/skills",
                TargetStyle::Relative
            ),
            native(AgentId::Codex),
            switch(AgentId::Gemini),
            switch(AgentId::Copilot),
            switch(AgentId::Opencode),
            switch(AgentId::Antigravity),
            switch(AgentId::Cursor),
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
            &repo(),
            ".claude/skills",
            TargetStyle::Relative
        )
    );
    // Every other installed agent reads the project store, so each has an outcome that needs no link.
    assert_eq!(actions.len(), 7);
    assert!(actions[1..].iter().all(|action| action.via().is_empty()));
}

#[test]
fn unknown_link_capability_skips_and_never_links() {
    let views = installed_views(&AgentId::ALL, global_dirs, |id| match id {
        AgentId::Claude | AgentId::Antigravity => (unknown(), measured()),
        _ => all_measured(id),
    });

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
        let views = installed_views(&AgentId::ALL, global_dirs, |id| match id {
            AgentId::Claude => (capability.clone(), measured()),
            _ => all_measured(id),
        });
        let actions = plan(global(), &[AgentId::Claude], &views);
        assert_eq!(
            action_of(&actions, AgentId::Claude),
            Some(&skipped(AgentId::Claude, NOT_VERIFIED)),
            "{label}"
        );
    }

    // Carried and measured evidence for this system do authorize.
    for basis in [Basis::Carried, Basis::Measured] {
        let views = installed_views(&AgentId::ALL, global_dirs, |id| match id {
            AgentId::Claude => (verified(basis), measured()),
            _ => all_measured(id),
        });
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
        unknown(),
        Capability::None,
    ];
    for capability in cases {
        let views = installed_views(&AgentId::ALL, global_dirs, |id| match id {
            AgentId::Gemini => (measured(), capability.clone()),
            _ => all_measured(id),
        });
        let actions = plan(global(), &[AgentId::Codex], &views);
        assert_eq!(
            action_of(&actions, AgentId::Gemini),
            Some(&visible(AgentId::Gemini)),
            "{capability:?}"
        );
    }
}

#[test]
fn a_target_that_cannot_link_by_design_is_skipped_with_its_own_reason() {
    let views = installed_views(&AgentId::ALL, global_dirs, |id| match id {
        AgentId::Claude => (Capability::None, measured()),
        _ => all_measured(id),
    });

    let actions = plan(global(), &[AgentId::Claude], &views);

    assert_eq!(
        action_of(&actions, AgentId::Claude),
        Some(&skipped(AgentId::Claude, CANNOT_LINK))
    );
}

#[test]
fn missing_agents_get_no_link_switch_or_folder() {
    // Claude Code and Cursor are absent; Gemini CLI is in the input but not installed.
    let mut views = installed_views(
        &[
            AgentId::Codex,
            AgentId::Copilot,
            AgentId::Opencode,
            AgentId::Antigravity,
        ],
        global_dirs,
        all_measured,
    );
    views.push(AgentView::missing(AgentId::Gemini));

    let actions = plan(
        global(),
        &[AgentId::Claude, AgentId::Gemini, AgentId::Codex],
        &views,
    );

    assert_eq!(
        action_of(&actions, AgentId::Claude),
        Some(&skipped(AgentId::Claude, NOT_INSTALLED))
    );
    assert_eq!(
        action_of(&actions, AgentId::Gemini),
        Some(&skipped(AgentId::Gemini, NOT_INSTALLED))
    );
    assert_eq!(
        action_of(&actions, AgentId::Codex),
        Some(&native(AgentId::Codex))
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
    sorted.sort_by_key(|id| position(*id));
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
    assert!(!home().exists(), "the synthetic home must not be created");
    assert!(!repo().exists(), "the synthetic repo must not be created");
}

#[test]
fn guard_explorelink_c1_skill_names_follow_the_portable_component_rule() {
    // An ordinary folder name and other portable single names are planned.
    for name in ["pdf-tools", "skill_1", "a.b", "über-skill"] {
        let views = global_views();
        let actions = checked(&request(
            name,
            global(),
            &[AgentId::Claude, AgentId::Codex],
            &views,
        ));
        let expected = LinkAction::Link {
            agent: AgentId::Claude,
            from: under(&home(), ".agents/skills").join(name),
            to: under(&home(), ".claude/skills").join(name),
            style: TargetStyle::Absolute,
        };
        assert_eq!(actions[..2], [expected, native(AgentId::Codex)], "{name:?}");
    }
    // Names the portable rule refuses, and names of more than one part, skip every target and plan nothing else.
    let refused = [
        "",
        ".",
        "..",
        "a/b",
        "../escape",
        "a\\b",
        "nul\0",
        "CON",
        "nul.txt",
        "Aux",
        "com1.md",
        "LPT9",
        "conin$",
        "name.",
        "name ",
        ".git",
        ".Git",
        "a:b",
        "a*b",
        "a?b",
        "a|b",
        "a<b",
        "a>b",
        "a\"b",
        "tab\there",
        "del\u{7f}",
        "zero\u{200b}width",
        "replacement\u{fffd}",
        "\u{ff43}\u{ff4f}\u{ff4e}",
        "a\u{ff0f}b",
    ];
    for name in refused {
        let views = global_views();
        let actions = checked(&request(
            name,
            global(),
            &[AgentId::Claude, AgentId::Codex],
            &views,
        ));
        assert_eq!(
            actions,
            vec![
                skipped(AgentId::Claude, UNSAFE_NAME),
                skipped(AgentId::Codex, UNSAFE_NAME),
            ],
            "{name:?}"
        );
    }
}

#[test]
fn guard_explorelink_c2_only_the_store_is_read_directly_and_link_readers_name_their_link() {
    for (scope, root, opencode, copilot) in [
        (
            global(),
            home(),
            ".config/opencode/skills",
            ".copilot/skills",
        ),
        (project(), repo(), ".opencode/skills", ".github/skills"),
    ] {
        // Claude Code is linked. OpenCode reads only Claude's folder; Copilot also reads the store directly and its
        // own link visibility is unknown, which a direct reader never needs.
        let layout = |id| match id {
            AgentId::Claude => dirs(&root, ".claude/skills", &[]),
            AgentId::Opencode => dirs(&root, opencode, &[".claude/skills"]),
            _ => dirs(&root, copilot, &[".claude/skills", ".agents/skills"]),
        };
        let style = scope.link_style();
        let claude = planned(AgentId::Claude, &root, ".claude/skills");
        let incomplete = Capability::Verified {
            value: (),
            evidence: Evidence {
                verified_on: None,
                ..evidence(Basis::Measured, &[Os::Macos])
            },
        };
        let readability = [
            ("measured", measured(), true),
            ("carried", verified(Basis::Carried), true),
            ("unknown", unknown(), false),
            ("docs", verified(Basis::Docs), false),
            (
                "another system",
                Capability::Verified {
                    value: (),
                    evidence: evidence(Basis::Measured, &[Os::Linux]),
                },
                false,
            ),
            ("measured without a version", incomplete, false),
            ("cannot read links", Capability::None, false),
        ];
        for (label, link_visibility, confirmed) in readability {
            let views = installed_views(
                &[AgentId::Claude, AgentId::Copilot, AgentId::Opencode],
                layout,
                |id| match id {
                    AgentId::Opencode => (link_visibility.clone(), measured()),
                    AgentId::Copilot => (unknown(), measured()),
                    _ => all_measured(id),
                },
            );
            let none = matches!(link_visibility, Capability::None);

            // OpenCode as a non-target: switched through Claude's link only when it can be seen to read it.
            let actions = plan(scope.clone(), &[AgentId::Claude], &views);
            let opencode_outcome = if confirmed {
                Some(switch_via(AgentId::Opencode, vec![claude.clone()]))
            } else if none {
                None
            } else {
                Some(skipped(AgentId::Opencode, NOT_VERIFIED))
            };
            assert_eq!(
                action_of(&actions, AgentId::Opencode),
                opencode_outcome.as_ref(),
                "{label}"
            );
            assert_eq!(
                action_of(&actions, AgentId::Copilot),
                Some(&switch(AgentId::Copilot)),
                "{label}"
            );
            assert_eq!(
                action_of(&actions, AgentId::Claude),
                Some(&link(AgentId::Claude, &root, ".claude/skills", style)),
                "{label}"
            );

            // OpenCode as a target: reached through Claude's link, never a direct Native and never a second copy.
            let actions = plan(
                scope.clone(),
                &[AgentId::Claude, AgentId::Copilot, AgentId::Opencode],
                &views,
            );
            let opencode_outcome = if confirmed {
                native_via(AgentId::Opencode, vec![claude.clone()])
            } else if none {
                skipped(AgentId::Opencode, CANNOT_LINK)
            } else {
                skipped(AgentId::Opencode, NOT_VERIFIED)
            };
            assert_eq!(
                actions,
                vec![
                    link(AgentId::Claude, &root, ".claude/skills", style),
                    native(AgentId::Copilot),
                    opencode_outcome,
                ],
                "{label}"
            );
        }

        // A reader that sees the planned link and has no authorized switch stays visible through it.
        let views = installed_views(
            &[AgentId::Claude, AgentId::Opencode],
            layout,
            |id| match id {
                AgentId::Opencode => (measured(), unknown()),
                _ => all_measured(id),
            },
        );
        assert_eq!(
            action_of(
                &plan(scope.clone(), &[AgentId::Claude], &views),
                AgentId::Opencode
            ),
            Some(&visible_via(AgentId::Opencode, vec![claude.clone()]))
        );
    }

    // An agent that reads the folders of two planned links depends on either, whichever target comes first.
    let layout = |id| match id {
        AgentId::Claude => dirs(&home(), ".claude/skills", &[]),
        AgentId::Copilot => dirs(
            &home(),
            ".copilot/skills",
            &[".claude/skills", ".gemini/config/skills"],
        ),
        AgentId::Antigravity => dirs(&home(), ".gemini/config/skills", &[]),
        _ => dirs(
            &home(),
            ".cursor/skills",
            &[".claude/skills", ".gemini/config/skills"],
        ),
    };
    let views = installed_views(
        &[
            AgentId::Claude,
            AgentId::Copilot,
            AgentId::Antigravity,
            AgentId::Cursor,
        ],
        layout,
        all_measured,
    );
    let both = vec![
        planned(AgentId::Claude, &home(), ".claude/skills"),
        planned(AgentId::Antigravity, &home(), ".gemini/config/skills"),
    ];
    let actions = plan(
        global(),
        &[AgentId::Claude, AgentId::Copilot, AgentId::Antigravity],
        &views,
    );
    assert_eq!(
        actions,
        vec![
            link(
                AgentId::Claude,
                &home(),
                ".claude/skills",
                TargetStyle::Absolute
            ),
            native_via(AgentId::Copilot, both.clone()),
            link(
                AgentId::Antigravity,
                &home(),
                ".gemini/config/skills",
                TargetStyle::Absolute
            ),
            switch_via(AgentId::Cursor, both),
        ]
    );
}

#[test]
fn guard_explorelink_c3_installed_views_come_only_from_a_detection_witness() {
    // Detections that do not show the agent installed build no witness, so their views are missing ones.
    let shim = Resolution::UnsupportedShim {
        path: PathBuf::from("/npm/codex.cmd"),
        reason: String::from("no package"),
    };
    let failed = [
        detect(AgentId::Claude, &FakeProbe::new(Os::Macos), None),
        detect(
            AgentId::Codex,
            &FakeProbe::new(Os::Windows).resolves("codex", shim),
            None,
        ),
        detect(
            AgentId::Gemini,
            &FakeProbe::new(Os::Macos)
                .program("gemini", "/bin/gemini")
                .run_fails("/bin/gemini --version", RunError::TimedOut),
            None,
        ),
    ];
    assert!(
        failed
            .iter()
            .all(|detection| installed(detection).is_none())
    );
    let views: Vec<AgentView> = AgentId::ALL
        .iter()
        .map(|id| {
            let detection = failed
                .iter()
                .find(|detection| detection.agent == *id)
                .cloned()
                .unwrap_or_else(|| detect(*id, &machine(*id), None));
            view_from(&detection, global_dirs(*id), measured(), measured())
        })
        .collect();
    let installed_ids: Vec<AgentId> = views
        .iter()
        .filter(|view| view.is_installed())
        .map(AgentView::id)
        .collect();
    assert_eq!(
        installed_ids,
        [
            AgentId::Copilot,
            AgentId::Opencode,
            AgentId::Antigravity,
            AgentId::Cursor
        ]
    );

    // A missing target is skipped as not installed; a missing non-target gets nothing, whatever it would read.
    let actions = plan(
        global(),
        &[
            AgentId::Claude,
            AgentId::Codex,
            AgentId::Gemini,
            AgentId::Copilot,
        ],
        &views,
    );
    assert_eq!(
        actions,
        vec![
            skipped(AgentId::Claude, NOT_INSTALLED),
            skipped(AgentId::Codex, NOT_INSTALLED),
            skipped(AgentId::Gemini, NOT_INSTALLED),
            native(AgentId::Copilot),
            switch(AgentId::Opencode),
            switch(AgentId::Cursor),
        ]
    );
    let actions = plan(global(), &[AgentId::Copilot], &views);
    for id in [AgentId::Claude, AgentId::Codex, AgentId::Gemini] {
        assert_eq!(action_of(&actions, id), None, "{id:?}");
    }

    // The witness names the agent of an installed view.
    let claude = view(AgentId::Claude, global_dirs(AgentId::Claude));
    assert!(claude.is_installed());
    assert_eq!(claude.id(), AgentId::Claude);
    assert!(!AgentView::missing(AgentId::Claude).is_installed());
}

#[test]
fn guard_explorelink_c4_paths_are_checked_lexically_before_any_join() {
    // A scope folder that is not a plain absolute path has no store folder, skips every target and plans nothing
    // else.
    let relative = PathBuf::from("synthetic").join("home");
    let stepping = repo().join("..").join("home");
    for root in [PathBuf::new(), relative, stepping] {
        for scope in [
            Scope::Global { home: root.clone() },
            Scope::Project { repo: root.clone() },
        ] {
            assert_eq!(scope.store_folder(), None, "{root:?}");
            let actions = plan(scope, &[AgentId::Claude, AgentId::Codex], &global_views());
            assert_eq!(
                actions,
                vec![
                    skipped(AgentId::Claude, UNSAFE_ROOT),
                    skipped(AgentId::Codex, UNSAFE_ROOT),
                ],
                "{root:?}"
            );
        }
    }

    // Folders that are relative or step out with `..` make a view unusable: skipped as a target or not, and never
    // linked or switched, even when the rest of its folders read the store.
    let unusable = [
        SkillDirs {
            primary: PathBuf::from(".claude").join("skills"),
            also_reads: Vec::new(),
        },
        SkillDirs {
            primary: under(&home(), ".claude/skills"),
            also_reads: vec![PathBuf::from("skills")],
        },
        SkillDirs {
            primary: home().join("..").join("elsewhere").join("skills"),
            also_reads: Vec::new(),
        },
        SkillDirs {
            primary: under(&home(), ".cursor/skills"),
            also_reads: vec![
                under(&home(), ".agents/skills"),
                home().join("x").join("..").join(".claude").join("skills"),
            ],
        },
    ];
    for unusable in unusable {
        let views = installed_views(
            &AgentId::ALL,
            |id| match id {
                AgentId::Claude => unusable.clone(),
                _ => global_dirs(id),
            },
            all_measured,
        );
        let actions = plan(global(), &[AgentId::Claude, AgentId::Codex], &views);
        assert_eq!(
            action_of(&actions, AgentId::Claude),
            Some(&skipped(AgentId::Claude, UNSAFE_DIRS)),
            "{unusable:?}"
        );
        assert!(actions.iter().all(|action| action.via().is_empty()));

        let views = installed_views(
            &AgentId::ALL,
            |id| match id {
                AgentId::Cursor => unusable.clone(),
                _ => global_dirs(id),
            },
            all_measured,
        );
        let actions = plan(global(), &[AgentId::Codex], &views);
        assert_eq!(
            action_of(&actions, AgentId::Cursor),
            Some(&skipped(AgentId::Cursor, UNSAFE_DIRS)),
            "{unusable:?}"
        );
    }

    // A custom absolute agent folder outside the home folder is supported for a user install. In a project the
    // same folder is outside the project: it never receives a link, and an agent reading it sees nothing through it.
    let custom = synthetic("custom").join("claude").join("skills");
    let installed_with = |root: &Path, opencode: &str| {
        let layout = |id| match id {
            AgentId::Claude => SkillDirs {
                primary: custom.clone(),
                also_reads: Vec::new(),
            },
            AgentId::Opencode => SkillDirs {
                primary: under(root, opencode),
                also_reads: vec![custom.clone()],
            },
            _ => dirs(root, ".agents/skills", &[]),
        };
        installed_views(
            &[AgentId::Claude, AgentId::Codex, AgentId::Opencode],
            layout,
            all_measured,
        )
    };
    let views = installed_with(&home(), ".config/opencode/skills");
    let through = PlannedLink {
        agent: AgentId::Claude,
        to: custom.join(SKILL),
    };
    assert_eq!(
        plan(global(), &[AgentId::Claude, AgentId::Codex], &views),
        vec![
            LinkAction::Link {
                agent: AgentId::Claude,
                from: store(&home()),
                to: custom.join(SKILL),
                style: TargetStyle::Absolute,
            },
            native(AgentId::Codex),
            switch_via(AgentId::Opencode, vec![through]),
        ]
    );
    let views = installed_with(&repo(), ".opencode/skills");
    assert_eq!(
        plan(project(), &[AgentId::Claude, AgentId::Codex], &views),
        vec![
            skipped(AgentId::Claude, UNSAFE_DIRS),
            native(AgentId::Codex)
        ]
    );

    // A project primary folder that starts with the project but steps out with `..` is outside it too.
    let escaping = SkillDirs {
        primary: repo().join("..").join("elsewhere").join("skills"),
        also_reads: Vec::new(),
    };
    let views = installed_views(&[AgentId::Claude], |_| escaping.clone(), all_measured);
    assert_eq!(
        plan(project(), &[AgentId::Claude], &views),
        vec![skipped(AgentId::Claude, UNSAFE_DIRS)]
    );
}

#[test]
fn guard_explorelink_c5_ambiguous_views_and_incomplete_evidence_never_authorize() {
    // Two different views of one agent leave it skipped in either input order, and nothing links for it.
    let mut views = global_views();
    views.push(view(
        AgentId::Claude,
        dirs(&home(), ".claude/other-skills", &[]),
    ));
    let forward = plan(global(), &[AgentId::Claude, AgentId::Codex], &views);
    views.rotate_right(1);
    let backward = plan(global(), &[AgentId::Claude, AgentId::Codex], &views);
    assert_eq!(forward, backward);
    assert_eq!(
        action_of(&forward, AgentId::Claude),
        Some(&skipped(AgentId::Claude, AMBIGUOUS))
    );
    assert!(
        forward
            .iter()
            .all(|action| !matches!(action, LinkAction::Link { .. }))
    );

    // The same holds for a non-target whose views differ only in their evidence, and for a missing view beside an
    // installed one.
    let mut views = global_views();
    views.push(view_with(
        AgentId::Cursor,
        global_dirs(AgentId::Cursor),
        measured(),
        Capability::None,
    ));
    views.push(AgentView::missing(AgentId::Gemini));
    for _ in 0..2 {
        let actions = plan(global(), &[AgentId::Codex, AgentId::Gemini], &views);
        assert_eq!(
            action_of(&actions, AgentId::Cursor),
            Some(&skipped(AgentId::Cursor, AMBIGUOUS))
        );
        assert_eq!(
            action_of(&actions, AgentId::Gemini),
            Some(&skipped(AgentId::Gemini, AMBIGUOUS))
        );
        views.reverse();
    }

    // The same view given twice counts once.
    let mut twice = global_views();
    twice.extend(global_views());
    assert_eq!(
        plan(global(), &[AgentId::Claude, AgentId::Codex], &twice),
        plan(
            global(),
            &[AgentId::Claude, AgentId::Codex],
            &global_views()
        )
    );

    // Measured evidence needs its source, the version it saw and this system; carried evidence needs its source.
    let complete = evidence(Basis::Measured, &[Os::Macos]);
    let incomplete = [
        Evidence {
            source: "",
            ..complete
        },
        Evidence {
            source: "  ",
            ..complete
        },
        Evidence {
            verified_on: None,
            ..complete
        },
        Evidence {
            verified_on: Some(" "),
            ..complete
        },
        Evidence {
            os: &[Os::Linux, Os::Windows],
            ..complete
        },
        Evidence {
            source: "",
            ..evidence(Basis::Carried, &[Os::Macos])
        },
    ];
    let reader = |id| match id {
        AgentId::Opencode => dirs(&home(), ".config/opencode/skills", &[".claude/skills"]),
        _ => global_dirs(id),
    };
    for evidence in incomplete {
        let capability = Capability::Verified {
            value: (),
            evidence,
        };
        let views = installed_views(&AgentId::ALL, global_dirs, |id| match id {
            AgentId::Claude => (capability.clone(), measured()),
            AgentId::Gemini => (measured(), capability.clone()),
            _ => all_measured(id),
        });
        let actions = plan(global(), &[AgentId::Claude], &views);
        assert_eq!(
            action_of(&actions, AgentId::Claude),
            Some(&skipped(AgentId::Claude, NOT_VERIFIED)),
            "{evidence:?}"
        );
        assert_eq!(
            action_of(&actions, AgentId::Gemini),
            Some(&visible(AgentId::Gemini)),
            "{evidence:?}"
        );

        let views = installed_views(&AgentId::ALL, reader, |id| match id {
            AgentId::Opencode => (capability.clone(), measured()),
            _ => all_measured(id),
        });
        let actions = plan(global(), &[AgentId::Claude], &views);
        assert_eq!(
            action_of(&actions, AgentId::Opencode),
            Some(&skipped(AgentId::Opencode, NOT_VERIFIED)),
            "{evidence:?}"
        );
    }

    // The complete evidence authorizes the link, the switch and the sight of the planned link.
    let capability = Capability::Verified {
        value: (),
        evidence: complete,
    };
    let views = installed_views(&AgentId::ALL, reader, |_| {
        (capability.clone(), capability.clone())
    });
    let actions = plan(global(), &[AgentId::Claude], &views);
    assert!(matches!(
        action_of(&actions, AgentId::Claude),
        Some(LinkAction::Link { .. })
    ));
    assert_eq!(
        action_of(&actions, AgentId::Gemini),
        Some(&switch(AgentId::Gemini))
    );
    assert_eq!(
        action_of(&actions, AgentId::Opencode),
        Some(&switch_via(
            AgentId::Opencode,
            vec![planned(AgentId::Claude, &home(), ".claude/skills")]
        ))
    );
}
