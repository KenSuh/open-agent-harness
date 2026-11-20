// CLI 복사: 번들 CLI 를 OAH_HOME/bin 에 두되, 바꿔 넣기 전에 임시 사본을 --version 으로 확인하고 결과를 정직하게 분류한다.
use super::Ctx;
use crate::dto::{CliState, NoticeDto, display_path, display_text};
use crate::error::{Coded, Error, codes};
use crate::fsx::{self, Fingerprint, FsxError, FsxErrorKind, WriteReceipt};
use crate::paths;
use crate::proc::{self, Run};
use std::fs;
use std::path::Path;
use std::time::Duration;

/// How long a `--version` run may take.
const VERSION_DEADLINE: Duration = Duration::from_secs(10);
/// How many bytes a `--version` run may print on each stream.
const VERSION_OUTPUT_LIMIT: usize = 4096;
/// The notice subject of every CLI copy notice, so a later start's notice replaces an earlier one.
const SUBJECT: &str = "cli-copy";

/// What this start's CLI copy did (CONTRACTS C7).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliCopy {
    pub state: CliState,
    /// The version a `--version` run printed this start: on the validated candidate (`copied`) or the existing
    /// copy (`unchanged`). `None` when no run was observed.
    pub version: Option<String>,
    /// `OAH_CLI_COPY_FAILED` (`kept-previous`) or `OAH_FILE_UNVERIFIED` (`unknown`).
    pub notice: Option<NoticeDto>,
}

/// Copies the bundled CLI at `bundled` (a path the shell supplies) to `paths::cli_bin`, and records the result
/// in the session for `app_info`. It never fails: every failure is a state and a notice.
///
/// The same bytes already in place are `unchanged` once the installed copy itself prints `oah <app version>`
/// for `--version`; an installed copy that fails that check is left as it is and reported `kept-previous`.
/// Otherwise the bytes go to a temporary sibling that must pass the same check before it is renamed into place.
/// A failure before the rename keeps the previous copy (`kept-previous`); a failure after it is `unknown`, and
/// the next start compares hashes again.
pub fn cli_copy(ctx: &Ctx<'_>, bundled: &Path) -> CliCopy {
    let copy = copy(ctx, bundled);
    ctx.session.set_cli(copy.clone());
    copy
}

fn copy(ctx: &Ctx<'_>, bundled: &Path) -> CliCopy {
    let dest = match paths::cli_bin(ctx.env) {
        Ok(dest) => dest,
        Err(error) => return kept(ctx, "finding OAH_HOME", error.message()),
    };
    if !fs::metadata(bundled).is_ok_and(|metadata| metadata.is_file()) {
        return kept(ctx, "bundled CLI not found", &bundled.display().to_string());
    }
    let bytes = match fs::read(bundled) {
        Ok(bytes) => bytes,
        Err(error) => return kept(ctx, "reading the bundled CLI", &error.to_string()),
    };
    let existing = match read_destination(&dest) {
        Ok(existing) => existing,
        Err(error) => return kept(ctx, "inspecting the installed CLI path", &error),
    };
    if let Some(existing) = existing
        && Fingerprint::of(&existing) == Fingerprint::of(&bytes)
    {
        // Equal bytes skip the publish, never the validation: a copy that cannot run is reported, not repaired.
        return match check(ctx, &dest) {
            Ok(()) => CliCopy {
                state: CliState::Unchanged,
                version: Some(crate::VERSION.to_string()),
                notice: None,
            },
            Err(reason) => kept(ctx, "checking the installed copy", &reason),
        };
    }
    if let Some(folder) = dest.parent()
        && let Err(error) = fs::create_dir_all(folder)
    {
        let detail = format!("{}: {error}", folder.display());
        return kept(ctx, "creating the bin folder", &detail);
    }
    let published = fsx::publish_executable(&dest, &bytes, |candidate| check(ctx, candidate));
    classify(ctx, &dest, published)
}

fn read_destination(dest: &Path) -> Result<Option<Vec<u8>>, String> {
    match fs::symlink_metadata(dest) {
        Ok(metadata) if is_plain_file(&metadata) => {
            fsx::read_optional(dest).map_err(|error| Error::from(error).message().to_string())
        }
        Ok(_) => Err(format!("{} is not a regular file", dest.display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("{}: {error}", dest.display())),
    }
}

fn is_plain_file(metadata: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return false;
        }
    }
    metadata.is_file()
}

/// Requires `program` (a candidate or the installed copy) to print exactly `oah <app version>` for `--version`.
fn check(ctx: &Ctx<'_>, program: &Path) -> Result<(), String> {
    let expected = format!("oah {}", crate::VERSION);
    match version_line(ctx, program) {
        Ok(line) if line == expected => Ok(()),
        Ok(_) => Err(format!("the --version run did not print {expected:?}")),
        Err(reason) => Err(reason),
    }
}

