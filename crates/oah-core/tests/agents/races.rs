// 겹친 확인 시험: 늦게 끝난 옛 확인이 새 결과를 덮지 않는지, 시작 전이나 도중에 취소한 확인이 아무것도 남기지 않는지 본다.
use crate::fake_agents::FakeProbe;
use crate::session::{CLAUDE_VERSION, claude, list, probe, req, session};
use oah_core::agents::{Os, Probe, Resolution, Resolved, RunError, RunOutput};
use oah_core::app;
use oah_core::config::AgentId;
use oah_core::dto::{AgentStatusDto, SignInDto};
use oah_core::paths::Env;
use oah_core::tasks::{CancelToken, NoProgress, Progress, ProgressSink};
use std::path::Path;
use std::sync::{Mutex, mpsc};
use std::time::Duration;

const WAIT: Duration = Duration::from_secs(5);

#[test]
fn overlapping_focus_requests_check_the_same_agent_sign_in_once() {
    let session = session();
    let older = claude(CLAUDE_VERSION, true);
    let newer = claude(CLAUDE_VERSION, false)
        .program("codex", "/bin/codex")
        .run("/bin/codex --version", Some(0), "codex-cli 0.163.0", "")
        .run("/bin/codex login status", Some(0), "Logged in", "");
    let one = req(&[AgentId::Claude], true, true);
    let two = req(&[AgentId::Claude, AgentId::Codex], true, true);
    assert_ne!(app::agents_probe_key(&one), app::agents_probe_key(&two));

    let (held, started, release) = hold_status(&older, &["auth", "status"]);
    let later = std::thread::scope(|scope| {
        let first = scope.spawn(|| probe(&session, one, &held));
        started.recv_timeout(WAIT).unwrap();
        let (send, recv) = mpsc::channel();
        let (session, newer) = (&session, &newer);
        scope.spawn(move || send.send(probe(session, two, newer)).unwrap());
        let later = recv.recv_timeout(WAIT);
        release.send(()).unwrap();
        first.join().unwrap();
        later.expect("the overlapping refresh finishes while the other status call is held")
    });

    let runs: Vec<_> = older.ran().into_iter().chain(newer.ran()).collect();
    let count = |command: &str| runs.iter().filter(|line| line.as_str() == command).count();
    assert_eq!(count("/bin/claude auth status"), 1, "per-agent throttle");
    assert_eq!(count("/bin/claude --version"), 2, "both requests detect");
    assert_eq!(count("/bin/codex --version"), 1, "the other agent detects");
    assert_eq!(
        count("/bin/codex login status"),
        1,
        "the other agent checks"
    );
    let claude_row = |rows: &[AgentStatusDto]| {
        rows.iter()
            .find(|row| row.id.0 == AgentId::Claude)
            .unwrap()
            .sign_in
    };
    assert_eq!(claude_row(&later), SignInDto::Unknown);
    assert_eq!(claude_row(&list(&session)), SignInDto::SignedIn);
    let repeat = claude(CLAUDE_VERSION, false);
    let rows = probe(&session, req(&[AgentId::Claude], true, true), &repeat);
    assert_eq!(repeat.ran(), ["/bin/claude --version"]);
    assert_eq!(claude_row(&rows), SignInDto::SignedIn);
}

struct FirstDetection(mpsc::Sender<()>);

impl ProgressSink for FirstDetection {
    fn progress(&self, progress: Progress) {
        if progress.done == 1 {
            self.0.send(()).unwrap();
        }
    }
}

enum Completion {
    InitialFocus,
    Manual,
    DeferredFocus,
}

