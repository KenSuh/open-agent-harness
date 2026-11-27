// 프로세스 트리 실행 시험: 종료 상태와 출력, 환경과 폴더, 마감·취소·출력 상한, 그리고 그룹이나 Job 에 남은 자식까지 끝나는지를 실제 프로세스로 확인한다.
//! Tests of the tree runner with real processes: `/bin/sh` on Unix, `cmd.exe` on Windows.

use super::*;
use std::fs;

#[cfg(unix)]
mod unix {
    use super::*;

    const GRACE: Duration = Duration::from_millis(300);
    /// Long enough after a child's one or two seconds that a missing mark means the child was stopped.
    const AFTER_THE_MARK: Duration = Duration::from_millis(2500);

    /// Runs `script` with `/bin/sh` in `dir`, which is also where `$MARK` points.
    fn sh(
        dir: &Path,
        script: &str,
        deadline: Duration,
        limit: usize,
        cancel: &CancelToken,
    ) -> Result<Output, TreeError> {
        let env = [
            (OsString::from("PATH"), OsString::from("/usr/bin:/bin")),
            (OsString::from("MARK"), dir.join("mark").into_os_string()),
        ];
        run_tree(
            &TreeRun {
                program: Path::new("/bin/sh"),
                args: &[OsStr::new("-c"), OsStr::new(script)],
                env: &env,
                cwd: dir,
                deadline,
                stdout_limit: limit,
                stderr_limit: limit,
                grace: GRACE,
            },
            cancel,
        )
    }

    fn quick(script: &str) -> Result<Output, TreeError> {
        let dir = tempfile::tempdir().unwrap();
        sh(
            dir.path(),
            script,
            Duration::from_secs(30),
            4096,
            &CancelToken::new(),
        )
    }

    #[test]
    fn a_finished_run_returns_its_status_and_both_streams() {
        let output = quick("echo out; echo err >&2; exit 3").unwrap();
        assert_eq!(output.status.code(), Some(3));
        assert_eq!(output.stdout, b"out\n");
        assert_eq!(output.stderr, b"err\n");
    }

