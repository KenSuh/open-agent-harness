// 공식 설치 링크 정책: Tauri 내비게이션 경계에서 oah-core 의 일곱 공식 HTTPS 주소만 OS 기본 브라우저로 넘기고 나머지는 차단한다.
//! Native navigation policy for official agent install links (M1-S2 PACKET §9, desktop AC1).
//!
//! The desktop UI renders missing-agent install addresses as ordinary `<a href="…">` links without
//! `target="_blank"` and without adding an IPC command, capability, or crate dependency. This in-app
//! Tauri plugin intercepts webview navigation at `on_navigation`:
//!
//! - App origins (the bundled `tauri://localhost` / `http(s)://tauri.localhost` app or, in dev builds
//!   only, the configured `devUrl` origin) stay inside the webview (`true`).
//! - Exact matches against the seven canonical static `https://` URLs from `oah_core::agents::profile`
//!   cancel webview navigation (`false`) and hand the static catalog address — never renderer-supplied
//!   bytes, paths, or arguments — to the OS default browser (`/usr/bin/open` on macOS; `ShellExecuteW`
//!   on an STA-initialized worker thread off the WebView2 navigation callback on Windows). Because
//!   navigation is cancelled immediately, OAH stays responsive and focused on the same view when the
//!   user returns.
//! - Every other destination is rejected (`false`) without launching anything.

use oah_core::agents::profile;
use oah_core::config::AgentId;
use tauri::plugin::{Builder as PluginBuilder, TauriPlugin};
use tauri::{Manager, Url};

/// Witness that a parsed navigation URL matched one of `oah_core::agents::profile`'s seven canonical
/// HTTPS install URLs. Its inner string is always the static catalog constant, never renderer bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct CanonicalInstallUrl(&'static str);

impl CanonicalInstallUrl {
    /// Matches a parsed `Url` against the seven canonical HTTPS install URLs in `oah_core::agents::profile`.
    fn match_url(url: &Url) -> Option<Self> {
        if url.scheme() != "https" || !url.username().is_empty() || url.password().is_some() {
            return None;
        }
        let candidate = url.as_str();
        AgentId::ALL
            .into_iter()
            .map(|id| profile(id).install_url)
            .find(|&canonical| canonical.starts_with("https://") && canonical == candidate)
            .map(Self)
    }

    /// Returns the static catalog URL string from `oah_core::agents::profile`.
    const fn as_str(self) -> &'static str {
        self.0
    }
}

/// Returns `true` only for OAH's own bundled custom-protocol origin or (when supplied in dev builds)
/// the exact origin of `dev_url`.
fn is_app_url(url: &Url, dev_url: Option<&Url>, is_windows: bool) -> bool {
    if !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    if is_windows {
        // Windows production custom protocol: `http://tauri.localhost/...` (or `https://tauri.localhost/...`).
        if matches!(url.scheme(), "http" | "https")
            && url.host_str() == Some("tauri.localhost")
            && url.port().is_none()
        {
            return true;
        }
    } else {
        // macOS / Linux production custom protocol: `tauri://localhost/...`.
        if url.scheme() == "tauri" && url.host_str() == Some("localhost") && url.port().is_none() {
            return true;
        }
    }
    // Development server origin (only supplied when `tauri::is_dev()` is true).
    if let Some(dev) = dev_url
        && dev.origin().is_tuple()
        && url.origin() == dev.origin()
    {
        return true;
    }
    false
}

/// Reports an external browser handoff error in debug builds without logging any URI,
/// environment, or profile data.
fn report_handoff_failure<E>(outcome: Result<(), E>) {
    #[cfg(debug_assertions)]
    if outcome.is_err() {
        eprintln!("oah-desktop: external install link handoff failed");
    }
    #[cfg(not(debug_assertions))]
    drop(outcome);
}

