// 제한된 HTTPS 클라이언트 시험: §9.3 행 검사, 요청 모양, 비율 제한·크기·압축 응답의 유형, 토큰 가림을 기록 전송으로 확인한다.
//! The bounded HTTPS client (CONTRACTS C18) over a recording transport: the row check before anything is sent,
//! request shapes, typed answers and the redaction of tokens. Nothing here opens a connection.

use oah_core::net::{
    Client, GITHUB_API, GITHUB_RAW, HttpsTransport, NetError, Offline, ROWS, RecordingTransport,
    Request, Response, Row, Token, check, user_agent,
};
use oah_core::paths::Env;
use std::time::Duration;

const COMMIT_URL: &str = "https://api.github.com/repos/acme/skills/commits/HEAD";

fn api(url: &str) -> Request {
    Request::get(GITHUB_API, url, 1024, Duration::from_secs(5))
}

#[test]
fn the_rows_are_the_two_github_review_rows_of_the_network_table() {
    assert_eq!(ROWS, [GITHUB_API, GITHUB_RAW]);
    assert_eq!(
        (GITHUB_API.number, GITHUB_API.host, GITHUB_API.token),
        (2, "api.github.com", true)
    );
    assert_eq!(
        (GITHUB_RAW.number, GITHUB_RAW.host, GITHUB_RAW.token),
        (3, "raw.githubusercontent.com", false)
    );
}

#[test]
fn a_request_names_oah_and_asks_for_an_uncompressed_body() {
    let request = api(COMMIT_URL).header("X-GitHub-Api-Version", "2022-11-28");
    assert_eq!(user_agent(), format!("oah/{}", oah_core::VERSION));
    assert_eq!(
        request.headers,
        vec![
            ("user-agent".to_string(), user_agent()),
            ("accept-encoding".to_string(), "identity".to_string()),
            ("x-github-api-version".to_string(), "2022-11-28".to_string()),
        ]
    );
    assert_eq!(
        request.header_value("X-GITHUB-API-VERSION"),
        Some("2022-11-28")
    );
    let transport = RecordingTransport::new();
    transport.answer(
        COMMIT_URL,
        Response::new(200, "0123456789abcdef0123456789abcdef01234567"),
    );
    let answer = Client::new(&transport).send(&request).expect("an answer");
    assert_eq!(answer.status, 200);
    assert_eq!(transport.requests(), vec![request]);
}

#[test]
fn a_request_that_breaks_a_rule_is_refused_before_anything_is_sent() {
    let transport = RecordingTransport::new();
    let client = Client::new(&transport);
    let other = Row {
        number: 9,
        host: "example.com",
        token: false,
    };
    let credential = Token::new("not-a-real-credential").expect("a token value");
    let second = Duration::from_secs(1);
    let refused = [
        ("http", api("http://api.github.com/repos/acme/skills")),
        (
            "another row's host",
            api("https://raw.githubusercontent.com/acme/skills/x"),
        ),
        ("a longer host", api("https://api.github.com.example/repos")),
        ("a port", api("https://api.github.com:443/repos")),
        (
            "user information",
            api("https://user:@api.github.com/repos"),
        ),
        ("no path", api("https://api.github.com")),
        ("a space", api("https://api.github.com/repos/a b")),
        ("a backslash", api("https://api.github.com\\@example.com/")),
        (
            "an undeclared row",
            Request::get(other, "https://example.com/", 1, second),
        ),
        (
            "a token on the raw row",
            Request::get(
                GITHUB_RAW,
                "https://raw.githubusercontent.com/a/b/c/d",
                1,
                second,
            )
            .bearer(&credential),
        ),
        (
            "no byte limit",
            Request::get(GITHUB_API, "https://api.github.com/x", 0, second),
        ),
        (
            "no deadline",
            Request::get(GITHUB_API, "https://api.github.com/x", 1, Duration::ZERO),
        ),
        (
            "a header line break",
            api("https://api.github.com/x").header("accept", "a\r\nx-other: b"),
        ),
        (
            "a header name",
            api("https://api.github.com/x").header("bad name", "v"),
        ),
    ];
    for (name, request) in refused {
        let error = client.send(&request).expect_err(name);
        assert!(
            matches!(error, NetError::Refused { .. }),
            "{name}: {error:?}"
        );
        assert_eq!(check(&request), Err(error), "{name}");
    }
    assert!(transport.requests().is_empty(), "nothing was sent");
}