    #[test]
    fn input_over_a_pipe_arrives_in_full_and_a_stalled_writer_stops() {
        let dir = tempfile::tempdir().unwrap();
        let read = TreeRun {
            program: Path::new("/bin/cat"),
            args: &[],
            env: &[],
            cwd: dir.path(),
            deadline: Duration::from_secs(10),
            stdout_limit: 65_536,
            stderr_limit: 256,
            grace: GRACE,
        };
        let data = vec![b'x'; 60_000];
        let cancel = CancelToken::new();
        let output = run_tree_input(&read, &cancel, &data).unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout, data);
        let stalled = TreeRun {
            program: Path::new("/bin/sleep"),
            args: &[OsStr::new("60")],
            deadline: Duration::from_millis(200),
            ..read
        };
        assert!(matches!(
            run_tree_input(&stalled, &cancel, &vec![b'x'; 262_144]),
            Err(TreeError::Timeout)
        ));
    }

    #[test]
    fn the_child_sees_only_the_given_environment_and_runs_in_the_given_folder() {
        let dir = tempfile::tempdir().unwrap();
        let folder = fs::canonicalize(dir.path()).unwrap();
        let env = [
            (OsString::from("PATH"), OsString::from("/usr/bin:/bin")),
            (OsString::from("OAH_SAMPLE"), OsString::from("given")),
        ];
        let output = run_tree(
            &TreeRun {
                program: Path::new("/bin/sh"),
                args: &[
                    OsStr::new("-c"),
                    OsStr::new("echo \"$OAH_SAMPLE:${HOME:-unset}:${CARGO:-unset}\"; pwd -P"),
                ],
                env: &env,
                cwd: &folder,
                deadline: Duration::from_secs(30),
                stdout_limit: 4096,
                stderr_limit: 4096,
                grace: GRACE,
            },
            &CancelToken::new(),
        )
        .unwrap();
        let expected = format!("given:unset:unset\n{}\n", folder.display());
        assert_eq!(String::from_utf8_lossy(&output.stdout), expected);
    }

    #[test]
    fn a_relative_program_is_refused_before_anything_starts() {
        let dir = tempfile::tempdir().unwrap();
        let refused = run_tree(
            &TreeRun {
                program: Path::new("sh"),
                args: &[],
                env: &[],
                cwd: dir.path(),
                deadline: Duration::from_secs(1),
                stdout_limit: 64,
                stderr_limit: 64,
                grace: GRACE,
            },
            &CancelToken::new(),
        );
        assert!(
            matches!(refused, Err(TreeError::NotAbsolute)),
            "{refused:?}"
        );
    }

    #[test]
    fn a_missing_program_is_a_spawn_failure() {
        let dir = tempfile::tempdir().unwrap();
        let failed = run_tree(
            &TreeRun {
                program: Path::new("/nonexistent-oah-sample/tool"),
                args: &[],
                env: &[],
                cwd: dir.path(),
                deadline: Duration::from_secs(1),
                stdout_limit: 64,
                stderr_limit: 64,
                grace: GRACE,
            },
            &CancelToken::new(),
        );
        assert!(
            matches!(&failed, Err(TreeError::Spawn(error)) if error.kind() == io::ErrorKind::NotFound),
            "{failed:?}"
        );
    }

    #[test]
    fn a_run_past_its_deadline_is_stopped_with_the_children_in_its_group() {
        let dir = tempfile::tempdir().unwrap();
        let started = Instant::now();
        let failed = sh(
            dir.path(),
            "(sleep 1; echo late > \"$MARK\") & sleep 30",
            Duration::from_millis(200),
            4096,
            &CancelToken::new(),
        );
        assert!(matches!(failed, Err(TreeError::Timeout)), "{failed:?}");
        assert!(started.elapsed() < Duration::from_secs(20));
        std::thread::sleep(AFTER_THE_MARK);
        assert!(
            !dir.path().join("mark").exists(),
            "a child outlived the stop"
        );
    }

    #[test]
    fn a_child_left_behind_neither_delays_a_finished_run_nor_survives_it() {
        let dir = tempfile::tempdir().unwrap();
        let started = Instant::now();
        // The background child keeps both pipes open for 30 seconds.
        let output = sh(
            dir.path(),
            "(sleep 1; echo late > \"$MARK\"; sleep 30) & echo started",
            Duration::from_secs(60),
            4096,
            &CancelToken::new(),
        )
        .unwrap();
        assert_eq!(output.stdout, b"started\n");
        assert!(output.status.success());
        assert!(started.elapsed() < Duration::from_secs(20));
        std::thread::sleep(AFTER_THE_MARK);
        assert!(
            !dir.path().join("mark").exists(),
            "a child outlived the run"
        );
    }

    #[test]
    fn output_past_the_limit_stops_the_run() {
        let dir = tempfile::tempdir().unwrap();
        let started = Instant::now();
        let failed = sh(
            dir.path(),
            "while :; do echo 0123456789; done",
            Duration::from_secs(60),
            1024,
            &CancelToken::new(),
        );
        assert!(matches!(failed, Err(TreeError::TooLarge)), "{failed:?}");
        assert!(started.elapsed() < Duration::from_secs(20));
    }

    #[test]
    fn output_up_to_the_limit_is_returned_whole() {
        let dir = tempfile::tempdir().unwrap();
        let output = sh(
            dir.path(),
            "printf 0123456789abcdef",
            Duration::from_secs(30),
            16,
            &CancelToken::new(),
        )
        .unwrap();
        assert_eq!(output.stdout, b"0123456789abcdef");
    }

    #[test]
    fn a_cancelled_run_is_stopped_with_its_children() {
        let dir = tempfile::tempdir().unwrap();
        let cancel = CancelToken::new();
        let from_outside = cancel.clone();
        let canceller = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(200));
            from_outside.cancel();
        });
        let started = Instant::now();
        let failed = sh(
            dir.path(),
            "(sleep 1; echo late > \"$MARK\") & sleep 30",
            Duration::from_secs(60),
            4096,
            &cancel,
        );
        canceller.join().unwrap();
        assert!(matches!(failed, Err(TreeError::Cancelled)), "{failed:?}");
        assert!(started.elapsed() < Duration::from_secs(20));
        std::thread::sleep(AFTER_THE_MARK);
        assert!(
            !dir.path().join("mark").exists(),
            "a child outlived the cancellation"
        );
    }

    #[test]
    fn a_stopped_tree_is_asked_to_end_before_it_is_killed() {
        let dir = tempfile::tempdir().unwrap();
        let failed = sh(
            dir.path(),
            "trap 'echo asked > \"$MARK\"; exit 0' TERM; echo ready; sleep 30 & wait",
            Duration::from_millis(500),
            4096,
            &CancelToken::new(),
        );
        assert!(matches!(failed, Err(TreeError::Timeout)), "{failed:?}");
        assert_eq!(
            fs::read_to_string(dir.path().join("mark")).unwrap(),
            "asked\n"
        );
    }

    #[test]
    fn a_tree_that_ignores_the_request_is_killed_after_the_grace() {
        let dir = tempfile::tempdir().unwrap();
        let started = Instant::now();
        let failed = sh(
            dir.path(),
            "trap '' TERM; (sleep 2; echo late > \"$MARK\") & sleep 30",
            Duration::from_millis(200),
            4096,
            &CancelToken::new(),
        );
        assert!(matches!(failed, Err(TreeError::Timeout)), "{failed:?}");
        let elapsed = started.elapsed();
        assert!(elapsed >= Duration::from_millis(200) + GRACE, "{elapsed:?}");
        assert!(elapsed < Duration::from_secs(20), "{elapsed:?}");
        std::thread::sleep(AFTER_THE_MARK);
        assert!(
            !dir.path().join("mark").exists(),
            "a child outlived the kill"
        );
    }
}

