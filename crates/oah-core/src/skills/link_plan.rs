// 링크 계획: 설치된 에이전트의 읽는 폴더와 능력 근거만 보고 스킬마다 Native·Link·Switch·VisibleAnyway·Skipped 를 정하는 순수 함수다.
//! The link plan (CONTRACTS C9, DESIGN-C2 5.3): a pure function from the skill, the scope, the target agents and
//! each agent's projected skill folders and capability evidence to at most one [`LinkAction`] per agent, in the
//! fixed agent order. It reads no disk and writes nothing; the installer that later runs the actions supplies the
//! views and reports each agent's outcome from the actions and the links it managed to make.
//!
//! Inputs are checked lexically before any path is joined:
//!
//! * The skill name is one folder name that the review's portable path rule accepts
//!   ([`crate::review::identity`]): no separator and no reserved, aliasing or unsafe name. The scope's folder is an
//!   absolute path without `.` or `..` parts. Otherwise every target is skipped and nothing else is planned.
//! * An agent has one view: the same view repeated counts once, two different views leave the agent ambiguous. An
//!   installed view's folders are absolute paths without `.` or `..` parts, and a project's primary folder lies
//!   inside the project; a global view may name a custom absolute folder anywhere. An ambiguous or unusable view is
//!   skipped with the reason, target or not, and its folders never hold the skill.
//!
//! Then, in agent order:
//!
//! 1. A target that reads the store folder needs nothing: [`LinkAction::Native`] with an empty `via`.
//! 2. Any other target needs its link capability authorized for this system (see below). That is C8's link
//!    visibility: whether the agent reads a linked skill folder. When the target reads the folder of a link this
//!    plan makes for an earlier target, it gets no second copy but a `Native` whose `via` names the planned links
//!    it sees the skill through; otherwise it is linked ([`LinkAction::Link`]). Without the capability it is
//!    [`LinkAction::Skipped`] with the reason.
//! 3. An installed agent that is not a target and reads the store folder gets its native switch when that switch
//!    is authorized, else [`LinkAction::VisibleAnyway`]. One that reads only folders of planned links gets the same,
//!    with those links as `via`, when its own link visibility is authorized; nothing when it cannot read links;
//!    and [`LinkAction::Skipped`] as not verified when that is not known.
//! 4. A target that is not installed is skipped; a non-target that is not installed gets no action.
//!
//! An action with a non-empty `via` holds only once one of those links exists: the installer reports the agent
//! reached, or writes its switch, only when it made one of them.
//!
//! A capability authorizes a write under an agent's home, or an inference from link visibility, only when it is
//! verified with complete measured or carried evidence that covers the running system (DECISIONS D-07, the evidence
//! rule of C8): it names its source, a measurement also the version it saw, and its systems include this one.
//! Documented evidence, evidence for another system, incomplete evidence and an unknown capability never do. The
//! downgrade after a newer major version stays where [`Facts::evaluate`](crate::agents::Facts::evaluate) applies it,
//! before a view is built.

use crate::agents::{Basis, Capability, Evidence, InstalledAgent, Os};
use crate::config::AgentId;
use crate::fsx::links::TargetStyle;
use crate::review::identity::portable_paths;
use std::path::{Component, Path, PathBuf};

/// Why a capability that is not verified for this system skips an agent.
const NOT_VERIFIED: &str = "not verified on this system";
const CANNOT_LINK: &str = "this agent cannot read linked skill folders";
const NOT_INSTALLED: &str = "not installed";
const UNSAFE_NAME: &str = "not a safe skill folder name";
const UNSAFE_ROOT: &str = "the install folder is not a plain absolute path";
const UNSAFE_DIRS: &str = "its skill folders are not plain absolute folders of this scope";
const AMBIGUOUS: &str = "two different views of this agent were given";

/// The scope of an install and the folder it is rooted in.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Scope {
    /// User level, rooted in the home folder.
    Global { home: PathBuf },
    /// This project only, rooted in the repository.
    Project { repo: PathBuf },
}

impl Scope {
    /// The store folder that holds every skill of this scope, `<root>/.agents/skills`, when the root is an absolute
    /// path without `.` or `..` parts; `None` otherwise, so no store path is built on an unchecked root.
    pub fn store_folder(&self) -> Option<PathBuf> {
        let root = self.root();
        plain_absolute(root).then(|| root.join(".agents").join("skills"))
    }

    /// Links from a user's agent folders name an absolute target; links inside a project a relative one, so a
    /// moved worktree keeps working.
    pub fn link_style(&self) -> TargetStyle {
        match self {
            Scope::Global { .. } => TargetStyle::Absolute,
            Scope::Project { .. } => TargetStyle::Relative,
        }
    }

