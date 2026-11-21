// 하위 명령 모음: doctor·sections·rules 와 옛 hook 답을 두고, 핵심 오류를 C5 모양(--json 이면 표준 출력의 JSON, 아니면 한 줄)으로 알린다.
pub mod doctor;
pub mod hook;
pub mod rules;
pub mod sections;

use oah_core::paths::Env;
use oah_core::{Coded, Error, ErrorDto};
use serde::Serialize;
use std::process::ExitCode;

/// A usage error (v0.4:src/oah/doctor.py:28).
pub const USAGE: u8 = 2;
/// A refusal with a typed `OAH_*` error.
pub const REFUSED: u8 = 1;

/// `{"error": {code, message, hint}}` (CONTRACTS C5).
#[derive(Serialize)]
struct Refusal<'a> {
    error: &'a ErrorDto,
}

/// Reports `error` the C5 way: with `--json` one JSON value on stdout, else `CODE: message` on stderr; exit 1.
pub fn refuse(env: &Env, error: &Error, json: bool) -> ExitCode {
    let dto = error.to_dto(env);
    if json {
        match serde_json::to_string_pretty(&Refusal { error: &dto }) {
            Ok(text) => println!("{text}"),
            Err(_) => eprintln!("{}: {}", error.code(), dto.message),
        }
    } else {
        eprintln!("{}: {}", dto.code, dto.message.replace('\n', " "));
    }
    ExitCode::from(REFUSED)
}

/// An environment for showing an error when the process's own cannot be read: no home, so paths stay whole.
pub fn fallback_env() -> Env {
    Env::new(std::env::temp_dir()).expect("the temporary folder is absolute")
}