#[cfg(windows)]
mod windows {
    use super::*;
    use std::path::PathBuf;

    /// Long enough after the child's two seconds that a missing mark means the child was stopped.
    const AFTER_THE_MARK: Duration = Duration::from_secs(5);

    fn system32(name: &str) -> PathBuf {
        let root = std::env::var_os("SystemRoot").expect("SystemRoot is set on Windows");
        PathBuf::from(root).join("System32").join(name)
    }

    /// Runs `cmd.exe /D /C <args>` in `dir` with only `SystemRoot` set.
    fn cmd(
        dir: &Path,
        args: &[&OsStr],
        deadline: Duration,
        cancel: &CancelToken,
    ) -> Result<Output, TreeError> {
        let root = std::env::var_os("SystemRoot").expect("SystemRoot is set on Windows");
        let env = [(OsString::from("SystemRoot"), root)];
        let mut all = vec![OsStr::new("/D"), OsStr::new("/C")];
        all.extend_from_slice(args);
        run_tree(
            &TreeRun {
                program: &system32("cmd.exe"),
                args: &all,
                env: &env,
                cwd: dir,
                deadline,
                stdout_limit: 4096,
                stderr_limit: 4096,
                grace: Duration::from_millis(300),
            },
            cancel,
        )
    }

    #[test]
    fn a_finished_run_returns_its_status_and_output() {
        let dir = tempfile::tempdir().unwrap();
        let printed = cmd(
            dir.path(),
            &[OsStr::new("echo"), OsStr::new("out")],
            Duration::from_secs(60),
            &CancelToken::new(),
        )
        .unwrap();
        assert!(printed.status.success());
        assert_eq!(printed.stdout, b"out\r\n");
        let ended = cmd(
            dir.path(),
            &[OsStr::new("exit"), OsStr::new("3")],
            Duration::from_secs(60),
            &CancelToken::new(),
        )
        .unwrap();
        assert_eq!(ended.status.code(), Some(3));
    }

    #[test]
    fn a_run_past_its_deadline_is_stopped() {
        let dir = tempfile::tempdir().unwrap();
        let ping = system32("PING.EXE");
        let started = Instant::now();
        let failed = cmd(
            dir.path(),
            &[
                ping.as_os_str(),
                OsStr::new("-n"),
                OsStr::new("60"),
                OsStr::new("127.0.0.1"),
            ],
            Duration::from_millis(500),
            &CancelToken::new(),
        );
        assert!(matches!(failed, Err(TreeError::Timeout)), "{failed:?}");
        assert!(started.elapsed() < Duration::from_secs(30));
    }

    #[test]
    fn a_cancelled_run_is_stopped() {
        let dir = tempfile::tempdir().unwrap();
        let ping = system32("PING.EXE");
        let cancel = CancelToken::new();
        let from_outside = cancel.clone();
        let canceller = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(500));
            from_outside.cancel();
        });
        let started = Instant::now();
        let failed = cmd(
            dir.path(),
            &[
                ping.as_os_str(),
                OsStr::new("-n"),
                OsStr::new("60"),
                OsStr::new("127.0.0.1"),
            ],
            Duration::from_secs(120),
            &cancel,
        );
        canceller.join().unwrap();
        assert!(matches!(failed, Err(TreeError::Cancelled)), "{failed:?}");
        assert!(started.elapsed() < Duration::from_secs(30));
    }

    #[test]
    fn a_child_left_behind_neither_delays_a_finished_run_nor_survives_it() {
        let dir = tempfile::tempdir().unwrap();
        // `late.cmd` waits about two seconds, then writes the mark. `helper.cmd` starts it in the background, with
        // the same output pipes, and ends at once.
        fs::write(
            dir.path().join("late.cmd"),
            "@echo off\r\n\"%SystemRoot%\\System32\\PING.EXE\" -n 3 127.0.0.1 >NUL\r\necho late>\"%~dp0mark.txt\"\r\n",
        )
        .unwrap();
        let helper = dir.path().join("helper.cmd");
        fs::write(
            &helper,
            "@echo off\r\nstart \"\" /B \"%SystemRoot%\\System32\\cmd.exe\" /D /C \"%~dp0late.cmd\"\r\necho started\r\n",
        )
        .unwrap();
        let started = Instant::now();
        let output = cmd(
            dir.path(),
            &[helper.as_os_str()],
            Duration::from_secs(60),
            &CancelToken::new(),
        )
        .unwrap();
        assert!(output.stdout.starts_with(b"started"), "{:?}", output.stdout);
        assert!(started.elapsed() < Duration::from_secs(30));
        std::thread::sleep(AFTER_THE_MARK);
        assert!(
            !dir.path().join("mark.txt").exists(),
            "a child outlived the run"
        );
    }
}
