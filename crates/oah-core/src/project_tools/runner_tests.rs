// 프로젝트 도구 실행기 어댑터 시험: 답 검증은 프로세스 없이, 인자·환경·없는 도구·다른 판·출력 상한·마감·취소는 가짜 도구 스크립트로 확인한다.
//! Tests of the project-tools adapter: the answer checks without a process, the rest against a stand-in tool.

use super::*;
use serde_json::{Value, json};

const HEAD: &str = "0123456789abcdef0123456789abcdef01234567";

/// An exit status with `code`, as a finished tool would have.
fn exit(code: i32) -> std::process::ExitStatus {
    #[cfg(unix)]
    {
        std::os::unix::process::ExitStatusExt::from_raw(code << 8)
    }
    #[cfg(windows)]
    {
        std::os::windows::process::ExitStatusExt::from_raw(code as u32)
    }
}

/// A complete answer about `root`, with every field the tool prints, free text and absolute paths included.
fn answer(action: &str, root: &Path) -> Value {
    json!({
        "schema": 1, "tool": "impact", "action": action, "ok": true, "state": "fresh",
        "reason": "", "performed": "none", "changed": 0,
        "root": root, "branch": "main", "head": HEAD,
        "store": {"path": root.join("private-store-path"), "source": "git-common-dir"},
        "map": {
            "commit": HEAD, "updated_at": "2026-11-28T00:00:00Z", "manifest": "m", "tool": "t", "result": "r",
            "dirty": 1, "untracked": 2, "includes_uncommitted": true, "excluded_paths": ["a/b.bin"],
            "excluded_total": 1, "sql_unparsed": 0, "warnings": ["a warning text"],
        },
        "graph": {
            "state": "absent", "detail": "graph detail text", "file": "graphify-out/graph.json",
            "commit": null, "nodes": 0, "edges": 0,
        },
        "runner": {"name": "oah-project-tools", "version": crate::VERSION, "frozen": true},
        "git": {"path": "/private-git-path/git", "version": "2.50.1"},
        "error": null, "warnings": ["another warning text"],
    })
}

fn finished(code: i32, stdout: impl Into<Vec<u8>>) -> Output {
    Output {
        status: exit(code),
        stdout: stdout.into(),
        stderr: Vec::new(),
    }
}

fn printed(code: i32, document: &Value) -> Output {
    finished(code, format!("{document}\n"))
}

#[test]
fn a_complete_answer_becomes_checked_values() {
    let dir = tempfile::tempdir().unwrap();
    let mut document = answer("refresh", dir.path());
    document["performed"] = json!("refreshed");
    document["changed"] = json!(7);
    let reply = read_answer(ImpactAction::Refresh, dir.path(), &printed(0, &document)).unwrap();
    assert_eq!(
        reply,
        ImpactReply {
            action: ImpactAction::Refresh,
            state: MapState::Fresh,
            performed: Performed::Refreshed,
            changed: 7,
            head: Some(HEAD.to_string()),
            branch: Some(String::from("main")),
            map: Some(MapSummary {
                commit: HEAD.to_string(),
                dirty: 1,
                untracked: 2,
                includes_uncommitted: true,
                excluded_total: 1,
                sql_unparsed: 0,
            }),
            graph: Some(GraphSummary {
                state: GraphState::Absent,
                nodes: 0,
                edges: 0,
            }),
            refusal: None,
        }
    );
}

#[test]
fn a_status_before_the_first_commit_has_no_head_map_or_graph() {
    let dir = tempfile::tempdir().unwrap();
    let mut document = answer("status", dir.path());
    for name in ["head", "branch", "map", "graph"] {
        document[name] = Value::Null;
    }
    document["state"] = json!("no_commit");
    let reply = read_answer(ImpactAction::Status, dir.path(), &printed(0, &document)).unwrap();
    assert_eq!(reply.state, MapState::NoCommit);
    assert_eq!(
        (reply.head, reply.branch, reply.map, reply.graph),
        (None, None, None, None)
    );
}

