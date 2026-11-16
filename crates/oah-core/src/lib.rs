// oah-core 라이브러리: 모든 판단과 파일 형식이 놓일 자리다.
//! Home of every decision and file format of OAH. The CLI and the desktop app stay thin on top of it.

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
