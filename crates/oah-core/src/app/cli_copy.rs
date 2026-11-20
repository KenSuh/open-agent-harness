// CLI 복사: 번들 CLI 를 OAH_HOME/bin 에 두되, OAH_HOME 밖의 비공개 사본에서만 --version 으로 확인하고 결과를 정직하게 분류한다.
use super::Ctx;
use crate::dto::{CliState, NoticeDto, display_path, display_text};
use crate::error::{Coded, Error, codes};
use crate::fsx::{self, Fingerprint, FsxError, FsxErrorKind, WriteReceipt};
use crate::paths;
use crate::proc::{self, Run};
use std::convert::Infallible;
use std::fs;
use std::path::Path;
use std::time::{Duration, Instant};

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
    /// The version the bundled bytes printed for `--version`, run from a private copy this start (`copied` and
    /// `unchanged`). `None` when no run was observed. The installed copy itself is never run.
    pub version: Option<String>,
    /// `OAH_CLI_COPY_FAILED` (`kept-previous`) or `OAH_FILE_UNVERIFIED` (`unknown`).
    pub notice: Option<NoticeDto>,
}

/// Copies the bundled CLI at `bundled` (a path the shell supplies) to `paths::cli_bin`, and records the result
/// in the session for `app_info`. It never fails: every failure is a state and a notice.
///
/// Nothing under `OAH_HOME` runs. The bundled bytes must print `oah <app version>` for `--version` when run from
/// a private copy in a new folder of their own inside the operating system's per-user location
/// (`fsx::validate_private_copy`). A check that cannot be set up or that the bytes fail is `kept-previous`, with
/// nothing written under `OAH_HOME`. The installed copy is read once, without following its final link:
///
/// * The same bytes are `unchanged` and nothing is written. On Unix the mode of that same opened file must also
///   let this user run it (the owner, group or others bit that applies, any bit for root); a copy without it is
///   `kept-previous`. The installed copy is not run, so a policy that applies only to its path is not observed:
///   a `noexec` mount, access control lists or mandatory access control, quarantine or Gatekeeper on macOS, and
///   AppLocker, WDAC or SmartScreen rules on Windows.
/// * Other bytes go to a temporary sibling that is renamed into place and read back without following the final
///   link; the sibling is not run. A failure before the rename keeps the previous copy (`kept-previous`); a
///   failure after it is `unknown`, and the next start compares the bytes again.
pub fn cli_copy(ctx: &Ctx<'_>, bundled: &Path) -> CliCopy {
    let copy = copy(ctx, bundled);
    ctx.session.set_cli(copy.clone());
    copy
}

fn copy(ctx: &Ctx<'_>, bundled: &Path) -> CliCopy {
    let (home, dest) = match (paths::oah_home(ctx.env), paths::cli_bin(ctx.env)) {
        (Ok(home), Ok(dest)) => (home, dest),
        (Err(error), _) | (_, Err(error)) => return kept(ctx, "finding OAH_HOME", error.message()),
    };
    if !fs::metadata(bundled).is_ok_and(|metadata| metadata.is_file()) {
        return kept(ctx, "bundled CLI not found", &bundled.display().to_string());
    }
    let bytes = match fs::read(bundled) {
        Ok(bytes) => bytes,
        Err(error) => return kept(ctx, "reading the bundled CLI", &error.to_string()),
    };
    let existing = match fsx::read_regular_optional(&dest) {
        Ok(existing) => existing,
        Err(error) => {
            return kept(
                ctx,
                "inspecting the installed CLI path",
                &Error::from(error).message(),
            );
        }
    };
    let same = existing
        .filter(|(installed, _)| Fingerprint::of(installed) == Fingerprint::of(&bytes))
        .map(|(_, metadata)| metadata);
    if let Some(metadata) = &same
        && let Err(reason) = may_execute(metadata)
    {
        return kept(ctx, "checking the installed copy", &reason);
    }
    let name = dest.file_name().unwrap_or_default();
    let failed = match fsx::validate_private_copy(&home, name, &bytes, |copy| check(ctx, copy)) {
        Ok(Ok(())) => None,
        Ok(Err(reason)) if same.is_some() => Some(("checking the installed copy", reason)),
        Ok(Err(reason)) => Some(("the --version check", reason)),
        Err(error) => Some(("preparing the private check", Error::from(error).message())),
    };
    if let Some((step, reason)) = failed {
        return kept(ctx, step, &reason);
    }
    if same.is_some() {
        return CliCopy {
            state: CliState::Unchanged,
            version: Some(crate::VERSION.to_string()),
            notice: None,
        };
    }
    if let Some(folder) = dest.parent()
        && let Err(error) = fs::create_dir_all(folder)
    {
        let detail = format!("{}: {error}", folder.display());
        return kept(ctx, "creating the bin folder", &detail);
    }
    // These bytes already passed the private check; the temporary sibling is published without running it.
    let published = fsx::publish_executable(&dest, &bytes, |_| Ok::<(), Infallible>(()));
    classify(ctx, &dest, published.map(|Ok(receipt)| receipt))
}

