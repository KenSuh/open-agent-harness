// 링크 계획: 설치된 에이전트의 읽는 폴더와 능력 근거만 보고 스킬마다 Native·Link·Switch·VisibleAnyway·Skipped 를 정하는 순수 함수다.
//! The link plan (CONTRACTS C9, DESIGN-C2 5.3): a pure function from the skill, the scope, the target agents and
//! each agent's projected skill folders and capability evidence to one [`LinkAction`] per agent, in the fixed agent
//! order. It reads no disk and writes nothing; the installer that later runs the actions supplies the views.
//!
//! Rules, in order:
//!
//! 1. A target that already reads a folder holding the skill needs nothing ([`LinkAction::Native`]). The folders
//!    holding it are the store and the destination of every link this plan already makes for an earlier target, so
//!    a second copy is never planned for an agent that sees one.
//! 2. Any other target is linked ([`LinkAction::Link`]) only when its link capability is authorized for this
//!    system (see below); otherwise it is [`LinkAction::Skipped`] with the reason.
//! 3. An installed agent that is not a target but reads a folder holding the skill (the store or a new link's
//!    folder) gets its native switch when that switch is authorized, else [`LinkAction::VisibleAnyway`].
//! 4. A target that is not installed is skipped; a non-target that is not installed gets no action. An unsafe
//!    skill name skips every target.
//!
//! A capability authorizes a write under an agent's home only when it is verified with measured or carried
//! evidence that covers the running system (DECISIONS D-07, the evidence rule of C8). Documented evidence,
//! evidence for another system and an unknown capability never do.

use crate::agents::{Basis, Capability, InstalledAgent, Os};
use crate::config::AgentId;
use crate::fsx::links::TargetStyle;
use std::path::{Path, PathBuf};

/// Why a capability that is not verified for this system skips a link.
const NOT_VERIFIED: &str = "not verified on this system";
const CANNOT_LINK: &str = "this agent cannot read linked skill folders";
const NOT_INSTALLED: &str = "not installed";
const UNSAFE_NAME: &str = "not a safe skill folder name";

/// The scope of an install and the folder it is rooted in.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Scope {
    /// User level, rooted in the home folder.
    Global { home: PathBuf },
    /// This project only, rooted in the repository.
    Project { repo: PathBuf },
}

impl Scope {
    /// The store folder that holds every skill of this scope: `<root>/.agents/skills`.
    pub fn store_folder(&self) -> PathBuf {
        let (Scope::Global { home: root } | Scope::Project { repo: root }) = self;
        root.join(".agents").join("skills")
    }

    /// Links from a user's agent folders name an absolute target; links inside a project a relative one, so a
    /// moved worktree keeps working.
    pub fn link_style(&self) -> TargetStyle {
        match self {
            Scope::Global { .. } => TargetStyle::Absolute,
            Scope::Project { .. } => TargetStyle::Relative,
        }
    }
}

/// The folders an agent reads skills from, already projected for one scope: absolute paths under the home folder
/// for a global plan, or under the project for a project plan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkillDirs {
    /// The folder OAH links into when the agent does not read the store.
    pub primary: PathBuf,
    /// Other folders the agent reads.
    pub also_reads: Vec<PathBuf>,
}

impl SkillDirs {
    fn reads(&self, folder: &Path) -> bool {
        self.primary == folder || self.also_reads.iter().any(|read| read == folder)
    }
}

/// What the plan needs to know about one agent. Build it from a detection's [`InstalledAgent`] witness with
/// [`AgentView::installed`], or with [`AgentView::missing`] for an agent that was not detected.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentView {
    pub id: AgentId,
    pub installed: bool,
    pub dirs: SkillDirs,
    /// The `skills_link` capability as the facts file evaluates it.
    pub link: Capability<()>,
    /// The `native_switch` capability as the facts file evaluates it.
    pub switch: Capability<()>,
}

impl AgentView {
    /// The view of a detected agent.
    pub fn installed(
        agent: &InstalledAgent,
        dirs: SkillDirs,
        link: Capability<()>,
        switch: Capability<()>,
    ) -> AgentView {
        AgentView {
            id: agent.id(),
            installed: true,
            dirs,
            link,
            switch,
        }
    }

    /// The view of an agent that is not installed: no folder, no capability.
    pub fn missing(id: AgentId) -> AgentView {
        AgentView {
            id,
            installed: false,
            dirs: SkillDirs {
                primary: PathBuf::new(),
                also_reads: Vec::new(),
            },
            link: Capability::None,
            switch: Capability::None,
        }
    }
}

/// What to do to an agent's native switch.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SwitchOp {
    /// Turn the skill off for the agent.
    Disable,
}