/// Production navigation policy handler invoked by the Tauri `on_navigation` callback.
/// Returns `true` only when webview navigation may proceed inside OAH.
fn handle_navigation_for_os<E>(
    is_main_window: bool,
    url: &Url,
    dev_url: Option<&Url>,
    is_windows: bool,
    launcher: impl FnOnce(CanonicalInstallUrl) -> Result<(), E>,
) -> bool {
    if !is_main_window {
        return false;
    }
    if is_app_url(url, dev_url, is_windows) {
        return true;
    }
    if let Some(canonical) = CanonicalInstallUrl::match_url(url) {
        report_handoff_failure(launcher(canonical));
    }
    false
}

/// Registers the in-app navigation plugin using the platform's default OS browser launcher.
pub fn plugin<R: tauri::Runtime>() -> TauriPlugin<R> {
    plugin_with(open_in_browser)
}

/// Builds the in-app navigation plugin with an injectable launcher.
fn plugin_with<R: tauri::Runtime, F>(launcher: F) -> TauriPlugin<R>
where
    F: Fn(CanonicalInstallUrl) -> std::io::Result<()> + Send + 'static,
{
    PluginBuilder::new("oah-install-links")
        .on_navigation(move |webview, url| {
            let dev_url = if tauri::is_dev() {
                webview.app_handle().config().build.dev_url.as_ref()
            } else {
                None
            };
            handle_navigation_for_os(
                webview.label() == crate::MAIN_WINDOW,
                url,
                dev_url,
                cfg!(windows),
                &launcher,
            )
        })
        .build()
}

/// Opens a verified static canonical install URL in macOS's default browser via `/usr/bin/open`.
#[cfg(target_os = "macos")]
fn open_in_browser(target: CanonicalInstallUrl) -> std::io::Result<()> {
    let mut child = std::process::Command::new("/usr/bin/open")
        .arg(target.as_str())
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()?;
    // Reap `/usr/bin/open` off the webview callback thread so navigation cancels immediately and leaves no zombie.
    let _ = std::thread::Builder::new()
        .name("oah-open-reap".into())
        .spawn(move || {
            let _ = child.wait();
        });
    Ok(())
}

/// Encodes `value` as a NUL-terminated UTF-16 buffer for Windows wide-string APIs, refusing interior NULs.
#[cfg(any(windows, test))]
fn utf16_nul(value: &str) -> Option<Vec<u16>> {
    if value.contains('\0') {
        return None;
    }
    let mut wide: Vec<u16> = value.encode_utf16().collect();
    wide.push(0);
    Some(wide)
}

/// Owned NUL-terminated UTF-16 buffer with no interior NULs, guaranteed by construction.
#[cfg(any(windows, test))]
#[derive(Debug, PartialEq, Eq)]
struct Utf16Nul(Vec<u16>);

#[cfg(any(windows, test))]
impl Utf16Nul {
    fn encode(value: &str) -> Option<Self> {
        utf16_nul(value).map(Self)
    }

    #[cfg(windows)]
    fn as_ptr(&self) -> *const u16 {
        self.0.as_ptr()
    }

    #[cfg(test)]
    fn as_slice(&self) -> &[u16] {
        &self.0
    }
}

#[cfg(any(windows, test))]
const COINIT_APARTMENTTHREADED: u32 = 0x2;
#[cfg(any(windows, test))]
const COINIT_DISABLE_OLE1DDE: u32 = 0x4;
#[cfg(any(windows, test))]
const S_OK: i32 = 0;
#[cfg(any(windows, test))]
const S_FALSE: i32 = 1;
#[cfg(test)]
const RPC_E_CHANGED_MODE: i32 = 0x8001_0106_u32 as i32;

/// RAII guard for COM single-threaded apartment initialization on a Windows browser-handoff
/// worker thread. Constructed only when `CoInitializeEx` returns `S_OK` or `S_FALSE`, so drop
/// always pairs `uninit` 1:1 with successful STA initialization.
#[cfg(any(windows, test))]
struct ComApartmentGuard<U: FnOnce()> {
    uninit: Option<U>,
}