fn version_line(ctx: &Ctx<'_>, program: &Path) -> Result<String, String> {
    let output = proc::run(
        ctx.env,
        &Run {
            program,
            args: &["--version"],
            deadline: VERSION_DEADLINE,
            limit: VERSION_OUTPUT_LIMIT,
        },
    )
    .map_err(|error| format!("the --version run failed: {error}"))?;
    if !output.status.success() {
        return Err(format!("the --version run exited with {}", output.status));
    }
    String::from_utf8(output.stdout)
        .map(|text| text.trim_end().to_string())
        .map_err(|_| String::from("the --version run printed bytes that are not UTF-8"))
}

/// The state, version and notice of a publish result.
fn classify(
    ctx: &Ctx<'_>,
    dest: &Path,
    published: Result<Result<WriteReceipt, String>, FsxError>,
) -> CliCopy {
    match published {
        Ok(Ok(_receipt)) => CliCopy {
            state: CliState::Copied,
            version: Some(crate::VERSION.to_string()),
            notice: None,
        },
        Ok(Err(reason)) => kept(ctx, "the --version check", &reason),
        Err(error) if error.kind() == FsxErrorKind::Unverified => {
            let message = format!(
                "{}: the CLI copy may be the old or the new version ({}); the next start checks it again",
                display_path(ctx.env, dest),
                display_text(ctx.env, &Error::from(error).message()),
            );
            CliCopy {
                state: CliState::Unknown,
                version: None,
                notice: Some(NoticeDto::warning(
                    FsxErrorKind::Unverified.code(),
                    SUBJECT,
                    message,
                )),
            }
        }
        Err(error) => kept(ctx, "publishing the copy", &Error::from(error).message()),
    }
}

/// `kept-previous` with `OAH_CLI_COPY_FAILED` naming the step that failed. No version: none was observed.
fn kept(ctx: &Ctx<'_>, step: &str, detail: &str) -> CliCopy {
    let message = format!("the CLI copy was not updated ({step}): {detail}");
    CliCopy {
        state: CliState::KeptPrevious,
        version: None,
        notice: Some(NoticeDto::warning(
            codes::CLI_COPY_FAILED,
            SUBJECT,
            display_text(ctx.env, &message),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Session;
    use crate::tasks::{CancelToken, NoProgress};
    use std::path::PathBuf;

    const HOME_VAR: &str = if cfg!(windows) { "USERPROFILE" } else { "HOME" };

    fn session() -> (tempfile::TempDir, Session) {
        let dir = tempfile::tempdir().unwrap();
        let mut env = crate::paths::Env::new(dir.path()).unwrap();
        env.set(HOME_VAR, dir.path());
        (dir, Session::new(env))
    }

    #[test]
    fn a_failure_after_the_rename_is_unknown_with_no_version() {
        let (dir, session) = session();
        let cancel = CancelToken::new();
        let ctx = session.ctx(&cancel, &NoProgress);
        let dest = dir.path().join(".oah").join("bin").join("oah");
        let unverified = FsxError::new(
            FsxErrorKind::Unverified,
            &dest,
            "replaced, but the folder could not be synced",
        );
        let copy = classify(&ctx, &dest, Err(unverified));
        assert_eq!(copy.state, CliState::Unknown);
        assert_eq!(copy.version, None);
        let notice = copy.notice.unwrap();
        assert_eq!(notice.code, "OAH_FILE_UNVERIFIED");
        let shown = std::path::Path::new("~")
            .join(".oah")
            .join("bin")
            .join("oah");
        let expected = format!(
            "{}: the CLI copy may be the old or the new version",
            shown.display()
        );
        assert!(notice.message.starts_with(&expected), "{}", notice.message);
    }

    #[test]
    fn a_failure_before_the_rename_keeps_the_previous_copy_with_no_version() {
        let (dir, session) = session();
        let cancel = CancelToken::new();
        let ctx = session.ctx(&cancel, &NoProgress);
        let dest = dir.path().join(".oah").join("bin").join("oah");
        let failed = FsxError::new(FsxErrorKind::Io, &dest, "cannot replace");
        let copy = classify(&ctx, &dest, Err(failed));
        assert_eq!(copy.state, CliState::KeptPrevious);
        assert_eq!(copy.version, None);
        let notice = copy.notice.unwrap();
        assert_eq!(notice.code, "OAH_CLI_COPY_FAILED");
        assert!(
            notice.message.contains("(publishing the copy)"),
            "{}",
            notice.message
        );
        let rejected = classify(&ctx, &PathBuf::from(&dest), Ok(Err(String::from("no"))));
        assert_eq!(rejected.state, CliState::KeptPrevious);
    }
}