#[test]
fn a_refusal_is_an_answer_that_carries_the_code_and_none_of_the_text() {
    let dir = tempfile::tempdir().unwrap();
    let mut document = answer("refresh", dir.path());
    document["ok"] = json!(false);
    document["state"] = json!("unavailable");
    document["reason"] = json!("git 2.20 at /private-git-path/git is too old");
    document["error"] = json!({
        "code": "OAH_PROJECT_TOOLS_GIT_UNSUPPORTED",
        "message": "git 2.20 at /private-git-path/git is too old",
    });
    let reply = read_answer(ImpactAction::Refresh, dir.path(), &printed(69, &document)).unwrap();
    assert_eq!(
        reply.refusal.as_deref(),
        Some("OAH_PROJECT_TOOLS_GIT_UNSUPPORTED")
    );
    assert_eq!(reply.state, MapState::Unavailable);
    let shown = format!("{reply:?}");
    for hidden in [
        "private",
        "too old",
        "warning text",
        "detail text",
        "graphify-out",
    ] {
        assert!(!shown.contains(hidden), "{hidden} in {shown}");
    }
}

#[test]
fn an_answer_that_cannot_be_used_is_refused_with_the_reason() {
    let dir = tempfile::tempdir().unwrap();
    let other = tempfile::tempdir().unwrap();
    let root = dir.path();
    let status = |change: &dyn Fn(&mut Value)| {
        let mut document = answer("status", root);
        change(&mut document);
        document
    };
    let refusal = |change: &dyn Fn(&mut Value)| {
        status(&|document| {
            document["ok"] = json!(false);
            document["error"] = json!({"code": "OAH_IMPACT_NO_COMMIT", "message": "m"});
            change(document);
        })
    };
    let whole = answer("status", root).to_string();
    let cases: Vec<(&str, Output, RunnerError)> = vec![
        (
            "not JSON",
            finished(0, "oah-project-tools: no answer\n"),
            ReplyProblem::Malformed.into(),
        ),
        ("nothing", finished(0, ""), ReplyProblem::Malformed.into()),
        (
            "two answers",
            finished(0, format!("{whole}\n{whole}\n")),
            ReplyProblem::Malformed.into(),
        ),
        (
            "text after the answer",
            finished(0, format!("{whole}\ndone\n")),
            ReplyProblem::Malformed.into(),
        ),
        (
            "a list",
            finished(0, format!("[{whole}]")),
            ReplyProblem::Malformed.into(),
        ),
        (
            "no schema",
            printed(0, &status(&|d| d["schema"] = Value::Null)),
            ReplyProblem::Schema.into(),
        ),
        (
            "another schema",
            printed(0, &status(&|d| d["schema"] = json!(2))),
            ReplyProblem::Schema.into(),
        ),
        (
            "another tool",
            printed(0, &status(&|d| d["tool"] = json!("jev"))),
            ReplyProblem::Request.into(),
        ),
        (
            "no tool",
            printed(0, &status(&|d| d["tool"] = Value::Null)),
            ReplyProblem::Request.into(),
        ),
        (
            "another action",
            printed(0, &status(&|d| d["action"] = json!("refresh"))),
            ReplyProblem::Request.into(),
        ),
        (
            "another version",
            printed(0, &status(&|d| d["runner"]["version"] = json!("0.0.1"))),
            ReplyProblem::Runner.into(),
        ),
        (
            "another program",
            printed(0, &status(&|d| d["runner"]["name"] = json!("oah"))),
            ReplyProblem::Runner.into(),
        ),
        (
            "another folder",
            printed(0, &status(&|d| d["root"] = json!(other.path()))),
            ReplyProblem::Root.into(),
        ),
        (
            "a folder that is not there",
            printed(0, &status(&|d| d["root"] = json!(root.join("gone")))),
            ReplyProblem::Root.into(),
        ),
        (
            "success that is not ok",
            printed(0, &refusal(&|_| {})),
            ReplyProblem::Outcome.into(),
        ),
        (
            "success with an error",
            printed(
                0,
                &status(&|d| d["error"] = json!({"code": "OAH_X", "message": ""})),
            ),
            ReplyProblem::Outcome.into(),
        ),
        (
            "a refusal that is ok",
            printed(69, &status(&|_| {})),
            ReplyProblem::Outcome.into(),
        ),
        (
            "a refusal without a reason",
            printed(69, &refusal(&|d| d["error"] = Value::Null)),
            ReplyProblem::Outcome.into(),
        ),
        (
            "a refusal whose code is text",
            printed(
                69,
                &refusal(&|d| d["error"]["code"] = json!("git failed in /private")),
            ),
            ReplyProblem::Outcome.into(),
        ),
        (
            "a status that built a map",
            printed(0, &status(&|d| d["performed"] = json!("built"))),
            ReplyProblem::Value.into(),
        ),
        (
            "a status that changed files",
            printed(0, &status(&|d| d["changed"] = json!(3))),
            ReplyProblem::Value.into(),
        ),
        (
            "a short head",
            printed(0, &status(&|d| d["head"] = json!("0123abc"))),
            ReplyProblem::Value.into(),
        ),
        (
            "a map commit that is no id",
            printed(
                0,
                &status(&|d| d["map"]["commit"] = json!(HEAD.to_uppercase())),
            ),
            ReplyProblem::Value.into(),
        ),
        (
            "a branch with a control character",
            printed(0, &status(&|d| d["branch"] = json!("main\u{1b}[31m"))),
            ReplyProblem::Value.into(),
        ),
        (
            "an unknown state",
            printed(0, &status(&|d| d["state"] = json!("ephemeral"))),
            ReplyProblem::Malformed.into(),
        ),
        (
            "a negative count",
            printed(0, &status(&|d| d["changed"] = json!(-1))),
            ReplyProblem::Malformed.into(),
        ),
        (
            "no runner",
            printed(0, &status(&|d| d["runner"] = Value::Null)),
            ReplyProblem::Malformed.into(),
        ),
        (
            "a usage error",
            printed(1, &status(&|_| {})),
            RunnerError::Failed(Failure::Exit(Some(1))),
        ),
        (
            "an internal error",
            printed(70, &status(&|_| {})),
            RunnerError::Failed(Failure::Exit(Some(70))),
        ),
    ];
    for (name, output, expected) in cases {
        let found = read_answer(ImpactAction::Status, root, &output);
        assert_eq!(found, Err(expected), "{name}");
    }
}

