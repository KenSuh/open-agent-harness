// 확인 단계: 버전 줄 읽기와 로그인 판정(v0.4.0 그대로), 명령 찾기·버전 실행·상태 명령 실행을 doctor 와 어댑터가 함께 쓴다.
//! The probe steps `doctor` and the adapters share, with v0.4.0's `first_line` and `signed_in` as given.

use super::probe::{Probe, Resolution, Resolved, RunError};
use serde_json::Value;
use std::path::PathBuf;
use std::time::Duration;

/// The first non-blank line of `text` without ANSI escape sequences, trimmed and cut to 120 characters
/// (v0.4:src/oah/doctor.py:39-44).
pub fn first_line(text: &str) -> Option<String> {
    let plain = strip_ansi(text);
    plain
        .split(is_line_break)
        .map(|line| line.trim_matches(is_py_space))
        .find(|line| !line.is_empty())
        .map(|line| line.chars().take(120).collect())
}

/// Whether a sign-in status run means signed in: exit code 0, and a JSON object's `loggedIn` is `true` when it
/// has one; any other output counts as signed in (v0.4:src/oah/setup/guide.py:247-257).
pub fn signed_in(exit: Option<i32>, stdout: &str) -> bool {
    if exit != Some(0) {
        return false;
    }
    match serde_json::from_str::<Value>(stdout) {
        Ok(Value::Object(map)) => map
            .get("loggedIn")
            .is_none_or(|flag| *flag == Value::Bool(true)),
        _ => true,
    }
}

/// The first `v?N.N.N` in `text` (v0.4:src/oah/doctor.py:21); a part too large for `u64` saturates.
pub(crate) fn semver(text: &str) -> Option<[u64; 3]> {
    let bytes = text.as_bytes();
    (0..bytes.len())
        .filter(|&at| bytes[at].is_ascii_digit() && (at == 0 || !bytes[at - 1].is_ascii_digit()))
        .find_map(|at| triple(&bytes[at..]))
}

/// `N.N.N` at the start of `bytes`, each part a whole run of digits.
fn triple(bytes: &[u8]) -> Option<[u64; 3]> {
    let mut parts = [0u64; 3];
    let mut rest = bytes;
    for (index, part) in parts.iter_mut().enumerate() {
        if index > 0 {
            rest = rest.strip_prefix(b".")?;
        }
        let digits = rest.iter().take_while(|byte| byte.is_ascii_digit()).count();
        if digits == 0 {
            return None;
        }
        *part = rest[..digits].iter().fold(0u64, |value, digit| {
            value
                .saturating_mul(10)
                .saturating_add(u64::from(digit - b'0'))
        });
        rest = &rest[digits..];
    }
    Some(parts)
}

/// Removes `ESC [ [0-9;?]* <letter>` sequences, as v0.4.0's `ANSI` pattern does; anything else is kept.
fn strip_ansi(text: &str) -> String {
    let mut plain = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find('\x1b') {
        plain.push_str(&rest[..start]);
        let after = &rest[start + 1..];
        let body = after.strip_prefix('[').map(|body| {
            let params = body
                .bytes()
                .take_while(|byte| byte.is_ascii_digit() || *byte == b';' || *byte == b'?')
                .count();
            (body, params)
        });
        match body {
            Some((body, params))
                if body
                    .as_bytes()
                    .get(params)
                    .is_some_and(u8::is_ascii_alphabetic) =>
            {
                rest = &body[params + 1..];
            }
            _ => {
                plain.push('\x1b');
                rest = after;
            }
        }
    }
    plain.push_str(rest);
    plain
}

/// The line boundaries of Python's `str.splitlines`.
fn is_line_break(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\x0b'
            | '\x0c'
            | '\x1c'
            | '\x1d'
            | '\x1e'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

/// The characters Python's `str.strip` removes: Unicode white space plus the information separators.
fn is_py_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// How resolving a command and running its version arguments ended.
#[derive(Debug)]
pub(crate) enum VersionRun {
    Missing,
    UnsupportedShim {
        path: PathBuf,
        reason: String,
    },
    Launch {
        program: Resolved,
        reason: String,
    },
    TimedOut {
        program: Resolved,
    },
    /// It ran to the end; `version` is the first line of stdout, else of stderr.
    Exited {
        program: Resolved,
        exit: Option<i32>,
        version: Option<String>,
    },
}

/// Resolves `command` and runs it with `args` (v0.4:src/oah/doctor.py:80-96).
pub(crate) fn run_version(
    probe: &dyn Probe,
    command: &str,
    args: &[&str],
    deadline: Duration,
) -> VersionRun {
    let program = match probe.resolve(command) {
        Resolution::Missing => return VersionRun::Missing,
        Resolution::UnsupportedShim { path, reason } => {
            return VersionRun::UnsupportedShim { path, reason };
        }
        Resolution::Found(program) => program,
    };
    match probe.run(&program, args, deadline) {
        Err(RunError::Launch(reason)) => VersionRun::Launch { program, reason },
        Err(RunError::TimedOut) => VersionRun::TimedOut { program },
        Ok(output) => {
            let version = first_line(&output.stdout).or_else(|| first_line(&output.stderr));
            VersionRun::Exited {
                program,
                exit: output.exit,
                version,
            }
        }
    }
}

/// How a sign-in status run ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum SignInRun {
    SignedIn,
    SignedOut,
    Launch(String),
    TimedOut,
}

/// Runs a sign-in status command. Only the exit code and the `loggedIn` flag are read; the output is dropped
/// here, so no status text reaches a caller, a log or a kept value (v0.4:src/oah/doctor.py:104-117).
pub(crate) fn run_sign_in(
    probe: &dyn Probe,
    program: &Resolved,
    args: &[&str],
    deadline: Duration,
) -> SignInRun {
    match probe.run(program, args, deadline) {
        Err(RunError::Launch(reason)) => SignInRun::Launch(reason),
        Err(RunError::TimedOut) => SignInRun::TimedOut,
        Ok(output) if signed_in(output.exit, &output.stdout) => SignInRun::SignedIn,
        Ok(_) => SignInRun::SignedOut,
    }
}
