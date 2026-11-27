use super::{
    ANSWER_LIMIT, EXIT_REFUSED, Failure, ProjectTools, RUNNER_NAME, ReplyProblem, RunnerError,
    SCHEMA, STDERR_LIMIT, STEPS, STOP_GRACE, VERSION_LIMIT, is_code, is_commit_id, run_bundled,
    same_folder, tool_env,
};
use crate::app::Ctx;
use crate::config::{ProjectTools as ToolsSettings, WikiIndexDevice};
use crate::dto::{JevInput, JevResult, WikiIndexInput, WikiIndexResult};
use crate::paths::oah_home;
use crate::proc::tree::TreeRun;
use crate::tasks::Progress;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::ffi::{OsStr, OsString};
use std::path::Path;
use std::time::Duration;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Reply<T> {
    schema: u64,
    version: String,
    tool: String,
    action: String,
    root: String,
    result: T,
}

#[derive(Serialize)]
struct WikiSettings<'a> {
    wiki_dir: &'a str,
    exclude: &'a [String],
    model: &'a str,
    device: &'a WikiIndexDevice,
    #[serde(skip_serializing_if = "Option::is_none")]
    query: Option<&'a str>,
}

struct Call<'a> {
    group: &'static str,
    action: &'static str,
    root: &'a Path,
    input: &'a [u8],
    max_time: Duration,
    changes: bool,
}

impl ProjectTools {
    pub fn jev(
        &self,
        ctx: &Ctx<'_>,
        root: &Path,
        request: &JevInput,
    ) -> Result<JevResult, RunnerError> {
        let bytes = match request {
            JevInput::StoreKey { key } => {
                if key.is_empty()
                    || key.len() > 4096
                    || !key.bytes().all(|byte| byte.is_ascii_graphic())
                {
                    return Err(RunnerError::Request);
                }
                key.as_bytes()
            }
            JevInput::Enable { token } | JevInput::Test { token } => {
                if token.len() > 128 || !token.bytes().all(|byte| byte.is_ascii_graphic()) {
                    return Err(RunnerError::Request);
                }
                token.as_bytes()
            }
            _ => &[],
        };
        let (result, ok): (JevResult, _) = self.extension(
            ctx,
            Call {
                group: "jev",
                action: request.action(),
                root,
                input: bytes,
                max_time: Duration::from_secs(75),
                changes: request.may_change(),
            },
        )?;
        if result.ok != ok
            || result.status.len() > 48
            || result.status.is_empty()
            || !result
                .status
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        {
            return Err(ReplyProblem::Value.into());
        }
        Ok(result)
    }

    pub fn wiki_index(
        &self,
        ctx: &Ctx<'_>,
        root: &Path,
        settings: &ToolsSettings,
        device: &WikiIndexDevice,
        request: &WikiIndexInput,
    ) -> Result<WikiIndexResult, RunnerError> {
        let query = match request {
            WikiIndexInput::Search { query } => {
                let query = query.trim();
                if query.is_empty()
                    || query.chars().count() > 300
                    || query.chars().any(char::is_control)
                {
                    return Err(RunnerError::Request);
                }
                Some(query)
            }
            _ => None,
        };
        let managed = &settings.managed.wiki_index;
        let input = serde_json::to_vec(&WikiSettings {
            wiki_dir: &settings.wiki.dir.value,
            exclude: &managed.exclude.value,
            model: &managed.embedding_model.value,
            device,
            query,
        })
        .map_err(|_| RunnerError::Request)?;
        let changes = matches!(request, WikiIndexInput::Build);
        let (result, ok): (WikiIndexResult, _) = self.extension(
            ctx,
            Call {
                group: "wiki-index",
                action: request.action(),
                root,
                input: &input,
                max_time: if changes {
                    Duration::from_secs(60 * 60)
                } else {
                    Duration::from_secs(5 * 60)
                },
                changes,
            },
        )?;
        if result.ok != ok
            || result.code.as_deref().is_some_and(|code| !is_code(code))
            || result
                .source
                .as_ref()
                .is_some_and(|source| !is_commit_id(&source.commit))
            || result.results.as_ref().is_some_and(|hits| {
                hits.len() > 20
                    || !hits
                        .iter()
                        .all(|hit| hit.line > 0 && is_relative_note(&hit.file))
            })
        {
            return Err(ReplyProblem::Value.into());
        }
        Ok(result)
    }