#[cfg(unix)]
#[test]
fn the_answered_root_may_be_the_folder_behind_a_link() {
    let dir = tempfile::tempdir().unwrap();
    let real = fs::canonicalize(dir.path()).unwrap().join("real");
    fs::create_dir(&real).unwrap();
    let link = dir.path().join("link");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    let reply = read_answer(
        ImpactAction::Status,
        &link,
        &printed(0, &answer("status", &real)),
    );
    assert_eq!(reply.map(|reply| reply.state), Ok(MapState::Fresh));
}

#[cfg(windows)]
#[test]
fn the_answered_root_may_differ_in_letter_case() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("Project");
    fs::create_dir(&root).unwrap();
    let shouted = PathBuf::from(root.to_str().unwrap().to_uppercase());
    let reply = read_answer(
        ImpactAction::Status,
        &root,
        &printed(0, &answer("status", &shouted)),
    );
    assert_eq!(reply.map(|reply| reply.state), Ok(MapState::Fresh));
}

#[test]
fn only_absolute_path_entries_outside_the_project_are_kept() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("project");
    let outside = dir.path().join("tools");
    fs::create_dir_all(root.join("bin")).unwrap();
    fs::create_dir(&outside).unwrap();
    let given = std::env::join_paths([
        root.join("bin"),
        PathBuf::from("relative"),
        outside.clone(),
        fs::canonicalize(&root).unwrap().join("bin"),
        dir.path().join("missing"),
    ])
    .unwrap();
    let kept = path_outside(&given, &root).unwrap();
    let kept: Vec<PathBuf> = std::env::split_paths(&kept).collect();
    assert_eq!(kept, [outside, dir.path().join("missing")]);
    let only_inside = std::env::join_paths([root.join("bin")]).unwrap();
    assert_eq!(path_outside(&only_inside, &root), None);
}