#[cfg(any(windows, test))]
impl<U: FnOnce()> ComApartmentGuard<U> {
    fn init(init_fn: impl FnOnce(u32) -> i32, uninit_fn: U) -> std::io::Result<Self> {
        let hr = init_fn(COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
        match hr {
            S_OK | S_FALSE => Ok(Self {
                uninit: Some(uninit_fn),
            }),
            _ => Err(std::io::Error::other(format!(
                "CoInitializeEx failed with HRESULT 0x{:08x}",
                hr as u32
            ))),
        }
    }
}

#[cfg(any(windows, test))]
impl<U: FnOnce()> Drop for ComApartmentGuard<U> {
    fn drop(&mut self) {
        if let Some(uninit) = self.uninit.take() {
            uninit();
        }
    }
}

/// Converts a `ShellExecuteW` `HINSTANCE` return value cast to `isize` into a `std::io::Result`.
#[cfg(any(windows, test))]
fn shell_execute_result(code: isize) -> std::io::Result<()> {
    if code > 32 {
        Ok(())
    } else {
        Err(std::io::Error::other(format!(
            "ShellExecuteW failed with code {code}"
        )))
    }
}

/// Dispatches a verified Windows install URL handoff to a named background worker thread so the
/// WebView2 `NavigationStarting` callback cancels navigation and returns immediately.
#[cfg(any(windows, test))]
fn dispatch_windows_handoff<F>(target: CanonicalInstallUrl, runner: F) -> std::io::Result<()>
where
    F: FnOnce(CanonicalInstallUrl) -> std::io::Result<()> + Send + 'static,
{
    std::thread::Builder::new()
        .name("oah-open-browser".into())
        .spawn(move || {
            report_handoff_failure(runner(target));
        })
        .map(|_| ())
}

/// Executes the Windows worker boundary for a verified `CanonicalInstallUrl`: performs checked
/// NUL-terminated UTF-16 encoding inside the worker, requires STA COM setup before invoking
/// `shell_exec`, and pairs COM cleanup exactly once with successful STA initialization.
#[cfg(any(windows, test))]
fn run_windows_handoff_worker<I, U, S>(
    target: CanonicalInstallUrl,
    init_com: I,
    uninit_com: U,
    shell_exec: S,
) -> std::io::Result<()>
where
    I: FnOnce(u32) -> i32,
    U: FnOnce(),
    S: FnOnce(&Utf16Nul) -> isize,
{
    let url_wide = Utf16Nul::encode(target.as_str()).ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "canonical install URL contains an interior NUL",
        )
    })?;
    let _com = ComApartmentGuard::init(init_com, uninit_com)?;
    shell_execute_result(shell_exec(&url_wide))
}

/// Opens a verified static canonical install URL in Windows's default browser via `ShellExecuteW`
/// on an STA-initialized worker thread off the WebView2 navigation callback.
#[cfg(windows)]
fn open_in_browser(target: CanonicalInstallUrl) -> std::io::Result<()> {
    dispatch_windows_handoff(target, open_in_browser_windows_worker)
}

