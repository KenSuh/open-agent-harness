// 앱 DTO: 앱 정보·CLI 복사 상태·사용자 설정·새로고침 결과와 설정 변경 요청의 모양을 둔다.
use super::NoticeDto;
use serde::{Deserialize, Serialize};

/// The response of `app_info`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AppInfoDto {
    pub version: String,
    /// `macos`, `windows` or `linux`.
    pub os: String,
    pub oah_home: String,
    /// The CLI copy of this start; `null` until the shell has run [`crate::app::cli_copy`].
    pub cli: Option<CliDto>,
    /// Notices raised while the app started (the CLI copy), so a window that loads later still sees them.
    pub notices: Vec<NoticeDto>,
}

/// `OAH_HOME/bin/oah` as the start left it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CliDto {
    pub state: CliState,
    pub path: String,
    /// The version the bundled bytes printed for `--version` when run from a private copy outside `OAH_HOME`
    /// this start (`copied` and `unchanged`); `null` when none ran. The installed copy itself is never run.
    pub version: Option<String>,
}

/// What the CLI copy did (CONTRACTS C7). `copied` and `unchanged` mean that the installed bytes, read without
/// following the final link, equal the bundled bytes, and that those bytes printed `oah <version>` when run from a
/// private copy in a new folder inside the operating system's per-user location.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum CliState {
    /// The checked bundled bytes were published by a rename and read back equal.
    Copied,
    /// The copy already had the bundled bytes, and on Unix its mode lets this user run it; nothing was written.
    /// The installed copy was not run, so a policy that applies only to its path (a `noexec` mount, access
    /// control lists or mandatory access control, quarantine, or AppLocker on Windows) is not observed.
    Unchanged,
    /// A step before publishing failed, the private check included; the previous copy, or none, is in place.
    KeptPrevious,
    /// A step after the rename failed; the copy may hold the old or the new bytes.
    Unknown,
}

/// The response of `settings_get` and `settings_set`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SettingsDto {
    pub automation_level: String,
    pub ui: UiDto,
    pub wiki_index: crate::config::WikiIndexDevice,
}

/// `[ui]` of `settings.toml`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UiDto {
    /// `auto`, `light` or `dark`.
    pub theme: String,
    pub quick_start_dismissed: bool,
}

/// The request of `settings_set`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SettingsSetReq {
    pub edits: Vec<SettingEdit>,
}

/// One change: a dotted key such as `ui.theme` and its new value.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SettingEdit {
    pub key: String,
    pub value: SettingValue,
}

/// A settings value as JSON carries it: a boolean, an integer or a string.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum SettingValue {
    Bool(bool),
    Integer(i64),
    Text(String),
}

/// The result of the refresh task: how many projects were re-read and how many notices they raised.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct RefreshDto {
    pub projects: u32,
    pub notices: u32,
}