#[test]
fn every_error_has_its_own_code_and_a_message_without_a_path() {
    let errors = [
        RunnerError::Unavailable,
        RunnerError::Version,
        RunnerError::Request,
        RunnerError::Cancelled {
            may_have_changed: true,
        },
        RunnerError::TimedOut {
            may_have_changed: true,
        },
        RunnerError::Output,
        RunnerError::Failed(Failure::Exit(Some(70))),
        RunnerError::Reply(ReplyProblem::Root),
    ];
    let codes: std::collections::BTreeSet<&str> = errors.iter().map(Coded::code).collect();
    assert_eq!(codes.len(), errors.len());
    for error in errors {
        assert!(is_code(error.code()), "{error:?}");
        let message = error.message();
        assert!(!message.contains(['/', '\\']), "{message}");
        assert_eq!(
            error.is_cancelled(),
            matches!(error, RunnerError::Cancelled { .. })
        );
    }
    for stopped in [
        RunnerError::Cancelled {
            may_have_changed: true,
        },
        RunnerError::TimedOut {
            may_have_changed: true,
        },
    ] {
        assert!(
            stopped.message().contains("check the status again"),
            "{stopped}"
        );
    }
    let unchanged = RunnerError::Cancelled {
        may_have_changed: false,
    }
    .message();
    assert!(
        unchanged.contains("before they changed anything"),
        "{unchanged}"
    );
}

#[cfg(unix)]
mod with_a_tool {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::sync::Mutex;
    use std::time::Instant;