#[test]
fn a_rate_limit_answer_is_typed_with_its_retry_time() {
    let transport = RecordingTransport::new();
    let client = Client::new(&transport);
    transport.answer(
        COMMIT_URL,
        Response::new(429, "slow down")
            .with_header("Retry-After", "120")
            .with_header("Content-Encoding", "gzip"),
    );
    let limited = client.send(&api(COMMIT_URL)).expect_err("limited");
    assert_eq!(
        limited,
        NetError::RateLimited {
            host: "api.github.com",
            reset_at: None,
            retry_after: Some(120)
        }
    );
    assert_eq!(
        limited.to_string(),
        "api.github.com refused more requests for now (rate limit); try again in 120 seconds"
    );

    transport.answer(
        COMMIT_URL,
        Response::new(403, "")
            .with_header("X-RateLimit-Remaining", "0")
            .with_header("X-RateLimit-Reset", "1767225600"),
    );
    let limited = client.send(&api(COMMIT_URL)).expect_err("limited");
    assert_eq!(
        limited,
        NetError::RateLimited {
            host: "api.github.com",
            reset_at: Some(1_767_225_600),
            retry_after: None
        }
    );
    assert_eq!(
        (limited.code(), limited.failure()),
        (Some("OAH_NET_RATE_LIMITED"), "rate-limited")
    );
    assert_eq!(
        limited.to_string(),
        "api.github.com refused more requests for now (rate limit); try again after 2026-01-01T00:00:00Z"
    );

    transport.answer(
        COMMIT_URL,
        Response::new(403, "forbidden").with_header("x-ratelimit-remaining", "12"),
    );
    let answer = client
        .send(&api(COMMIT_URL))
        .expect("another 403 is an answer");
    assert_eq!((answer.status, answer.body.len()), (403, 0));
}

#[test]
fn rate_limit_guidance_uses_only_representable_absolute_times() {
    for (reset_at, retry_after, guidance) in [
        (u64::MAX, None, "try again later"),
        (253_402_300_800, Some(60), "try again in 60 seconds"),
        (
            253_402_300_799,
            Some(60),
            "try again after 9999-12-31T23:59:59Z",
        ),
    ] {
        let error = NetError::RateLimited {
            host: GITHUB_API.host,
            reset_at: Some(reset_at),
            retry_after,
        };
        assert_eq!(
            error.to_string(),
            format!("api.github.com refused more requests for now (rate limit); {guidance}"),
            "reset_at={reset_at}"
        );
    }
}

#[test]
fn only_a_success_has_a_body_and_it_is_bounded() {
    let transport = RecordingTransport::new();
    let client = Client::new(&transport);
    transport.answer(COMMIT_URL, Response::new(200, vec![b'x'; 1025]));
    let error = client.send(&api(COMMIT_URL)).expect_err("too large");
    assert_eq!(
        error,
        NetError::TooLarge {
            host: "api.github.com",
            limit: 1024
        }
    );
    assert_eq!(
        (error.code(), error.failure()),
        (Some("OAH_NET_TOO_LARGE"), "oversize")
    );

    transport.answer(
        COMMIT_URL,
        Response::new(404, vec![b'x'; 4096]).with_header("Content-Encoding", "gzip"),
    );
    let answer = client
        .send(&api(COMMIT_URL))
        .expect("an error page is an answer");
    assert_eq!((answer.status, answer.body.len()), (404, 0));
}

#[test]
fn a_success_accepts_only_identity_content_encodings() {
    let transport = RecordingTransport::new();
    let client = Client::new(&transport);
    let request = api(COMMIT_URL);
    let cases: &[(&[&str], bool)] = &[
        (&["identity", "gzip"], false),
        (&["identity, gzip"], false),
        (&["gzip"], false),
        (&[""], false),
        (&[], true),
        (&["  IdEnTiTy\t"], true),
        (&["identity", "identity, IDENTITY"], true),
    ];
    for &(values, identity) in cases {
        let response = values
            .iter()
            .fold(Response::new(200, "abc"), |response, &value| {
                response.with_header("Content-Encoding", value)
            });
        transport.answer(COMMIT_URL, response);

        let result = client.send(&request);
        if identity {
            assert_eq!(
                result.expect("identity response").body,
                b"abc",
                "{values:?}"
            );
        } else {
            let error = result.expect_err("unsupported or invalid encoding");
            assert!(
                matches!(
                    error,
                    NetError::BadResponse {
                        host: "api.github.com",
                        failure: "content-encoding",
                        ..
                    }
                ),
                "{values:?}: {error:?}"
            );
            assert_eq!(error.code(), None);
        }
    }
}

