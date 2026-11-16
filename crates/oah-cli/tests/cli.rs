// oah 실행 파일을 직접 돌려 버전 출력과 사용법 오류를 확인하는 시험.
use std::ffi::OsStr;
use std::process::Command;

fn oah(args: &[impl AsRef<OsStr>]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_oah"))
        .args(args)
        .output()
        .expect("run oah")
}

#[test]
fn version_flag_prints_the_version() {
    let out = oah(&["--version"]);
    assert!(out.status.success());
    assert_eq!(String::from_utf8_lossy(&out.stdout).trim_end(), "oah 0.0.0");
}

#[test]
fn anything_else_prints_usage_and_exits_2() {
    for args in [&[][..], &["--help"], &["--version", "x"]] {
        let out = oah(args);
        assert_eq!(out.status.code(), Some(2), "args {args:?}");
        assert!(out.stdout.is_empty());
        let err = String::from_utf8_lossy(&out.stderr);
        assert_eq!(err.trim_end().lines().count(), 1);
        assert!(err.starts_with("usage:"));
    }
}

#[cfg(any(unix, windows))]
#[test]
fn non_unicode_argument_prints_usage_and_exits_2() {
    #[cfg(unix)]
    let argument = {
        use std::os::unix::ffi::OsStrExt;
        OsStr::from_bytes(b"\xff").to_owned()
    };
    #[cfg(windows)]
    let argument = {
        use std::os::windows::ffi::OsStringExt;
        std::ffi::OsString::from_wide(&[0xd800])
    };

    let out = oah(&[argument]);
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stdout.is_empty());
    assert_eq!(
        String::from_utf8_lossy(&out.stderr).trim_end(),
        "usage: oah --version"
    );
}
