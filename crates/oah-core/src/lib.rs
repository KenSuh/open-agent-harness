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

pub mod agents;
pub mod app;
pub mod config;
pub mod dto;
pub mod error;
pub mod fsx;
pub mod paths;
pub mod plan;
mod proc;
pub mod project;
pub mod rules;
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
        assert_eq!(VERSION, "0.0.0");
    }
}