/// Whether this process may run the installed copy as the mode of the same opened file says: the owner's bit for
/// its owner, the group's bit for a member of its group, otherwise the others' bit, and any bit for root.
#[cfg(unix)]
fn may_execute(metadata: &fs::Metadata) -> Result<(), String> {
    use rustix::process::{getegid, geteuid, getgroups};
    use std::os::unix::fs::MetadataExt;
    let (user, group) = (geteuid(), metadata.gid());
    let member = getegid().as_raw() == group
        || getgroups().is_ok_and(|groups| groups.iter().any(|gid| gid.as_raw() == group));
    let bit = if user.is_root() {
        0o111
    } else if metadata.uid() == user.as_raw() {
        0o100
    } else if member {
        0o010
    } else {
        0o001
    };
    let mode = metadata.mode() & 0o7777;
    if mode & bit == 0 {
        return Err(format!("not executable for this user (mode {mode:o})"));
    }
    Ok(())
}

/// Windows has no execute bits; a policy that applies to the installed path is not observed.
#[cfg(not(unix))]
fn may_execute(_metadata: &fs::Metadata) -> Result<(), String> {
    Ok(())
}

/// Requires the private copy at `program` to print exactly `oah <app version>` for `--version`.
fn check(ctx: &Ctx<'_>, program: &Path) -> Result<(), String> {
    let expected = format!("oah {}", crate::VERSION);
    match version_line(ctx, program) {
        Ok(line) if line == expected => Ok(()),
        Ok(_) => Err(format!("the private copy did not print {expected:?}")),
        Err(reason) => Err(reason),
    }
}

fn version_line(ctx: &Ctx<'_>, program: &Path) -> Result<String, String> {
    let run = |deadline| {
        proc::run(
            ctx.env,
            &Run {
                program,
                args: &["--version"],
                deadline,
                limit: VERSION_OUTPUT_LIMIT,
            },
        )
    };
    let start = Instant::now();
    let result = run(VERSION_DEADLINE);
    // Linux reports a file written a moment ago busy while a child another thread is starting still holds it
    // open for writing; one short retry within the same deadline covers that.
    let result = if cfg!(target_os = "linux")
        && matches!(&result, Err(proc::ProcError::Spawn(error)) if error.kind() == std::io::ErrorKind::ExecutableFileBusy)
    {
        std::thread::sleep(Duration::from_millis(10));
        let remaining = VERSION_DEADLINE.saturating_sub(start.elapsed());
        if remaining.is_zero() {
            result
        } else {
            run(remaining)
        }
    } else {
        result
    };
    let output = result.map_err(|error| format!("the --version run failed: {error}"))?;
    if !output.status.success() {
        return Err(format!("the --version run exited with {}", output.status));
    }
    String::from_utf8(output.stdout)
        .map(|text| text.trim_end().to_string())
        .map_err(|_| String::from("the --version run printed bytes that are not UTF-8"))
}

/// The state, version and notice of a publish result.
fn classify(ctx: &Ctx<'_>, dest: &Path, published: Result<WriteReceipt, FsxError>) -> CliCopy {
    match published {
        Ok(_receipt) => CliCopy {
            state: CliState::Copied,
            version: Some(crate::VERSION.to_string()),
            notice: None,
        },
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
    }
}
