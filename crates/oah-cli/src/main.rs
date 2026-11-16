// oah 명령줄 진입점: 지금은 --version 만 처리한다.
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() == 1 && args[0] == "--version" {
        println!("oah {}", oah_core::VERSION);
        ExitCode::SUCCESS
    } else {
        eprintln!("usage: oah --version");
        ExitCode::from(2)
    }
}