#[test]
fn an_address_without_an_answer_is_offline() {
    let transport = RecordingTransport::new();
    let error = Client::new(&transport)
        .send(&api(COMMIT_URL))
        .expect_err("offline");
    assert_eq!(
        (error.code(), error.failure()),
        (Some("OAH_NET_OFFLINE"), "network")
    );
    assert_eq!(transport.requests().len(), 1);
    transport.clear();
    assert!(transport.requests().is_empty());
}

#[test]
fn every_failure_has_its_code_and_v040_word() {
    let host = "api.github.com";
    let offline = |reason| NetError::Offline {
        host,
        reason,
        detail: String::new(),
    };
    let cases = [
        (offline(Offline::Dns), Some("OAH_NET_OFFLINE"), "dns"),
        (
            offline(Offline::Timeout),
            Some("OAH_NET_OFFLINE"),
            "timeout",
        ),
        (
            offline(Offline::Network),
            Some("OAH_NET_OFFLINE"),
            "network",
        ),
        (
            NetError::TooLarge { host, limit: 1 },
            Some("OAH_NET_TOO_LARGE"),
            "oversize",
        ),
        (
            NetError::Tls {
                host,
                detail: String::new(),
            },
            None,
            "network",
        ),
        (NetError::Status { host, status: 404 }, None, "not-found"),
        (NetError::Status { host, status: 410 }, None, "gone"),
        (NetError::Status { host, status: 301 }, None, "moved"),
        (NetError::Status { host, status: 500 }, None, "http-error"),
        (
            NetError::BadResponse {
                host,
                failure: "bad-response",
                detail: String::new(),
            },
            None,
            "bad-response",
        ),
        (
            NetError::Refused {
                failure: "insecure-url",
                detail: String::new(),
            },
            None,
            "insecure-url",
        ),
    ];
    for (error, code, word) in cases {
        assert_eq!((error.code(), error.failure()), (code, word), "{error:?}");
    }
    assert_eq!(
        NetError::Status { host, status: 404 }.to_string(),
        "api.github.com answered HTTP 404: not found, or private"
    );
}

#[test]
fn a_token_comes_from_the_environment_and_is_never_shown() {
    let folder = tempfile::tempdir().expect("a folder");
    let env = Env::new(folder.path()).expect("an environment");
    assert_eq!(Token::from_env(&env), None);
    let env = env.with("GH_TOKEN", "second-value");
    assert_eq!(Token::from_env(&env), Token::new("second-value"));
    let env = env.with("GITHUB_TOKEN", "first-value");
    assert_eq!(Token::from_env(&env), Token::new("first-value"));
    assert_eq!(
        Token::from_env(
            &Env::new(folder.path())
                .expect("an environment")
                .with("GITHUB_TOKEN", "")
        ),
        None
    );
    assert_eq!(
        Token::from_env(
            &Env::new(folder.path())
                .expect("an environment")
                .with("GITHUB_TOKEN", "  padded-value\n")
        ),
        Token::new("padded-value")
    );
    assert_eq!(Token::new("has space"), None);
    assert_eq!(Token::new("line\nbreak"), None);

    let token = Token::new("first-value").expect("a token value");
    assert_eq!(format!("{token:?}"), "Token(<redacted>)");
    let request = api(COMMIT_URL).bearer(&token);
    assert_eq!(
        request.header_value("authorization"),
        Some("Bearer first-value")
    );
    let shown = format!("{request:?}");
    assert!(
        !shown.contains("first-value") && shown.contains("<redacted>"),
        "{shown}"
    );
}

#[test]
fn the_real_transport_is_built_offline_and_sits_behind_the_same_check() {
    let transport = HttpsTransport::new();
    let error = Client::new(&transport)
        .send(&api("http://api.github.com/x"))
        .expect_err("refused");
    assert!(
        matches!(
            error,
            NetError::Refused {
                failure: "insecure-url",
                ..
            }
        ),
        "{error:?}"
    );
}
