// 설정 오류와 알림: 안정된 코드와 사람이 읽을 메시지를 나누고, 치명적이지 않은 알림은 따로 모은다.
use crate::fsx::FsxError;
use std::fmt;

/// Error codes this module returns.
pub mod codes {
    /// The file cannot be read, is not UTF-8 or TOML, or a value has the wrong type or an unusable value.
    pub const INVALID: &str = "OAH_CONFIG_INVALID";
    /// A stage names Cursor, which receives skills and rules but cannot own a section.
    pub const AGENT_DEPLOY_ONLY: &str = "OAH_AGENT_DEPLOY_ONLY";
    /// A save of a v0.4.0 file was attempted without the upgrade approval.
    pub const UPGRADE_REQUIRED: &str = "OAH_CONFIG_UPGRADE_REQUIRED";
    /// The file changed between the read a save started from and the save.
    pub const CHANGED: &str = "OAH_CONFIG_CHANGED";
    /// `.oah/config.toml.v0.4.bak` exists and is not the file being upgraded.
    pub const BACKUP_CONFLICT: &str = "OAH_CONFIG_BACKUP_CONFLICT";
    /// The edit cannot be made while keeping every other byte of the file (for example, a key written in an
    /// inline table or as a dotted key).
    pub const LAYOUT_UNSUPPORTED: &str = "OAH_CONFIG_LAYOUT_UNSUPPORTED";
    /// An edit names a key outside the tables OAH writes.
    pub const KEY_NOT_OWNED: &str = "OAH_CONFIG_KEY_NOT_OWNED";
    /// The approved project-tools plan is not the plan the project and its file give now.
    pub const PLAN_STALE: &str = crate::error::codes::PLAN_STALE;
    /// The folder of a project-tools change is not the project the change was made for.
    pub const PROJECT_CHANGED: &str = crate::error::codes::PROJECT_CHANGED;
}

/// Notice codes: reported, never fatal.
pub mod notice_codes {
    /// A key inside a table OAH reads that OAH does not know; it is kept in the file and not used.
    pub const UNKNOWN_KEY: &str = "OAH_CONFIG_UNKNOWN_KEY";
    /// A `[stages]` key or `[sections.<id>]` table whose id is not in `sections.order`; kept and not used.
    pub const SECTION_UNLISTED: &str = "OAH_SECTION_UNLISTED";
    /// v0.4.0 `[stages]` keys ignored because the file also has 1.0 keys.
    pub const STAGES_LEGACY_IGNORED: &str = "OAH_STAGES_LEGACY_IGNORED";
    /// A project-tools value that only this machine can use (an absolute path, a drive, `..`, a backslash); it is
    /// read as written.
    pub const NOT_PORTABLE: &str = "OAH_CONFIG_NOT_PORTABLE";
}

/// A configuration failure: a stable code and a message that names the file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError {
    code: &'static str,
    message: String,
}

impl ConfigError {
    pub(crate) fn new(code: &'static str, message: impl Into<String>) -> Self {
        ConfigError {
            code,
            message: message.into(),
        }
    }

    pub(crate) fn invalid(message: impl Into<String>) -> Self {
        ConfigError::new(codes::INVALID, message)
    }

    pub fn code(&self) -> &'static str {
        self.code
    }

    /// The message without the code.
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ConfigError {}

impl From<FsxError> for ConfigError {
    fn from(error: FsxError) -> Self {
        let text = error.to_string();
        let message = text
            .strip_prefix(error.code())
            .and_then(|rest| rest.strip_prefix(": "))
            .unwrap_or(&text)
            .to_string();
        ConfigError::new(error.code(), message)
    }
}

/// A non-fatal finding while reading: the file is still used.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notice {
    code: &'static str,
    message: String,
    keys: Vec<String>,
}

impl Notice {
    pub(crate) fn new(code: &'static str, message: impl Into<String>, keys: Vec<String>) -> Self {
        Notice {
            code,
            message: message.into(),
            keys,
        }
    }

    pub fn code(&self) -> &'static str {
        self.code
    }

    /// The message, naming the file.
    pub fn message(&self) -> &str {
        &self.message
    }

    /// The dotted keys the notice is about.
    pub fn keys(&self) -> &[String] {
        &self.keys
    }
}

impl fmt::Display for Notice {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}
