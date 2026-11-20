// 실제 확인 통로 시험: 설정한 명령이 PATH 찾기를 어떻게 대신하는지와, 가짜 실행 파일의 종료 코드·출력·시간 초과가 그대로 옮겨지는지 본다.
use super::Configured;
#[cfg(unix)]
use super::SystemProbe;
use crate::agents::capability::Os;
use crate::agents::probe::{Probe, Resolution, Resolved, RunError, RunOutput};
use crate::paths::Env;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

/// Answers lookups from a list and records every name asked.
struct Stub {
    env: Env,
    answers: Vec<(&'static str, Resolution)>,
    asked: Mutex<Vec<String>>,
}

impl Stub {
    fn new(answers: Vec<(&'static str, Resolution)>) -> Stub {
        Stub {
            env: Env::new(std::env::temp_dir().join("invocation")).unwrap(),
            answers,
            asked: Mutex::new(Vec::new()),
        }
    }

    fn asked(&self) -> Vec<String> {
        self.asked.lock().unwrap().clone()
    }
}

impl Probe for Stub {
    fn env(&self) -> &Env {
        &self.env
    }

    fn os(&self) -> Os {
        Os::Macos
    }

    fn resolve_in(&self, command: &str, folder: &Path) -> Resolution {
        self.asked
            .lock()
            .unwrap()
            .push(format!("{command} in {}", folder.display()));
        Resolution::Missing
    }

    fn resolve(&self, command: &str) -> Resolution {
        self.asked.lock().unwrap().push(command.to_string());
        let answer = self.answers.iter().find(|(name, _)| *name == command);
        answer.map_or(Resolution::Missing, |(_, resolution)| resolution.clone())
    }

    fn run(&self, _: &Resolved, _: &[&str], _: Duration) -> Result<RunOutput, RunError> {
        Err(RunError::Launch(String::from("the stub runs nothing")))
    }

    fn exists(&self, _: &Path) -> bool {
        false
    }
}

fn program(path: &str, extra: &[&str]) -> Resolution {
    let mut argv = vec![OsString::from(path)];
    argv.extend(extra.iter().map(OsString::from));
    Resolution::Found(Resolved {
        argv,
        source: PathBuf::from(path),
    })
}

fn strings(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| part.to_string()).collect()
}

#[test]
fn other_commands_reach_the_inner_probe_unchanged() {
    let stub = Stub::new(vec![("codex", program("/bin/codex", &[]))]);
    let command = strings(&["/opt/claude"]);
    let probe = Configured {
        inner: &stub,
        name: "claude",
        command: &command,
    };
    assert_eq!(probe.resolve("codex"), program("/bin/codex", &[]));
    assert_eq!(stub.asked(), ["codex"]);
}

#[test]
fn a_bare_configured_name_is_looked_up_and_keeps_its_arguments() {
    let stub = Stub::new(vec![("node", program("/bin/node", &[]))]);
    let command = strings(&["node", "/opt/agent/cli.js"]);
    let probe = Configured {
        inner: &stub,
        name: "claude",
        command: &command,
    };
    assert_eq!(
        probe.resolve("claude"),
        program("/bin/node", &["/opt/agent/cli.js"])
    );
    assert_eq!(stub.asked(), ["node"]);
}

#[test]
fn a_configured_path_is_used_as_written_from_the_invocation_folder() {
    let stub = Stub::new(vec![("claude", program("/bin/claude", &[]))]);
    let command = strings(&["tools/claude", "--flag"]);
    let probe = Configured {
        inner: &stub,
        name: "claude",
        command: &command,
    };
    let expected = stub.env.cwd().join("tools/claude");
    let Resolution::Found(found) = probe.resolve("claude") else {
        panic!("a configured path must resolve to itself");
    };
    assert_eq!(
        found.argv,
        [expected.clone().into_os_string(), OsString::from("--flag")]
    );
    assert_eq!(found.source, expected);
    assert!(
        stub.asked().is_empty(),
        "a configured path is never looked up"
    );
}

#[test]
fn a_configured_batch_file_is_refused_on_every_system() {
    let stub = Stub::new(Vec::new());
    let command = strings(&["tools\\Agent.CMD"]);
    let probe = Configured {
        inner: &stub,
        name: "codex",
        command: &command,
    };
    let expected = Resolution::UnsupportedShim {
        path: stub.env.cwd().join("tools\\Agent.CMD"),
        reason: String::from("batch files are never launched directly"),
    };
    assert_eq!(probe.resolve("codex"), expected);
}

#[test]
fn a_configured_name_missing_from_path_is_missing() {
    let stub = Stub::new(Vec::new());
    let command = strings(&["node", "cli.js"]);
    let probe = Configured {
        inner: &stub,
        name: "gemini",
        command: &command,
    };
    assert_eq!(probe.resolve("gemini"), Resolution::Missing);
}

#[cfg(unix)]
mod system {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// A synthetic program in a fresh folder that prints its arguments and a line on stderr, then runs `tail`.
    fn fake(tail: &str) -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("fake-agent");
        let script = format!("#!/bin/sh\nprintf '%s|' \"$@\"\necho on-stderr >&2\n{tail}\n");
        std::fs::write(&path, script).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        (dir, path)
    }

    fn env() -> Env {
        Env::new(std::env::temp_dir())
            .unwrap()
            .with("PATH", "/usr/bin:/bin")
    }

    fn resolved(path: &Path, fixed: &[&str]) -> Resolved {
        let mut argv = vec![path.as_os_str().to_os_string()];
        argv.extend(fixed.iter().map(OsString::from));
        Resolved {
            argv,
            source: path.to_path_buf(),
        }
    }

    #[test]
    fn the_resolved_arguments_come_before_the_run_arguments() {
        let (_dir, path) = fake("exit 3");
        let env = env();
        let output = SystemProbe::new(&env)
            .run(
                &resolved(&path, &["fixed one"]),
                &["--version"],
                Duration::from_secs(10),
            )
            .unwrap();
        assert_eq!(output.exit, Some(3));
        assert_eq!(output.stdout, "fixed one|--version|");
        assert_eq!(output.stderr, "on-stderr\n");
        let debug = format!("{output:?}");
        assert!(
            !debug.contains("fixed one") && !debug.contains("on-stderr"),
            "{debug}"
        );
    }

    #[test]
    fn a_run_past_its_deadline_is_timed_out() {
        let (_dir, path) = fake("exec sleep 5");
        let env = env();
        let result =
            SystemProbe::new(&env).run(&resolved(&path, &[]), &[], Duration::from_millis(200));
        assert_eq!(result, Err(RunError::TimedOut));
    }

    #[test]
    fn a_program_that_cannot_start_is_a_launch_failure() {
        let (dir, _) = fake("exit 0");
        let env = env();
        let probe = SystemProbe::new(&env);
        let missing = resolved(&dir.path().join("absent"), &[]);
        assert!(matches!(
            probe.run(&missing, &[], Duration::from_secs(1)),
            Err(RunError::Launch(_))
        ));
        let empty = Resolved {
            argv: Vec::new(),
            source: dir.path().to_path_buf(),
        };
        let refused = Err(RunError::Launch(String::from("there is no program to run")));
        assert_eq!(probe.run(&empty, &[], Duration::from_secs(1)), refused);
    }
}