    fn root(&self) -> &Path {
        let (Scope::Global { home: root } | Scope::Project { repo: root }) = self;
        root
    }

    /// Whether an agent's primary folder may receive a link: any folder for a user, one inside the project for a
    /// project. Both paths are plain absolute paths, so the prefix test is a containment test.
    fn admits(&self, primary: &Path) -> bool {
        match self {
            Scope::Global { .. } => true,
            Scope::Project { repo } => primary.starts_with(repo),
        }
    }
}

/// The folders an agent reads skills from, already projected for one scope: absolute paths, with the primary folder
/// inside the project for a project plan.
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

    fn plain(&self) -> bool {
        plain_absolute(&self.primary) && self.also_reads.iter().all(|read| plain_absolute(read))
    }
}

/// What the plan needs to know about one agent. Only a detection's [`InstalledAgent`] witness builds the view of
/// an installed agent ([`AgentView::installed`]); [`AgentView::missing`] builds the view of an agent that was not
/// detected. The fields are private, so no other value can say that an agent is installed:
///
/// ```compile_fail,E0451
/// # use oah_core::agents::Capability;
/// # use oah_core::config::AgentId;
/// # use oah_core::skills::{AgentView, SkillDirs};
/// let dirs = SkillDirs { primary: "/synthetic/skills".into(), also_reads: Vec::new() };
/// let view = AgentView { id: AgentId::Claude, installed: true, dirs, link: Capability::None, switch: Capability::None };
/// ```
///
/// ```compile_fail,E0616
/// # use oah_core::config::AgentId;
/// # use oah_core::skills::AgentView;
/// let mut view = AgentView::missing(AgentId::Claude);
/// view.installed = true;
/// ```
///
/// The same names build a view through its constructor:
///
/// ```
/// # use oah_core::agents::Capability;
/// # use oah_core::config::AgentId;
/// # use oah_core::skills::{AgentView, SkillDirs};
/// let dirs = SkillDirs { primary: "/synthetic/skills".into(), also_reads: Vec::new() };
/// let mut view = AgentView::missing(AgentId::Claude);
/// assert!(!view.is_installed() && view.link() == &Capability::None && view.dirs() != &dirs);
/// view = view.clone();
/// assert_eq!(view.id(), AgentId::Claude);
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentView {
    id: AgentId,
    installed: bool,
    dirs: SkillDirs,
    link: Capability<()>,
    switch: Capability<()>,
}

impl AgentView {
    /// The view of a detected agent. `link` and `switch` are the `skills_link` and `native_switch` capabilities
    /// as the facts file evaluates them for this system and the version the detection found.
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

    pub fn id(&self) -> AgentId {
        self.id
    }

    /// Whether a detection witness built the view.
    pub fn is_installed(&self) -> bool {
        self.installed
    }

    pub fn dirs(&self) -> &SkillDirs {
        &self.dirs
    }

    /// The `skills_link` capability: whether the agent reads a linked skill folder.
    pub fn link(&self) -> &Capability<()> {
        &self.link
    }

    /// The `native_switch` capability.
    pub fn switch(&self) -> &Capability<()> {
        &self.switch
    }
}

/// What to do to an agent's native switch.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SwitchOp {
    /// Turn the skill off for the agent.
    Disable,
}

/// A link this plan makes, named by an action that depends on it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlannedLink {
    /// The target the link is made for: the plan's [`LinkAction::Link`] for this agent creates it.
    pub agent: AgentId,
    /// The link, that action's `to`.
    pub to: PathBuf,
}

/// One planned action for one agent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LinkAction {
    /// A target sees the skill without a link of its own. With an empty `via` it reads the store folder itself.
    /// Otherwise it reads the folders of these links the plan makes for other targets, and it is reached once one
    /// of them exists.
    Native {
        agent: AgentId,
        via: Vec<PlannedLink>,
    },
    /// Link `to` (in the agent's folder) to the store folder `from`.
    Link {
        agent: AgentId,
        from: PathBuf,
        to: PathBuf,
        style: TargetStyle,
    },
    /// Write the native switch of a non-target agent that would otherwise see the skill: in the store folder when
    /// `via` is empty, else through one of these planned links, so only once one of them exists.
    Switch {
        agent: AgentId,
        op: SwitchOp,
        via: Vec<PlannedLink>,
    },
    /// A non-target agent sees the skill and OAH has no authorized switch for it: in the store folder when `via` is
    /// empty, else once one of these planned links exists.
    VisibleAnyway {
        agent: AgentId,
        via: Vec<PlannedLink>,
    },
    /// Nothing is done for the agent, and why: a target that is not installed, cannot be linked or was given unsafe
    /// inputs, or a non-target whose view is unusable or whose sight of a planned link is not verified.
    Skipped { agent: AgentId, why: &'static str },
}

