// oah-core 라이브러리: 모든 판단과 파일 형식이 놓일 자리다.
//! Home of every decision and file format of OAH. The CLI and the desktop app stay thin on top of it.
//!
//! * [`paths`] finds the user's OAH folder, a project's root and its per-machine state folder, creating nothing.
//! * [`fsx`] holds the file-system primitives every writer shares: atomic writes, short per-file locks, managed
//!   links and the self-write registry.
//! * [`config`] reads, interprets and format-preservingly saves `.oah/config.toml` and `OAH_HOME/settings.toml`.

pub mod config;
pub mod fsx;
pub mod paths;

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