fn overlapping_sign_ins(order: [Completion; 3]) -> [SignInDto; 3] {
    let session = session();
    let older = claude(CLAUDE_VERSION, true);
    let manual = claude(CLAUDE_VERSION, false);
    let newer = claude(CLAUDE_VERSION, true)
        .program("codex", "/bin/codex")
        .run("/bin/codex --version", Some(0), "codex-cli 0.163.0", "")
        .run("/bin/codex login status", Some(0), "Logged in", "");
    let (older_held, older_started, older_release) = hold_status(&older, &["auth", "status"]);
    let (manual_held, manual_started, manual_release) = hold_status(&manual, &["auth", "status"]);
    let (newer_held, codex_started, codex_release) = hold_status(&newer, &["login", "status"]);
    let (send, claude_finished) = mpsc::channel();
    let progress = FirstDetection(send);

    let completed = std::thread::scope(|scope| {
        let first =
            scope.spawn(|| probe(&session, req(&[AgentId::Claude], true, true), &older_held));
        older_started.recv_timeout(WAIT).unwrap();
        let second =
            scope.spawn(|| probe(&session, req(&[AgentId::Claude], true, false), &manual_held));
        manual_started.recv_timeout(WAIT).unwrap();
        let third = scope.spawn(|| {
            let cancel = CancelToken::new();
            let ctx = session.ctx(&cancel, &progress);
            let req = req(&[AgentId::Claude, AgentId::Codex], true, true);
            app::agents_probe_with(&ctx, req, &newer_held).unwrap()
        });
        claude_finished.recv_timeout(WAIT).unwrap();
        codex_started.recv_timeout(WAIT).unwrap();

        let mut initial_focus = Some((older_release, first));
        let mut manual = Some((manual_release, second));
        let mut deferred_focus = Some((codex_release, third));
        order.map(|completion| {
            let (release, running) = match completion {
                Completion::InitialFocus => initial_focus.take(),
                Completion::Manual => manual.take(),
                Completion::DeferredFocus => deferred_focus.take(),
            }
            .unwrap();
            release.send(()).unwrap();
            running.join().unwrap()[0].sign_in
        })
    });

    assert_eq!(list(&session)[0].sign_in, completed[2]);
    assert!(
        newer
            .ran()
            .iter()
            .all(|line| line != "/bin/claude auth status"),
        "the overlapping focus request reuses the pending check"
    );
    completed
}

#[test]
fn a_deferred_focus_result_cannot_replace_a_newer_manual_sign_in() {
    use Completion::{DeferredFocus, InitialFocus, Manual};
    assert_eq!(
        overlapping_sign_ins([Manual, DeferredFocus, InitialFocus]),
        [SignInDto::SignedOut; 3]
    );
}

#[test]
fn a_manual_result_updates_a_newer_deferred_focus_row_in_both_completion_orders() {
    use Completion::{DeferredFocus, InitialFocus, Manual};
    use SignInDto::{SignedIn, SignedOut, Unknown};
    assert_eq!(
        [
            overlapping_sign_ins([DeferredFocus, Manual, InitialFocus]),
            overlapping_sign_ins([DeferredFocus, InitialFocus, Manual]),
        ],
        [
            [Unknown, SignedOut, SignedOut],
            [Unknown, SignedIn, SignedOut]
        ]
    );
}

#[test]
fn a_manual_result_updates_a_newer_throttled_focus_row() {
    let session = session();
    probe(
        &session,
        req(&[AgentId::Claude], true, false),
        &claude(CLAUDE_VERSION, true),
    );
    let manual = claude(CLAUDE_VERSION, false);
    let focus = claude(CLAUDE_VERSION, true);
    let (held, started, release) = hold_status(&manual, &["auth", "status"]);
    let (focus_sign_in, manual_sign_in) = std::thread::scope(|scope| {
        let running = scope.spawn(|| probe(&session, req(&[AgentId::Claude], true, false), &held));
        started.recv_timeout(WAIT).unwrap();
        let focus_rows = probe(&session, req(&[AgentId::Claude], true, true), &focus);
        release.send(()).unwrap();
        (focus_rows[0].sign_in, running.join().unwrap()[0].sign_in)
    });

    assert_eq!(focus.ran(), ["/bin/claude --version"]);
    assert_eq!(focus_sign_in, SignInDto::SignedIn);
    assert_eq!(manual_sign_in, SignInDto::SignedOut);
    assert_eq!(list(&session)[0].sign_in, SignInDto::SignedOut);
}

#[test]
fn a_rejected_sign_in_does_not_throttle_focus() {
    let session = session();
    let older = claude(CLAUDE_VERSION, true);
    let updated = claude("2.2.0 (Claude Code)", false);
    let (held, started, release) = hold_status(&older, &["auth", "status"]);
    let late_sign_in = std::thread::scope(|scope| {
        let running = scope.spawn(|| probe(&session, req(&[AgentId::Claude], true, true), &held));
        started.recv_timeout(WAIT).unwrap();
        probe(&session, req(&[AgentId::Claude], false, false), &updated);
        release.send(()).unwrap();
        running.join().unwrap()[0].sign_in
    });
    assert_eq!(late_sign_in, SignInDto::Unknown);

    let rows = probe(&session, req(&[AgentId::Claude], true, true), &updated);
    assert_eq!(rows[0].sign_in, SignInDto::SignedOut);
    assert_eq!(
        updated.ran(),
        [
            "/bin/claude --version",
            "/bin/claude --version",
            "/bin/claude auth status"
        ]
    );
}

