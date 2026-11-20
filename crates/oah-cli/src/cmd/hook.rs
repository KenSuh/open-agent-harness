// 옛 hook 답: v0.4 훅이 부르는 oah hook <kind> 에 아무것도 읽거나 쓰지 않고 0 으로 끝내며, 그 훅이 돌지 않았다고 표준 오류에 한 줄 쓴다.
use clap::{Arg, ArgMatches, Command, value_parser};
use std::ffi::OsString;
use std::process::ExitCode;

/// The v0.4.0 hook entry (v0.4:src/oah/hooks.py:63): `oah hook <kind> [args…]`, hidden from help (§12.1 Q8).
pub fn command() -> Command {
    Command::new("hook")
        .hide(true)
        .arg(
            Arg::new("kind")
                .required(true)
                .value_parser(value_parser!(OsString)),
        )
        .arg(
            Arg::new("args")
                .num_args(0..)
                .trailing_var_arg(true)
                .allow_hyphen_values(true)
                .value_parser(value_parser!(OsString)),
        )
}

/// Exit 0 with one stderr line (CONTRACTS C5): the 0.4 hook did not run. Nothing is read or written.
pub fn run(matches: &ArgMatches) -> ExitCode {
    let kind = matches
        .get_one::<OsString>("kind")
        .map(|kind| kind.to_string_lossy().into_owned())
        .unwrap_or_default();
    eprintln!("oah: the 0.4 hook {kind:?} did not run; OAH 1.0 does not run 0.4 hooks");
    ExitCode::SUCCESS
}
