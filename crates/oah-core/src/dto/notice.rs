// 알림 DTO: 막지 않는 발견을 {id, level, code, message, hint, actions} 로 나타내고 설정 알림을 바꿔 담는다.
use crate::config::Notice;
use crate::error::hint_for;
use crate::paths::Env;
use serde::Serialize;

/// A non-fatal finding (CONTRACTS C3). `id` is stable for the same code and subject, so a repeated notice can
/// replace an earlier one; `actions` are action ids the UI may offer (none in this slice).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct NoticeDto {
    pub id: String,
    pub level: NoticeLevel,
    pub code: String,
    pub message: String,
    pub hint: Option<String>,
    pub actions: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum NoticeLevel {
    Info,
    Warning,
}

impl NoticeDto {
    /// A warning with its code's hint and no actions. `message` must already show paths with `~`.
    pub fn warning(code: &str, subject: &str, message: String) -> NoticeDto {
        NoticeDto::with_level(NoticeLevel::Warning, code, subject).message(message)
    }

    /// An informational notice with its code's hint and no actions.
    pub fn info(code: &str, subject: &str, message: String) -> NoticeDto {
        NoticeDto::with_level(NoticeLevel::Info, code, subject).message(message)
    }

    /// A configuration notice (such as `OAH_CONFIG_UNKNOWN_KEY`) as a warning, its keys as the subject.
    pub fn from_config(env: &Env, notice: &Notice) -> NoticeDto {
        NoticeDto::warning(
            notice.code(),
            &notice.keys().join(","),
            super::display_text(env, notice.message()),
        )
    }

    fn with_level(level: NoticeLevel, code: &str, subject: &str) -> NoticeDto {
        NoticeDto {
            id: if subject.is_empty() {
                code.to_string()
            } else {
                format!("{code}:{subject}")
            },
            level,
            code: code.to_string(),
            message: String::new(),
            hint: hint_for(code).map(str::to_string),
            actions: Vec::new(),
        }
    }

    fn message(mut self, message: String) -> NoticeDto {
        self.message = message;
        self
    }
}