impl LinkAction {
    /// The agent the action is for.
    pub fn agent(&self) -> AgentId {
        match self {
            LinkAction::Native { agent, .. }
            | LinkAction::Link { agent, .. }
            | LinkAction::Switch { agent, .. }
            | LinkAction::VisibleAnyway { agent, .. }
            | LinkAction::Skipped { agent, .. } => *agent,
        }
    }

    /// The planned links the action depends on, in agent order; any one of them is enough. Empty when it depends on
    /// none.
    pub fn via(&self) -> &[PlannedLink] {
        match self {
            LinkAction::Native { via, .. }
            | LinkAction::Switch { via, .. }
            | LinkAction::VisibleAnyway { via, .. } => via,
            LinkAction::Link { .. } | LinkAction::Skipped { .. } => &[],
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

/// Plans the links, native switches and notes for one skill. At most one action per agent, in the fixed agent
/// order; a repeated target counts once.
pub fn link_plan(request: &LinkPlanRequest<'_>) -> Vec<LinkAction> {
    let targeted = |id: AgentId| request.targets.contains(&id);
    let checked = if skill_folder_name(request.skill) {
        request.scope.store_folder().ok_or(UNSAFE_ROOT)
    } else {
        Err(UNSAFE_NAME)
    };
    let store_folder = match checked {
        Ok(folder) => folder,
        Err(why) => {
            return AgentId::ALL
                .into_iter()
                .filter(|id| targeted(*id))
                .map(|agent| LinkAction::Skipped { agent, why })
                .collect();
        }
    };
    let store = store_folder.join(request.skill);
    let style = request.scope.link_style();
    let seen = |id: AgentId| view_of(request.agents, id, &request.scope);

    // The planned links in agent order, each with the folder it is made in. A target that will see an earlier one
    // waits for the whole list, so its `via` names every planned link it reads.
    let mut links: Vec<(&Path, PlannedLink)> = Vec::new();
    let mut decided: Vec<(AgentId, Decision<'_>)> = Vec::new();
    for id in AgentId::ALL.into_iter().filter(|id| targeted(*id)) {
        let skipped = |why| Decision::Done(LinkAction::Skipped { agent: id, why });
        let decision = match seen(id) {
            Seen::Missing => skipped(NOT_INSTALLED),
            Seen::Refused(why) => skipped(why),
            Seen::Installed(view) if view.dirs.reads(&store_folder) => {
                Decision::Done(LinkAction::Native {
                    agent: id,
                    via: Vec::new(),
                })
            }
            Seen::Installed(view) => match authorize(&view.link, request.os) {
                Err(_) if matches!(view.link, Capability::None) => skipped(CANNOT_LINK),
                Err(why) => skipped(why),
                Ok(()) if links.iter().any(|(folder, _)| view.dirs.reads(folder)) => {
                    Decision::SeesLink(view)
                }
                Ok(()) => {
                    let to = view.dirs.primary.join(request.skill);
                    links.push((
                        &view.dirs.primary,
                        PlannedLink {
                            agent: id,
                            to: to.clone(),
                        },
                    ));
                    Decision::Done(LinkAction::Link {
                        agent: id,
                        from: store.clone(),
                        to,
                        style,
                    })
                }
            },
        };
        decided.push((id, decision));
    }
    let via = |view: &AgentView| -> Vec<PlannedLink> {
        links
            .iter()
            .filter(|(folder, _)| view.dirs.reads(folder))
            .map(|(_, link)| link.clone())
            .collect()
    };

    AgentId::ALL
        .into_iter()
        .filter_map(|id| {
            if let Some((_, decision)) = decided.iter().find(|(agent, _)| *agent == id) {
                return Some(match decision {
                    Decision::Done(action) => action.clone(),
                    Decision::SeesLink(view) => LinkAction::Native {
                        agent: id,
                        via: via(view),
                    },
                });
            }
            let view = match seen(id) {
                Seen::Missing => return None,
                Seen::Refused(why) => return Some(LinkAction::Skipped { agent: id, why }),
                Seen::Installed(view) => view,
            };
            let through = if view.dirs.reads(&store_folder) {
                Vec::new()
            } else {
                let through = via(view);
                if through.is_empty() {
                    return None;
                }
                match authorize(&view.link, request.os) {
                    Ok(()) => through,
                    Err(_) if matches!(view.link, Capability::None) => return None,
                    Err(why) => return Some(LinkAction::Skipped { agent: id, why }),
                }
            };
            Some(match authorize(&view.switch, request.os) {
                Ok(()) => LinkAction::Switch {
                    agent: id,
                    op: SwitchOp::Disable,
                    via: through,
                },
                Err(_) => LinkAction::VisibleAnyway {
                    agent: id,
                    via: through,
                },
            })
        })
        .collect()
}

/// What a target gets: its action, or a `Native` through planned links once they are all known.
enum Decision<'a> {
    Done(LinkAction),
    SeesLink(&'a AgentView),
}

/// What the plan may rely on for one agent.
enum Seen<'a> {
    /// No view, or the view of an agent that is not installed.
    Missing,
    /// The one view of an installed agent, with folders the scope admits.
    Installed(&'a AgentView),
    /// Two different views, or an installed view with unusable folders.
    Refused(&'static str),
}

/// The one view of `id`. Repeats of the same view count once whatever their order; two different views are refused
/// whatever their order, and so is an installed view whose folders are not plain absolute folders of the scope.
fn view_of<'a>(agents: &'a [AgentView], id: AgentId, scope: &Scope) -> Seen<'a> {
    let mut views = agents.iter().filter(|view| view.id == id);
    let Some(view) = views.next() else {
        return Seen::Missing;
    };
    if views.any(|other| other != view) {
        return Seen::Refused(AMBIGUOUS);
    }
    if !view.installed {
        return Seen::Missing;
    }
    if !(view.dirs.plain() && scope.admits(&view.dirs.primary)) {
        return Seen::Refused(UNSAFE_DIRS);
    }
    Seen::Installed(view)
}

/// Whether `capability` may be used for a write under an agent's home on `os`, or to infer what a linked folder
/// shows it: verified with complete measured or carried evidence that covers `os`. The error is the reason a
/// skipped agent shows.
fn authorize(capability: &Capability<()>, os: Os) -> Result<(), &'static str> {
    match capability {
        Capability::Verified { evidence, .. }
            if complete(evidence) && evidence.os.contains(&os) =>
        {
            Ok(())
        }
        _ => Err(NOT_VERIFIED),
    }
}

/// Measured or carried evidence that names its source, and for a measurement the version it saw.
fn complete(evidence: &Evidence) -> bool {
    let named = |text: &str| !text.trim().is_empty();
    named(evidence.source)
        && match evidence.basis {
            Basis::Measured => evidence.verified_on.is_some_and(named),
            Basis::Carried => true,
            Basis::Docs => false,
        }
}

/// A skill name is one folder name that the review's portable path rule accepts. The rule splits at `/`, so a name
/// with one is refused first.
fn skill_folder_name(name: &str) -> bool {
    !name.contains('/') && portable_paths(&[name.to_owned()]).is_ok()
}

/// An absolute path without `.` or `..` parts, so comparing and prefix-testing it is sound without the disk.
fn plain_absolute(path: &Path) -> bool {
    path.is_absolute()
        && path
            .components()
            .all(|part| !matches!(part, Component::CurDir | Component::ParentDir))
}

#[cfg(test)]
mod tests {
    use super::skill_folder_name;
    use crate::review::identity::portable_paths;

    /// Names the old planner rule accepted that the portable rule refuses, names both accept and names both refuse.
    const NAMES: [&str; 44] = [
        "pdf-tools",
        "skill_1",
        "a.b",
        "x..y",
        "über-skill",
        "技能",
        "CON",
        "con",
        "Nul.txt",
        "aux.md",
        "COM1",
        "lpt9.log",
        "conin$",
        "CONOUT$.json",
        "name.",
        "name ",
        ".git",
        ".GIT",
        "a:b",
        "a*b",
        "a?b",
        "a|b",
        "a<b",
        "a>b",
        "a\"b",
        "tab\there",
        "bell\u{7}",
        "del\u{7f}",
        "zero\u{200b}width",
        "bom\u{feff}",
        "replacement\u{fffd}",
        "\u{ff43}\u{ff4f}\u{ff4e}",
        "\u{ff23}\u{ff2f}\u{ff2d}\u{ff11}",
        "a\u{ff0f}b",
        "dot\u{2024}",
        "",
        ".",
        "..",
        "a/b",
        "../escape",
        "a\\b",
        "nul\0",
        "/abs",
        "trailing/",
    ];

    #[test]
    fn guard_explorelink_c1_the_name_rule_is_the_review_portable_component_rule() {
        for name in NAMES {
            let canonical = !name.contains('/') && portable_paths(&[name.to_owned()]).is_ok();
            assert_eq!(skill_folder_name(name), canonical, "{name:?}");
        }
        // The corpus reaches both sides of the rule, and the names the old rule let through.
        let accepted = NAMES.iter().filter(|name| skill_folder_name(name)).count();
        assert_eq!(
            accepted, 6,
            "only the first six names are portable single folder names"
        );
        assert!(NAMES[..6].iter().all(|name| skill_folder_name(name)));
    }
}
