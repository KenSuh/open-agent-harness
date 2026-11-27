// oah-core 라이브러리: 모든 판단과 파일 형식이 놓일 자리다.
//! Home of every decision and file format of OAH. The CLI and the desktop app stay thin on top of it.
//!
//! * [`paths`] finds the user's OAH folder, a project's root and its per-machine state folder, creating nothing.
//! * [`fsx`] holds the file-system primitives every writer shares: atomic writes, short per-file locks, managed
//!   links and the self-write registry.
//! * [`config`] reads, interprets and format-preservingly saves `.oah/config.toml` and `OAH_HOME/settings.toml`.
//! * [`error`] is the one error type operations return and its serialized form; [`dto`] holds the other
//!   serialized shapes shared by the CLI and the desktop app.
//! * [`tasks`] runs long operations: ids, states, progress, cooperative cancellation and the registry.
//! * [`plan`] is the one apply engine for change plans: preflight, then one target at a time.
//! * [`project`] opens a folder read-only; [`app`] holds the session and the operations the shell calls.
//! * [`agents`] detects the seven agents and their sign-in through one probe seam, with capability evidence.
//! * [`rules`] plans and writes the managed block of `AGENTS.md` and the thin files; [`workflow`] is the Team
//!   Workflow: one assignment operation (config, then rules), the single banner, presets and the rules check.
//! * [`net`] is the one bounded HTTPS client: the rows of the network table, typed failures, the real and the
//!   recording transport.
//! * [`review`] is the skill review and license core with its store of reviews and snapshots; [`registry`] holds
//!   the collectors it reads a source with (the HTTPS GitHub one and the bounded local Git one); [`skills`]
//!   computes the public `computedHash` of a skill's files.

// The library under its own name, so a module that the review-core test harness also mounts by path can name the
// shared file primitives as `oah_core::fsx`: in that harness the name is this library, here it is this crate.
extern crate self as oah_core;

pub mod agents;
pub mod app;
pub mod config;
pub mod dto;
pub mod error;
pub mod fsx;
pub mod net;
pub mod paths;
pub mod plan;
mod proc;
pub mod project;
pub mod project_tools;
pub mod registry;
pub mod review;
pub mod rules;
pub mod skills;
pub mod tasks;
pub mod workflow;

pub use error::{Coded, Error, ErrorDto, hint_for};

/// The version every OAH program reports, taken from the workspace.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(test)]
mod tests {
    use super::VERSION;

    #[test]
    fn version_is_the_workspace_version() {
        assert_eq!(VERSION, "1.2.1");
    }
}