    /// The steps a call reported.
    #[derive(Default)]
    struct Steps(Mutex<Vec<(&'static str, u32, Option<u32>)>>);

    impl ProgressSink for Steps {
        fn progress(&self, progress: Progress) {
            let step = (progress.phase, progress.done, progress.total);
            self.0.lock().unwrap().push(step);
        }
    }

    /// `action` behind the line that answers `--version` like this build's tool.
    fn versioned(action: &str) -> String {
        format!(
            "if [ \"$1\" = --version ]; then echo 'oah-project-tools {}'; exit 0; fi\n{action}",
            crate::VERSION
        )
    }

    /// Waits until the new script `program` can be started. A process another test thread forked while the file
    /// was being written can hold it open for writing for a moment, and starting it then fails as "text file busy".
    fn settle(program: &Path) {
        while let Err(error) = std::process::Command::new(program).arg("--settle").status() {
            assert_eq!(error.kind(), io::ErrorKind::ExecutableFileBusy, "{error}");
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    /// A stand-in tool in its own folder, a project folder, and the session environment of a call.
    struct Stand {
        dir: tempfile::TempDir,
        tools: ProjectTools,
        root: PathBuf,
        env: Env,
    }

    impl Stand {
        /// `body` is the stand-in's shell text; `$here` is its folder. It names every program by absolute path,
        /// because it does not always get a `PATH`.
        fn new(body: &str) -> Stand {
            let dir = tempfile::tempdir().unwrap();
            let bin = fs::canonicalize(dir.path()).unwrap().join("bin");
            let root = dir.path().join("project");
            fs::create_dir(&bin).unwrap();
            fs::create_dir(&root).unwrap();
            let program = bin.join(SIDECAR_NAME);
            let script = format!(
                "#!/bin/sh\nif [ \"$1\" = --settle ]; then exit 0; fi\nhere=${{0%/*}}\n{body}\n"
            );
            fs::write(&program, script).unwrap();
            fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
            settle(&program);
            let env = Env::new(dir.path()).unwrap();
            Stand {
                tools: ProjectTools::new(program),
                dir,
                root,
                env,
            }
        }

        /// A stand-in that prints `answer.json` and ends with `code`.
        fn answering(code: i32, document: &Value) -> Stand {
            let action = format!("/bin/cat \"$here/answer.json\"\nexit {code}");
            let stand = Stand::new(&versioned(&action));
            stand.answer(document);
            stand
        }

        fn answer(&self, document: &Value) {
            fs::write(self.bin("answer.json"), format!("{document}\n")).unwrap();
        }

        /// A file beside the stand-in.
        fn bin(&self, name: &str) -> PathBuf {
            self.tools.program.with_file_name(name)
        }

        /// The same stand-in with `deadline` for an action; the version check keeps a long one.
        fn within(mut self, deadline: Duration) -> Stand {
            self.tools.deadlines = Deadlines {
                version: Duration::from_secs(60),
                status: deadline,
                refresh: deadline,
            };
            self
        }

        fn call(
            &self,
            action: ImpactAction,
            cancel: &CancelToken,
        ) -> Result<ImpactReply, RunnerError> {
            let request = ImpactRequest {
                action,
                root: &self.root,
                git: None,
            };
            self.tools
                .impact_in(&self.env, cancel, &crate::tasks::NoProgress, &request)
        }
    }

    /// Cancels once `file` exists, which the stand-in writes when it is running.
    fn cancel_once(file: PathBuf, cancel: &CancelToken) -> std::thread::JoinHandle<()> {
        let cancel = cancel.clone();
        std::thread::spawn(move || {
            let until = Instant::now() + Duration::from_secs(60);
            while !file.exists() && Instant::now() < until {
                std::thread::sleep(Duration::from_millis(5));
            }
            cancel.cancel();
        })
    }

    #[test]
    fn a_missing_tool_is_unavailable() {
        let dir = tempfile::tempdir().unwrap();
        let tools = ProjectTools::new(dir.path().join(SIDECAR_NAME));
        let request = ImpactRequest {
            action: ImpactAction::Status,
            root: dir.path(),
            git: None,
        };
        let env = Env::new(dir.path()).unwrap();
        let steps = Steps::default();
        let found = tools.impact_in(&env, &CancelToken::new(), &steps, &request);
        assert_eq!(found, Err(RunnerError::Unavailable));
        assert!(steps.0.lock().unwrap().is_empty());
        assert!(!tools.installed());
    }

    #[test]
    fn a_program_that_is_not_the_installed_tool_is_never_started() {
        let stand = Stand::new("echo ran > \"$here/ran\"");
        assert!(stand.tools.installed());
        let renamed = stand.bin("another-name");
        fs::copy(&stand.tools.program, &renamed).unwrap();
        let folder = stand.dir.path().join("folder").join(SIDECAR_NAME);
        fs::create_dir_all(&folder).unwrap();
        for program in [
            PathBuf::from("/bin/sh"),
            PathBuf::from(SIDECAR_NAME),
            PathBuf::from("bin").join(SIDECAR_NAME),
            renamed,
            folder,
        ] {
            let tools = ProjectTools::new(&program);
            assert!(!tools.installed(), "{}", program.display());
            let request = ImpactRequest {
                action: ImpactAction::Refresh,
                root: &stand.root,
                git: None,
            };
            let found = tools.impact_in(
                &stand.env,
                &CancelToken::new(),
                &crate::tasks::NoProgress,
                &request,
            );
            assert_eq!(
                found,
                Err(RunnerError::Unavailable),
                "{}",
                program.display()
            );
        }
        assert!(!stand.bin("ran").exists());
    }

    #[test]
    fn a_relative_root_or_git_is_refused_before_anything_starts() {
        let stand = Stand::new("echo ran > \"$here/ran\"");
        let requests = [
            (Path::new("project"), None),
            (stand.root.as_path(), Some(Path::new("git"))),
        ];
        for (root, git) in requests {
            let request = ImpactRequest {
                action: ImpactAction::Refresh,
                root,
                git,
            };
            let found = stand.tools.impact_in(
                &stand.env,
                &CancelToken::new(),
                &crate::tasks::NoProgress,
                &request,
            );
            assert_eq!(found, Err(RunnerError::Request));
        }
        assert!(!stand.bin("ran").exists());
    }

    #[test]
    fn a_tool_of_another_version_runs_no_action() {
        let exact = format!("echo 'oah-project-tools {}'", crate::VERSION);
        for printed in [
            String::from("echo 'oah-project-tools 0.0.1'; exit 0"),
            format!("echo 'oah {}'; exit 0", crate::VERSION),
            format!("{exact}; echo more; exit 0"),
            format!("{exact}; exit 3"),
            String::from("exit 0"),
        ] {
            let stand = Stand::new(&format!(
                "if [ \"$1\" = --version ]; then {printed}; fi\necho ran > \"$here/ran\""
            ));
            let found = stand.call(ImpactAction::Refresh, &CancelToken::new());
            assert_eq!(found, Err(RunnerError::Version), "{printed}");
            assert!(!stand.bin("ran").exists(), "{printed}");
        }
    }

    #[test]
    fn a_tool_that_cannot_be_started_or_is_killed_is_a_failure() {
        let stand = Stand::new(&versioned("kill -9 $$"));
        let found = stand.call(ImpactAction::Status, &CancelToken::new());
        assert_eq!(found, Err(RunnerError::Failed(Failure::Exit(None))));
        fs::set_permissions(&stand.tools.program, fs::Permissions::from_mode(0o644)).unwrap();
        let found = stand.call(ImpactAction::Status, &CancelToken::new());
        assert_eq!(
            found,
            Err(RunnerError::Failed(Failure::Io(
                io::ErrorKind::PermissionDenied
            )))
        );
    }

    /// What the stand-in of the next two tests saw: its arguments, its environment and its folder.
    const RECORD: &str = "printf '%s\\n' \"$@\" > \"$here/args\"\n/usr/bin/env > \"$here/env\"\npwd -P > \"$here/cwd\"\n/bin/cat \"$here/answer.json\"";

    /// The variables the stand-in saw, without the ones a shell adds for itself.
    fn seen(stand: &Stand) -> Vec<(String, String)> {
        let mut seen: Vec<(String, String)> = fs::read_to_string(stand.bin("env"))
            .unwrap()
            .lines()
            .filter_map(|line| line.split_once('='))
            .filter(|(name, _)| !["PWD", "OLDPWD", "SHLVL", "_"].contains(name))
            .map(|(name, value)| (name.to_string(), value.to_string()))
            .collect();
        seen.sort();
        seen
    }

    fn session(stand: &mut Stand) {
        let path = std::env::join_paths([
            stand.root.join("bin"),
            PathBuf::from("relative"),
            PathBuf::from("/usr/bin"),
            PathBuf::from("/bin"),
        ])
        .unwrap();
        stand.env.set("PATH", path);
        stand.env.set("HOME", "/sample-home");
        stand.env.set("TMPDIR", "/sample-tmp");
        stand.env.set("GIT_CONFIG_GLOBAL", "/sample-home/gitconfig");
        stand.env.set("XDG_CONFIG_HOME", "");
        stand.env.set("OAH_STATE_DIR", "state");
        for hidden in [
            "GIT_DIR",
            "GIT_EXEC_PATH",
            "GIT_SSH_COMMAND",
            "PYTHONPATH",
            "PYTHONHOME",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "_MEIPASS2",
            "_PYI_ARCHIVE_FILE",
            "LANG",
            "LC_ALL",
            "OAH_HOME",
            "SAMPLE_API_KEY",
            "SSH_AUTH_SOCK",
        ] {
            stand.env.set(hidden, "hidden");
        }
    }

    #[test]
    fn the_tool_gets_fixed_arguments_the_allowed_environment_and_its_own_folder() {
        let mut stand = Stand::new(&versioned(RECORD));
        stand.answer(&answer("refresh", &stand.root));
        session(&mut stand);
        stand
            .call(ImpactAction::Refresh, &CancelToken::new())
            .unwrap();

        let args = fs::read_to_string(stand.bin("args")).unwrap();
        assert_eq!(
            args,
            format!("impact\nrefresh\n--root\n{}\n", stand.root.display())
        );
        let state = stand.dir.path().join("state").display().to_string();
        let expected = [
            ("GIT_CONFIG_GLOBAL", "/sample-home/gitconfig"),
            ("HOME", "/sample-home"),
            ("OAH_STATE_DIR", state.as_str()),
            ("PATH", "/usr/bin:/bin"),
            ("TMPDIR", "/sample-tmp"),
        ]
        .map(|(name, value)| (name.to_string(), value.to_string()));
        assert_eq!(seen(&stand), expected);
        let cwd = fs::read_to_string(stand.bin("cwd")).unwrap();
        assert_eq!(Path::new(cwd.trim_end()), stand.bin("x").parent().unwrap());
    }

    #[test]
    fn a_trusted_git_is_passed_on_and_then_the_tool_gets_no_path() {
        let mut stand = Stand::new(&versioned(RECORD));
        stand.answer(&answer("status", &stand.root));
        session(&mut stand);
        let request = ImpactRequest {
            action: ImpactAction::Status,
            root: &stand.root,
            git: Some(Path::new("/sample-git/git")),
        };
        stand
            .tools
            .impact_in(
                &stand.env,
                &CancelToken::new(),
                &crate::tasks::NoProgress,
                &request,
            )
            .unwrap();
        let args = fs::read_to_string(stand.bin("args")).unwrap();
        let root = stand.root.display();
        assert_eq!(
            args,
            format!("impact\nstatus\n--root\n{root}\n--git\n/sample-git/git\n")
        );
        assert!(seen(&stand).iter().all(|(name, _)| name != "PATH"));
    }

    #[test]
    fn a_status_answer_comes_back_checked_with_its_progress() {
        let dir = tempfile::tempdir().unwrap();
        let stand = Stand::answering(0, &answer("status", dir.path()));
        stand.answer(&answer("status", &stand.root));
        let request = ImpactRequest {
            action: ImpactAction::Status,
            root: &stand.root,
            git: None,
        };
        let steps = Steps::default();
        let reply = stand
            .tools
            .impact_in(&stand.env, &CancelToken::new(), &steps, &request)
            .unwrap();
        assert_eq!(
            (reply.state, reply.performed, reply.refusal),
            (MapState::Fresh, Performed::None, None)
        );
        assert_eq!(reply.head.as_deref(), Some(HEAD));
        let phase = "impact-status";
        assert_eq!(
            *steps.0.lock().unwrap(),
            [
                (phase, 0, Some(2)),
                (phase, 1, Some(2)),
                (phase, 2, Some(2))
            ]
        );
    }

    #[test]
    fn a_copy_staged_under_a_platform_name_is_the_tool_too() {
        let stand = Stand::answering(0, &json!(null));
        stand.answer(&answer("status", &stand.root));
        let staged = stand.bin("oah-project-tools-macos");
        fs::copy(&stand.tools.program, &staged).unwrap();
        settle(&staged);
        let request = ImpactRequest {
            action: ImpactAction::Status,
            root: &stand.root,
            git: None,
        };
        let reply = ProjectTools::new(staged).impact_in(
            &stand.env,
            &CancelToken::new(),
            &crate::tasks::NoProgress,
            &request,
        );
        assert_eq!(reply.map(|reply| reply.state), Ok(MapState::Fresh));
    }

    #[test]
    fn a_refusal_of_the_tool_comes_back_as_an_answer() {
        let dir = tempfile::tempdir().unwrap();
        let stand = Stand::answering(69, &answer("refresh", dir.path()));
        let mut document = answer("refresh", &stand.root);
        document["ok"] = json!(false);
        document["state"] = json!("not_a_repository");
        document["error"] = json!({"code": "OAH_NOT_A_REPO", "message": "a text with /a/path"});
        stand.answer(&document);
        let reply = stand
            .call(ImpactAction::Refresh, &CancelToken::new())
            .unwrap();
        assert_eq!(reply.refusal.as_deref(), Some("OAH_NOT_A_REPO"));
        assert_eq!(reply.state, MapState::NotARepository);
    }

    #[test]
    fn every_status_asks_the_tool_again() {
        let stand = Stand::new(&versioned(
            "echo asked >> \"$here/asked\"\n/bin/cat \"$here/answer.json\"",
        ));
        let mut document = answer("status", &stand.root);
        document["state"] = json!("stale");
        stand.answer(&document);
        let first = stand
            .call(ImpactAction::Status, &CancelToken::new())
            .unwrap();
        document["state"] = json!("fresh");
        stand.answer(&document);
        let second = stand
            .call(ImpactAction::Status, &CancelToken::new())
            .unwrap();
        assert_eq!(
            (first.state, second.state),
            (MapState::Stale, MapState::Fresh)
        );
        assert_eq!(
            fs::read_to_string(stand.bin("asked")).unwrap(),
            "asked\nasked\n"
        );
    }

    #[test]
    fn output_past_a_cap_stops_the_tool() {
        let line = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        for stream in ["", " >&2"] {
            let stand = Stand::new(&versioned(&format!(
                "while :; do echo {line}{stream}; done"
            )));
            let started = Instant::now();
            let found = stand.call(ImpactAction::Status, &CancelToken::new());
            assert_eq!(found, Err(RunnerError::Output), "{stream}");
            assert!(started.elapsed() < Duration::from_secs(60));
        }
    }

    #[test]
    fn a_run_past_its_deadline_is_stopped_and_only_a_refresh_may_have_saved() {
        let stand = Stand::new(&versioned("/bin/sleep 30")).within(Duration::from_millis(300));
        for (action, may_have_changed) in
            [(ImpactAction::Refresh, true), (ImpactAction::Status, false)]
        {
            let started = Instant::now();
            let found = stand.call(action, &CancelToken::new());
            assert_eq!(found, Err(RunnerError::TimedOut { may_have_changed }));
            assert!(started.elapsed() < Duration::from_secs(25));
        }
    }

    #[test]
    fn a_cancelled_refresh_is_stopped_and_may_have_saved() {
        let stand = Stand::new(&versioned("echo ready > \"$here/ready\"\n/bin/sleep 30"));
        let cancel = CancelToken::new();
        let canceller = cancel_once(stand.bin("ready"), &cancel);
        let started = Instant::now();
        let found = stand.call(ImpactAction::Refresh, &cancel);
        canceller.join().unwrap();
        assert_eq!(
            found,
            Err(RunnerError::Cancelled {
                may_have_changed: true
            })
        );
        assert!(found.unwrap_err().is_cancelled());
        assert!(started.elapsed() < Duration::from_secs(25));
    }

    #[test]
    fn a_cancellation_during_the_version_check_changed_nothing() {
        let stand = Stand::new(
            "if [ \"$1\" = --version ]; then echo ready > \"$here/ready\"; /bin/sleep 30; fi\necho ran > \"$here/ran\"",
        );
        let cancel = CancelToken::new();
        let canceller = cancel_once(stand.bin("ready"), &cancel);
        let found = stand.call(ImpactAction::Refresh, &cancel);
        canceller.join().unwrap();
        assert_eq!(
            found,
            Err(RunnerError::Cancelled {
                may_have_changed: false
            })
        );
        assert!(!stand.bin("ran").exists());
    }

    #[test]
    fn a_cancellation_before_the_start_runs_nothing() {
        let stand = Stand::new(&format!("echo ran >> \"$here/ran\"\n{}", versioned("")));
        let cancel = CancelToken::new();
        cancel.cancel();
        let found = stand.call(ImpactAction::Refresh, &cancel);
        assert_eq!(
            found,
            Err(RunnerError::Cancelled {
                may_have_changed: false
            })
        );
        assert!(!stand.bin("ran").exists());
    }
}