/// Runs `ShellExecuteW` on a dedicated Windows worker thread with checked UTF-16 encoding and
/// paired STA COM initialization and cleanup.
#[cfg(windows)]
fn open_in_browser_windows_worker(target: CanonicalInstallUrl) -> std::io::Result<()> {
    const SW_SHOWNORMAL: i32 = 1;
    // Static NUL-terminated UTF-16 "open" verb (`PCWSTR`).
    const VERB_OPEN: [u16; 5] = [b'o' as u16, b'p' as u16, b'e' as u16, b'n' as u16, 0];

    #[link(name = "ole32")]
    unsafe extern "system" {
        fn CoInitializeEx(pv_reserved: *mut core::ffi::c_void, dw_co_init: u32) -> i32;
        fn CoUninitialize();
    }

    #[link(name = "shell32")]
    unsafe extern "system" {
        fn ShellExecuteW(
            hwnd: *mut core::ffi::c_void,
            lp_operation: *const u16,
            lp_file: *const u16,
            lp_parameters: *const u16,
            lp_directory: *const u16,
            n_show_cmd: i32,
        ) -> *mut core::ffi::c_void;
    }

    run_windows_handoff_worker(
        target,
        |flags| {
            // SAFETY:
            // - ABI and types: `CoInitializeEx` in `ole32.dll` uses the `"system"` ABI with
            //   `(LPVOID, DWORD) -> HRESULT`.
            // - `pv_reserved` is null as required by Microsoft documentation; `flags` is
            //   `COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE` (`0x6`), matching the
            //   documented STA setup required before `ShellExecuteW` on a worker thread.
            unsafe { CoInitializeEx(core::ptr::null_mut(), flags) }
        },
        || {
            // SAFETY:
            // - `CoUninitialize` in `ole32.dll` uses the `"system"` ABI with `() -> ()`.
            // - Paired 1:1 with a preceding `CoInitializeEx` on this same worker thread that
            //   returned `S_OK` or `S_FALSE`.
            unsafe { CoUninitialize() }
        },
        |url_wide| {
            // SAFETY:
            // - ABI and types: `ShellExecuteW` in `shell32.dll` uses the `"system"` ABI with
            //   `(HWND, PCWSTR, PCWSTR, PCWSTR, PCWSTR, INT) -> HINSTANCE`. `SW_SHOWNORMAL` is `1i32`.
            // - Lifetimes and pointer validity: `VERB_OPEN` is a `'static` NUL-terminated UTF-16
            //   array; `url_wide` is a `Utf16Nul` constructed inside `run_windows_handoff_worker`
            //   from `target: CanonicalInstallUrl`, guaranteeing NUL termination with no interior
            //   NUL and remaining alive and unmoved for the entire `ShellExecuteW` call; `hwnd`,
            //   `lp_parameters`, and `lp_directory` are documented optional pointers passed as null.
            // - Input invariant: `target` is a `CanonicalInstallUrl` witness referencing only a
            //   static `https://` URL from `oah_core::agents::profile` (no renderer-supplied path
            //   or arguments), and STA COM initialization succeeded on this worker thread.
            // - Return value: Microsoft documents that `ShellExecuteW` returns an `HINSTANCE`
            //   value castable only to `INT_PTR` (`isize`), where `> 32` indicates success and
            //   `<= 32` is an error code (not an owned handle to close).
            unsafe {
                ShellExecuteW(
                    core::ptr::null_mut(),
                    VERB_OPEN.as_ptr(),
                    url_wide.as_ptr(),
                    core::ptr::null(),
                    core::ptr::null(),
                    SW_SHOWNORMAL,
                ) as isize
            }
        },
    )
}