    fn extension<T: DeserializeOwned>(
        &self,
        ctx: &Ctx<'_>,
        call: Call<'_>,
    ) -> Result<(T, bool), RunnerError> {
        if !call.root.is_absolute() || call.input.len() > 64 * 1024 {
            return Err(RunnerError::Request);
        }
        let folder = self.folder()?;
        let mut env = tool_env(ctx.env, call.root, true);
        env.retain(|(name, _)| !name.to_string_lossy().starts_with("GIT_"));
        if let Ok(home) = oah_home(ctx.env) {
            env.push((OsString::from("OAH_HOME"), home.into_os_string()));
        }
        let progress = |done| {
            ctx.progress.progress(Progress {
                phase: call.group,
                done,
                total: Some(STEPS),
            })
        };
        progress(0);
        self.check_version(folder, &env, ctx.cancel)?;
        progress(1);
        if ctx.cancel.is_cancelled() {
            return Err(RunnerError::Cancelled {
                may_have_changed: false,
            });
        }
        let args = [
            OsStr::new(call.group),
            OsStr::new(call.action),
            OsStr::new("--root"),
            call.root.as_os_str(),
        ];
        let output = run_bundled(
            &TreeRun {
                program: &self.program,
                args: &args,
                env: &env,
                cwd: folder,
                deadline: call.max_time,
                stdout_limit: ANSWER_LIMIT,
                stderr_limit: STDERR_LIMIT,
                grace: STOP_GRACE,
            },
            ctx.cancel,
            call.input,
        )
        .map_err(|err| RunnerError::from_run(err, call.changes))?;
        let ok = match output.status.code() {
            Some(0) => true,
            Some(EXIT_REFUSED) => false,
            code => return Err(RunnerError::Failed(Failure::Exit(code))),
        };
        let reply: Reply<T> =
            serde_json::from_slice(&output.stdout).map_err(|_| ReplyProblem::Malformed)?;
        if reply.schema != SCHEMA {
            return Err(ReplyProblem::Schema.into());
        }
        if reply.version != crate::VERSION {
            return Err(ReplyProblem::Runner.into());
        }
        if reply.tool != call.group || reply.action != call.action {
            return Err(ReplyProblem::Request.into());
        }
        if !same_folder(Path::new(&reply.root), call.root) {
            return Err(ReplyProblem::Root.into());
        }
        progress(STEPS);
        Ok((reply.result, ok))
    }

    pub(super) fn check_version(
        &self,
        folder: &Path,
        vars: &[(OsString, OsString)],
        cancel: &crate::tasks::CancelToken,
    ) -> Result<(), RunnerError> {
        if cancel.is_cancelled() {
            return Err(RunnerError::Cancelled {
                may_have_changed: false,
            });
        }
        let output = self
            .start(
                folder,
                vars,
                &[OsStr::new("--version")],
                self.deadlines.version,
                VERSION_LIMIT,
                cancel,
            )
            .map_err(|error| RunnerError::from_run(error, false))?;
        let expected = format!("{RUNNER_NAME} {}", crate::VERSION);
        if !output.status.success()
            || std::str::from_utf8(&output.stdout).ok().map(str::trim_end)
                != Some(expected.as_str())
        {
            return Err(RunnerError::Version);
        }
        Ok(())
    }
}

fn is_relative_note(file: &str) -> bool {
    file.len() <= 1024
        && !file.contains(['\\', ':'])
        && !file.chars().any(char::is_control)
        && Path::new(file)
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("md"))
        && file
            .split('/')
            .all(|part| !part.is_empty() && !part.starts_with('.'))
}