#[test]
fn a_cancelled_probe_publishes_nothing() {
    let session = session();
    let cancel = CancelToken::new();
    cancel.cancel();
    let ctx = session.ctx(&cancel, &NoProgress);
    let probe_claude = claude(CLAUDE_VERSION, true);
    assert!(
        app::agents_probe_with(&ctx, req(&[AgentId::Claude], true, false), &probe_claude).is_err()
    );
    assert!(probe_claude.ran().is_empty());
    assert!(list(&session).is_empty());
}

struct Held<'a> {
    inner: &'a FakeProbe,
    args: &'static [&'static str],
    started: mpsc::Sender<()>,
    release: Mutex<mpsc::Receiver<()>>,
}

fn hold_status<'a>(
    inner: &'a FakeProbe,
    args: &'static [&'static str],
) -> (Held<'a>, mpsc::Receiver<()>, mpsc::Sender<()>) {
    let (started, begun) = mpsc::channel();
    let (resume, release) = mpsc::channel();
    (
        Held {
            inner,
            args,
            started,
            release: Mutex::new(release),
        },
        begun,
        resume,
    )
}

impl Probe for Held<'_> {
    fn env(&self) -> &Env {
        self.inner.env()
    }
    fn os(&self) -> Os {
        self.inner.os()
    }
    fn resolve(&self, command: &str) -> Resolution {
        self.inner.resolve(command)
    }
    fn resolve_in(&self, command: &str, folder: &Path) -> Resolution {
        self.inner.resolve_in(command, folder)
    }
    fn run(
        &self,
        program: &Resolved,
        args: &[&str],
        deadline: Duration,
    ) -> Result<RunOutput, RunError> {
        if args == self.args {
            self.started.send(()).unwrap();
            self.release.lock().unwrap().recv_timeout(WAIT * 2).unwrap();
        }
        self.inner.run(program, args, deadline)
    }
    fn exists(&self, path: &Path) -> bool {
        self.inner.exists(path)
    }
}

#[test]
fn an_older_probe_that_finishes_last_does_not_replace_the_newer_status() {
    let session = session();
    let older = claude("1.0.0 (Claude Code)", true);
    let manual = claude(CLAUDE_VERSION, false);
    let (held, started, release) = hold_status(&older, &["auth", "status"]);
    let returned = std::thread::scope(|scope| {
        let first = scope.spawn(|| probe(&session, req(&[AgentId::Claude], true, true), &held));
        started.recv_timeout(WAIT).unwrap();
        probe(&session, req(&[AgentId::Claude], true, false), &manual);
        release.send(()).unwrap();
        first.join().unwrap()
    });
    assert_eq!(
        returned[0].version.as_deref(),
        Some("2.1.318 (Claude Code)"),
        "the older probe reports the held status"
    );
    assert_eq!(
        manual.ran(),
        ["/bin/claude --version", "/bin/claude auth status"]
    );
    assert_eq!(returned[0].sign_in, SignInDto::SignedOut);
    assert_eq!(
        list(&session)[0].version.as_deref(),
        Some("2.1.318 (Claude Code)")
    );
}

#[test]
fn a_probe_cancelled_while_it_runs_publishes_nothing() {
    let (session, cancel) = (session(), CancelToken::new());
    let inner = claude(CLAUDE_VERSION, true);
    let (held, started, release) = hold_status(&inner, &["auth", "status"]);
    let failed = std::thread::scope(|scope| {
        let running = scope.spawn(|| {
            let ctx = session.ctx(&cancel, &NoProgress);
            app::agents_probe_with(&ctx, req(&[AgentId::Claude], true, true), &held).is_err()
        });
        started.recv_timeout(WAIT).unwrap();
        cancel.cancel();
        release.send(()).unwrap();
        running.join().unwrap()
    });
    assert!(failed, "the probe ends cancelled");
    assert!(list(&session).is_empty());
    let retry = claude(CLAUDE_VERSION, false);
    let rows = probe(&session, req(&[AgentId::Claude], true, true), &retry);
    assert_eq!(
        retry.ran(),
        ["/bin/claude --version", "/bin/claude auth status"]
    );
    assert_eq!(rows[0].sign_in, SignInDto::SignedOut);
}