#[cfg(not(any(target_os = "macos", windows)))]
fn open_in_browser(_target: CanonicalInstallUrl) -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "external browser handoff is supported on macOS and Windows only",
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    // Windows unit test binaries do not embed `tauri_build`'s application manifest, yet linking
    // `tauri`/`wry` pulls in `comctl32.dll!TaskDialogIndirect`, which requires Common Controls v6.
    // Emit the MSVC linker `.drectve` `/MANIFESTDEPENDENCY` only for Windows MSVC test builds.
    #[cfg(all(test, windows, target_env = "msvc"))]
    #[used]
    #[unsafe(link_section = ".drectve")]
    static WINDOWS_TEST_COMMON_CONTROLS_MANIFEST_DIRECTIVE: [u8; 168] =
        *b" /MANIFESTDEPENDENCY:\"type='win32' name='Microsoft.Windows.Common-Controls' version='6.0.0.0' processorArchitecture='*' publicKeyToken='6595b64144ccf1df' language='*'\" ";

    fn parse(raw: &str) -> Url {
        Url::parse(raw).expect("valid test URL")
    }

    /// Runs the production handler with a spy launcher and returns `(allow_webview, launched_urls)`.
    fn run_handler(
        is_main_window: bool,
        url: &Url,
        dev_url: Option<&Url>,
        is_windows: bool,
    ) -> (bool, Vec<CanonicalInstallUrl>) {
        let launched = RefCell::new(Vec::new());
        let allow = handle_navigation_for_os(is_main_window, url, dev_url, is_windows, |target| {
            launched.borrow_mut().push(target);
            Ok::<(), ()>(())
        });
        (allow, launched.into_inner())
    }

    #[test]
    fn every_agent_profile_has_a_canonical_https_install_url_accepted_by_production_handler() {
        assert_eq!(AgentId::ALL.len(), 7);
        for id in AgentId::ALL {
            let expected = profile(id).install_url;
            let parsed = Url::parse(expected).expect("catalog install URL parses");
            assert_eq!(parsed.scheme(), "https");
            assert_eq!(parsed.as_str(), expected);
            assert!(parsed.query().is_none());
            assert!(parsed.fragment().is_none());
            assert!(parsed.username().is_empty());
            assert!(parsed.password().is_none());
            assert!(!std::ptr::eq(parsed.as_str(), expected));

            for is_windows in [false, true] {
                let (allow, launched) = run_handler(true, &parsed, None, is_windows);
                assert!(
                    !allow,
                    "opening an install link must cancel in-app navigation"
                );
                assert_eq!(launched.len(), 1);
                assert_eq!(launched[0].as_str().as_bytes(), expected.as_bytes());
            }
        }
    }

    #[test]
    fn parsed_equivalents_launch_only_the_exact_static_catalog_bytes() {
        let expected = profile(AgentId::Cursor).install_url;
        let equivalents = [
            "https://CURSOR.COM/download",
            "https://cursor.com:443/download",
            "https://cursor.com/a/../download",
            "https://cursor.com/download\n",
            "https://cursor.com/download\0",
        ];
        for raw in equivalents {
            let parsed = Url::parse(raw).expect("WHATWG URL parser normalizes equivalent");
            assert_eq!(parsed.as_str(), expected);
            let (allow, launched) = run_handler(true, &parsed, None, cfg!(windows));
            assert!(!allow, "webview navigation must cancel for {raw:?}");
            assert_eq!(launched.len(), 1);
            assert_eq!(launched[0].as_str().as_bytes(), expected.as_bytes());
        }
    }

    #[test]
    fn installed_app_origins_are_allowed_per_platform_without_launching_a_browser() {
        let macos_urls = [
            "tauri://localhost/",
            "tauri://localhost/index.html",
            "tauri://localhost/#agents",
        ];
        let windows_urls = [
            "http://tauri.localhost/",
            "http://tauri.localhost/index.html",
            "https://tauri.localhost/",
        ];
        for raw in macos_urls {
            let url = parse(raw);
            let (allow_mac, launched_mac) = run_handler(true, &url, None, false);
            assert!(
                allow_mac,
                "macOS installed app URL should stay allowed: {raw}"
            );
            assert!(launched_mac.is_empty());

            let (allow_win, launched_win) = run_handler(true, &url, None, true);
            assert!(
                !allow_win,
                "Windows must reject non-Windows custom protocol: {raw}"
            );
            assert!(launched_win.is_empty());
        }
        for raw in windows_urls {
            let url = parse(raw);
            let (allow_win, launched_win) = run_handler(true, &url, None, true);
            assert!(
                allow_win,
                "Windows installed app URL should stay allowed: {raw}"
            );
            assert!(launched_win.is_empty());

            let (allow_mac, launched_mac) = run_handler(true, &url, None, false);
            assert!(
                !allow_mac,
                "macOS must reject Windows custom protocol host: {raw}"
            );
            assert!(launched_mac.is_empty());
        }
    }

    #[test]
    fn dev_origin_is_allowed_only_when_configured_in_dev_mode() {
        let dev = parse("http://localhost:1420");
        for ok in [
            "http://localhost:1420/",
            "http://localhost:1420/index.html?v=1",
        ] {
            let url = parse(ok);
            let (allow_dev, launched_dev) = run_handler(true, &url, Some(&dev), cfg!(windows));
            assert!(allow_dev);
            assert!(launched_dev.is_empty());

            // Same dev URL is rejected in production (dev_url = None).
            let (allow_prod, launched_prod) = run_handler(true, &url, None, cfg!(windows));
            assert!(!allow_prod);
            assert!(launched_prod.is_empty());
        }

        // Different port, host, scheme, or credentials are rejected even in dev mode.
        for bad in [
            "http://localhost:1421/",
            "https://localhost:1420/",
            "http://127.0.0.1:1420/",
            "http://user@localhost:1420/",
        ] {
            let (allow, launched) = run_handler(true, &parse(bad), Some(&dev), cfg!(windows));
            assert!(!allow, "unexpectedly allowed dev URL: {bad}");
            assert!(launched.is_empty());
        }
    }

    #[test]
    fn unrelated_and_mutated_parsed_urls_are_rejected_by_production_handler() {
        let dev = parse("http://localhost:1420");
        let rejected_parsed = [
            // Unrelated external HTTPS URLs
            "https://example.com/",
            "https://github.com/openai/codex-other",
            "https://cursor.com.evil.example/download",
            "https://evil.example/?https://cursor.com/download",
            // Mutations of canonical URLs (including interior NUL / percent-encoded NUL)
            "http://cursor.com/download",
            "https://cursor.com/download?ref=test",
            "https://cursor.com/download#top",
            "https://cursor.com/download/",
            "https://cursor.com/download/extra",
            "https://cursor.com:8443/download",
            "https://cursor.com/down\0load",
            "https://cursor.com/download%00",
            "https://opencode.ai/docs",
            // Spoofed app origins
            "tauri://evil.example/",
            "tauri://localhost:1420/",
            "tauri://user@localhost/",
            "http://tauri.localhost.evil.example/",
            "http://tauri.localhost:8080/",
            "http://user@tauri.localhost/",
            // Non-HTTPS / dangerous schemes
            "file:///etc/passwd",
            "file:///C:/Windows/System32/calc.exe",
            "javascript:alert(1)",
            "data:text/html,<h1>x</h1>",
            "mailto:test@example.com",
            "about:blank",
            "ms-msdt:/id",
        ];

        let mut username_only = parse(profile(AgentId::Cursor).install_url);
        username_only
            .set_username("user")
            .expect("catalog URL supports a username");
        let mut username_and_password = username_only.clone();
        username_and_password
            .set_password(Some("pass"))
            .expect("catalog URL supports a password");

        for parsed in rejected_parsed
            .into_iter()
            .map(parse)
            .chain([username_only, username_and_password])
        {
            for is_windows in [false, true] {
                for dev_url in [None, Some(&dev)] {
                    let (allow, launched) = run_handler(true, &parsed, dev_url, is_windows);
                    assert!(
                        !allow,
                        "production handler must reject {parsed} (windows={is_windows}, dev={})",
                        dev_url.is_some()
                    );
                    assert!(launched.is_empty(), "launcher must not run for {parsed}");
                }
            }
        }
    }

    #[test]
    fn unparseable_strings_do_not_produce_a_url_for_on_navigation() {
        for raw in [
            "",
            "not a url",
            "://missing-scheme",
            "https://cursor.com:badport/download",
        ] {
            assert!(
                Url::parse(raw).is_err(),
                "expected Url::parse failure for {raw:?}"
            );
        }
    }

    #[test]
    fn non_main_window_navigation_is_rejected_and_never_launches() {
        let cursor = parse(profile(AgentId::Cursor).install_url);
        let (allow_external, launched_external) = run_handler(false, &cursor, None, false);
        assert!(!allow_external);
        assert!(launched_external.is_empty());

        let (allow_app, launched_app) =
            run_handler(false, &parse("tauri://localhost/"), None, false);
        assert!(!allow_app);
        assert!(launched_app.is_empty());
    }

    #[test]
    fn launcher_error_still_cancels_webview_navigation() {
        let cursor = parse(profile(AgentId::Cursor).install_url);
        let calls = RefCell::new(0usize);
        let allow = handle_navigation_for_os(true, &cursor, None, cfg!(windows), |_| {
            *calls.borrow_mut() += 1;
            Err("simulated launcher failure")
        });
        assert!(!allow);
        assert_eq!(*calls.borrow(), 1);
    }

    #[test]
    fn windows_utf16_encoder_terminates_every_catalog_url_and_rejects_interior_nul() {
        for id in AgentId::ALL {
            let url = profile(id).install_url;
            let wide = utf16_nul(url).expect("catalog URL encodes to UTF-16");
            assert_eq!(wide.last(), Some(&0));
            assert_eq!(String::from_utf16(&wide[..wide.len() - 1]).unwrap(), url);
        }
        assert_eq!(utf16_nul("https://cursor.com/\0download"), None);
    }

    #[test]
    fn windows_handoff_returns_from_navigation_callback_before_worker_completes() {
        use std::sync::mpsc;
        use std::time::Duration;

        let expected = profile(AgentId::Gemini).install_url;
        let parsed = parse(expected);
        let callback_thread = std::thread::current().id();

        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();

        let allow = handle_navigation_for_os(true, &parsed, None, true, |target| {
            dispatch_windows_handoff(target, move |worker_target| {
                entered_tx
                    .send((std::thread::current().id(), worker_target))
                    .expect("worker notifies entry");
                // Bounded wait so a regression to synchronous execution fails promptly instead of deadlocking.
                let released = release_rx.recv_timeout(Duration::from_secs(2)).is_ok();
                let _ = done_tx.send(released);
                if released {
                    Ok(())
                } else {
                    Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        "worker was not released after navigation callback returned",
                    ))
                }
            })
        });

        assert!(
            !allow,
            "navigation callback must cancel in-app navigation immediately"
        );
        // The callback already returned while the worker is still blocked on `release_rx`.
        let (worker_thread, received_target) = entered_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("worker thread starts");
        assert_ne!(
            worker_thread, callback_thread,
            "Windows handoff must run off the navigation callback thread"
        );
        assert_eq!(received_target.as_str(), expected);

        assert!(
            release_tx.send(()).is_ok(),
            "worker must remain blocked until released after the navigation callback returns"
        );
        assert!(
            done_rx
                .recv_timeout(Duration::from_secs(5))
                .expect("worker finishes cleanly"),
            "worker must receive post-callback release before its bounded timeout"
        );
    }

    #[test]
    fn windows_com_apartment_guard_initializes_sta_and_uninitializes_only_when_owned() {
        let expected_flags = COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE;

        for ok_hr in [S_OK, S_FALSE] {
            let uninit_calls = RefCell::new(0usize);
            let seen_flags = RefCell::new(0u32);
            {
                let _guard = ComApartmentGuard::init(
                    |flags| {
                        *seen_flags.borrow_mut() = flags;
                        ok_hr
                    },
                    || {
                        *uninit_calls.borrow_mut() += 1;
                    },
                )
                .expect("S_OK/S_FALSE must succeed");
                assert_eq!(*seen_flags.borrow(), expected_flags);
                assert_eq!(*uninit_calls.borrow(), 0);
            }
            assert_eq!(
                *uninit_calls.borrow(),
                1,
                "CoUninitialize must run exactly once on drop for HRESULT {ok_hr}"
            );
        }

        // RPC_E_CHANGED_MODE means STA could not be established on this thread;
        // report failure and do not call CoUninitialize.
        let changed_mode_uninit = RefCell::new(0usize);
        let changed_mode_err = ComApartmentGuard::init(
            |_| RPC_E_CHANGED_MODE,
            || {
                *changed_mode_uninit.borrow_mut() += 1;
            },
        )
        .err()
        .expect("RPC_E_CHANGED_MODE must fail");
        assert_eq!(*changed_mode_uninit.borrow(), 0);
        assert_eq!(
            changed_mode_err.to_string(),
            "CoInitializeEx failed with HRESULT 0x80010106"
        );

        // Any other negative HRESULT fails without calling CoUninitialize.
        let failed_uninit = RefCell::new(0usize);
        let err = ComApartmentGuard::init(
            |_| 0x8007_000E_u32 as i32,
            || {
                *failed_uninit.borrow_mut() += 1;
            },
        )
        .err()
        .expect("E_OUTOFMEMORY must fail");
        assert_eq!(*failed_uninit.borrow(), 0);
        assert_eq!(
            err.to_string(),
            "CoInitializeEx failed with HRESULT 0x8007000e"
        );
    }

    #[test]
    fn windows_worker_encodes_canonical_url_at_ffi_boundary_and_skips_shell_when_sta_fails() {
        let expected = profile(AgentId::Cursor).install_url;
        let expected_wide = utf16_nul(expected).expect("catalog URL encodes to UTF-16");
        let target = CanonicalInstallUrl::match_url(&parse(expected)).expect("canonical witness");
        let expected_flags = COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE;

        for ok_hr in [S_OK, S_FALSE] {
            for (shell_code, expect_ok) in [(33, true), (5, false)] {
                let seen_flags = RefCell::new(0u32);
                let shell_calls = RefCell::new(0usize);
                let uninit_calls = RefCell::new(0usize);
                let outcome = run_windows_handoff_worker(
                    target,
                    |flags| {
                        *seen_flags.borrow_mut() = flags;
                        ok_hr
                    },
                    || {
                        *uninit_calls.borrow_mut() += 1;
                    },
                    |url_wide| {
                        *shell_calls.borrow_mut() += 1;
                        assert_eq!(
                            *uninit_calls.borrow(),
                            0,
                            "STA must stay active during call"
                        );
                        assert_eq!(url_wide.as_slice(), expected_wide.as_slice());
                        shell_code
                    },
                );
                assert_eq!(outcome.is_ok(), expect_ok);
                assert_eq!(*seen_flags.borrow(), expected_flags);
                assert_eq!(*shell_calls.borrow(), 1);
                assert_eq!(
                    *uninit_calls.borrow(),
                    1,
                    "CoUninitialize must run exactly once for HRESULT {ok_hr} (shell code {shell_code})"
                );
            }
        }

        for (bad_hr, expected_hex) in [
            (RPC_E_CHANGED_MODE, "0x80010106"),
            (0x8007_000E_u32 as i32, "0x8007000e"),
        ] {
            let shell_calls = RefCell::new(0usize);
            let uninit_calls = RefCell::new(0usize);
            let err = run_windows_handoff_worker(
                target,
                |_| bad_hr,
                || {
                    *uninit_calls.borrow_mut() += 1;
                },
                |_| {
                    *shell_calls.borrow_mut() += 1;
                    33
                },
            )
            .expect_err("failed STA initialization must abort before ShellExecuteW");
            assert_eq!(
                err.to_string(),
                format!("CoInitializeEx failed with HRESULT {expected_hex}")
            );
            assert_eq!(*shell_calls.borrow(), 0);
            assert_eq!(*uninit_calls.borrow(), 0);
        }

        let init_calls = RefCell::new(0usize);
        let shell_calls = RefCell::new(0usize);
        let uninit_calls = RefCell::new(0usize);
        let bad_target = CanonicalInstallUrl("https://cursor.com/\0download");
        let err = run_windows_handoff_worker(
            bad_target,
            |_| {
                *init_calls.borrow_mut() += 1;
                S_OK
            },
            || {
                *uninit_calls.borrow_mut() += 1;
            },
            |_| {
                *shell_calls.borrow_mut() += 1;
                33
            },
        )
        .expect_err("interior NUL must fail before COM or ShellExecuteW");
        assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput);
        assert_eq!(*init_calls.borrow(), 0);
        assert_eq!(*shell_calls.borrow(), 0);
        assert_eq!(*uninit_calls.borrow(), 0);
    }

    #[test]
    fn windows_shell_execute_result_treats_values_above_32_as_success() {
        assert!(shell_execute_result(33).is_ok());
        assert!(shell_execute_result(42).is_ok());
        for code in [0, 2, 5, 31, 32] {
            let err = shell_execute_result(code).expect_err("codes <= 32 must fail");
            assert_eq!(
                err.to_string(),
                format!("ShellExecuteW failed with code {code}")
            );
        }
    }

    #[test]
    fn in_app_plugin_builds_with_spy_launcher() {
        let _plugin = plugin_with::<tauri::Wry, _>(|_| Ok(()));
    }
}