/// One planned action for one agent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LinkAction {
    /// The agent already reads a folder holding the skill.
    Native { agent: AgentId },
    /// Link `to` (in the agent's folder) to the store folder `from`.
    Link {
        agent: AgentId,
        from: PathBuf,
        to: PathBuf,
        style: TargetStyle,
    },
    /// Write the agent's native switch for a non-target agent that would otherwise see the skill.
    Switch { agent: AgentId, op: SwitchOp },
    /// A non-target agent sees the skill and OAH has no authorized switch for it.
    VisibleAnyway { agent: AgentId },
    /// Nothing is done for a target, and why.
    Skipped { agent: AgentId, why: &'static str },
}

impl LinkAction {
    /// The agent the action is for.
    pub fn agent(&self) -> AgentId {
        match self {
            LinkAction::Native { agent }
            | LinkAction::Link { agent, .. }
            | LinkAction::Switch { agent, .. }
            | LinkAction::VisibleAnyway { agent }
            | LinkAction::Skipped { agent, .. } => *agent,
        }
    }
}

/// What a plan is asked for.
#[derive(Debug, Clone)]
pub struct LinkPlanRequest<'a> {
    /// The skill's folder name.
    pub skill: &'a str,
    pub scope: Scope,
    pub targets: &'a [AgentId],
    pub agents: &'a [AgentView],
    /// The running system, which evidence must cover.
    pub os: Os,
}

/// Plans the links, native switches and notes for one skill. One action per agent that has one, in the fixed
/// agent order; a repeated target counts once.
pub fn link_plan(request: &LinkPlanRequest<'_>) -> Vec<LinkAction> {
    let targets = |id: AgentId| request.targets.contains(&id);
    if !safe_name(request.skill) {
        return AgentId::ALL
            .into_iter()
            .filter(|id| targets(*id))
            .map(|agent| LinkAction::Skipped {
                agent,
                why: UNSAFE_NAME,
            })
            .collect();
    }
    let store_folder = request.scope.store_folder();
    let store = store_folder.join(request.skill);
    let style = request.scope.link_style();
    let view_of = |id: AgentId| request.agents.iter().find(|view| view.id == id);

    // Folders that hold the skill once the plan is applied: the store, then the folder of each planned link.
    let mut holding: Vec<PathBuf> = vec![store_folder];
    let mut decided: Vec<LinkAction> = Vec::new();
    for id in AgentId::ALL.into_iter().filter(|id| targets(*id)) {
        let Some(view) = view_of(id).filter(|view| view.installed) else {
            decided.push(LinkAction::Skipped {
                agent: id,
                why: NOT_INSTALLED,
            });
            continue;
        };
        if holding.iter().any(|folder| view.dirs.reads(folder)) {
            decided.push(LinkAction::Native { agent: id });
            continue;
        }
        match authorize(&view.link, request.os) {
            Ok(()) => {
                holding.push(view.dirs.primary.clone());
                decided.push(LinkAction::Link {
                    agent: id,
                    from: store.clone(),
                    to: view.dirs.primary.join(request.skill),
                    style,
                });
            }
            Err(why) => decided.push(LinkAction::Skipped {
                agent: id,
                why: if matches!(view.link, Capability::None) {
                    CANNOT_LINK
                } else {
                    why
                },
            }),
        }
    }

    AgentId::ALL
        .into_iter()
        .filter_map(|id| {
            if targets(id) {
                return decided.iter().find(|action| action.agent() == id).cloned();
            }
            let view = view_of(id).filter(|view| view.installed)?;
            if !holding.iter().any(|folder| view.dirs.reads(folder)) {
                return None;
            }
            Some(match authorize(&view.switch, request.os) {
                Ok(()) => LinkAction::Switch {
                    agent: id,
                    op: SwitchOp::Disable,
                },
                Err(_) => LinkAction::VisibleAnyway { agent: id },
            })
        })
        .collect()
}

/// Whether `capability` may be used for a write under an agent's home on `os`: verified, measured or carried, and
/// covering `os`. The error is the reason a skipped link shows.
fn authorize(capability: &Capability<()>, os: Os) -> Result<(), &'static str> {
    match capability {
        Capability::Verified { evidence, .. }
            if matches!(evidence.basis, Basis::Measured | Basis::Carried)
                && evidence.os.contains(&os) =>
        {
            Ok(())
        }
        _ => Err(NOT_VERIFIED),
    }
}

/// A skill name is one plain folder name.
fn safe_name(name: &str) -> bool {
    !name.is_empty() && name != "." && name != ".." && !name.contains(['/', '\\', '\0'])
}
