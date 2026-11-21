// 제한된 HTTPS 클라이언트: §9.3 표의 행으로만 보내고, 환경 프록시·리디렉션 없이 호출마다 기한과 바이트 한도를 둔다.
//! The one bounded HTTPS client (DESIGN-C2 §9.3, CONTRACTS C18).
//!
//! Every request names the [`Row`] of the network table it belongs to. [`Client::send`] checks it before anything
//! leaves the machine: the row is one this build declares, the URL is `https://` with exactly the row's host (no
//! port, user or other host), a row without tokens carries no `Authorization`, and the call has a byte limit and a
//! deadline. A [`Transport`] then sends it: [`HttpsTransport`] over ureq and rustls with the operating system's
//! trust store, no proxy from the environment and no redirects, or [`RecordingTransport`], which answers from
//! memory and records each request. Only a 2xx answer's body is read, uncompressed (`Accept-Encoding: identity`)
//! and within the limit; any other status is an answer without a body, and one that reports a rate limit becomes
//! [`NetError::RateLimited`].
//!
//! Failures are typed [`NetError`]s. Three carry the stable codes `OAH_NET_OFFLINE`, `OAH_NET_RATE_LIMITED` and
//! `OAH_NET_TOO_LARGE`; each names the v0.4.0 failure word a collector reports. A token is only ever a request
//! header: [`Request`]'s and [`Token`]'s `Debug` write it as `<redacted>`, no error message carries a header, and
//! nothing here logs.

mod https;
mod recording;

pub use https::HttpsTransport;
pub use recording::RecordingTransport;

use crate::error::codes;
use crate::paths::Env;
use std::fmt;
use std::time::{Duration, UNIX_EPOCH};

/// One row of the network table (DESIGN-C2 §9.3) that this build sends requests for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Row {
    /// The row's number in §9.3.
    pub number: u8,
    /// The one host the row reaches.
    pub host: &'static str,
    /// Whether a request of the row may carry the user's token.
    pub token: bool,
}

/// Row 2: `api.github.com`, the commit and tree reads of a review; a token only when the user set one.
pub const GITHUB_API: Row = Row {
    number: 2,
    host: "api.github.com",
    token: true,
};

/// Row 3: `raw.githubusercontent.com`, the file bodies of a skill folder at a commit; never a token.
pub const GITHUB_RAW: Row = Row {
    number: 3,
    host: "raw.githubusercontent.com",
    token: false,
};

/// Every row this build sends requests for; [`Client::send`] refuses any other.
pub const ROWS: [Row; 2] = [GITHUB_API, GITHUB_RAW];

/// The `User-Agent` of every request: `oah/<version>`.
pub fn user_agent() -> String {
    format!("oah/{}", crate::VERSION)
}

/// The user's GitHub token as a header value only; its `Debug` never shows it.
#[derive(Clone, PartialEq, Eq)]
pub struct Token(String);

impl Token {
    /// `value` when it is a non-empty run of visible ASCII characters; anything else is no token.
    pub fn new(value: &str) -> Option<Token> {
        (!value.is_empty() && value.bytes().all(|byte| byte.is_ascii_graphic()))
            .then(|| Token(value.to_string()))
    }

    /// `GITHUB_TOKEN`, else `GH_TOKEN`, read from `env` now (§5.2) without surrounding white space. It is never
    /// stored or logged.
    pub fn from_env(env: &Env) -> Option<Token> {
        ["GITHUB_TOKEN", "GH_TOKEN"].into_iter().find_map(|name| {
            env.var(name)
                .and_then(|value| value.to_str())
                .and_then(|value| Token::new(value.trim()))
        })
    }
}

impl fmt::Debug for Token {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Token(<redacted>)")
    }
}

/// One GET request.
#[derive(Clone, PartialEq, Eq)]
pub struct Request {
    pub row: Row,
    pub url: String,
    /// Header names in lowercase, in the order they were added.
    pub headers: Vec<(String, String)>,
    /// The most body bytes the call accepts.
    pub limit: u64,
    /// The whole call, from the connection to the last body byte.
    pub deadline: Duration,
}

impl Request {
    /// A GET of `url` under `row`, with `oah/<version>` as its `User-Agent` and `Accept-Encoding: identity`.
    pub fn get(row: Row, url: impl Into<String>, limit: u64, deadline: Duration) -> Request {
        Request {
            row,
            url: url.into(),
            headers: vec![
                ("user-agent".to_string(), user_agent()),
                ("accept-encoding".to_string(), "identity".to_string()),
            ],
            limit,
            deadline,
        }
    }

    /// Adds a header; its name is kept in lowercase.
    pub fn header(mut self, name: &str, value: impl Into<String>) -> Request {
        self.headers.push((name.to_ascii_lowercase(), value.into()));
        self
    }

    /// Adds `Authorization: Bearer <token>`; [`Client::send`] refuses it on a row without tokens.
    pub fn bearer(self, token: &Token) -> Request {
        let value = format!("Bearer {}", token.0);
        self.header("authorization", value)
    }

    /// The value of the first header called `name`, in any case.
    pub fn header_value(&self, name: &str) -> Option<&str> {
        let name = name.to_ascii_lowercase();
        self.headers
            .iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| value.as_str())
    }
}

impl fmt::Debug for Request {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let headers: Vec<(&str, &str)> = self
            .headers
            .iter()
            .map(|(name, value)| {
                let shown = if name == "authorization" {
                    "<redacted>"
                } else {
                    value.as_str()
                };
                (name.as_str(), shown)
            })
            .collect();
        f.debug_struct("Request")
            .field("row", &self.row.number)
            .field("url", &self.url)
            .field("headers", &headers)
            .field("limit", &self.limit)
            .field("deadline", &self.deadline)
            .finish()
    }
}

/// An answer with any status: header names in lowercase; the body of a 2xx answer, at most the request's limit of
/// bytes, and no body for any other status.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    /// An answer without headers.
    pub fn new(status: u16, body: impl Into<Vec<u8>>) -> Response {
        Response {
            status,
            headers: Vec::new(),
            body: body.into(),
        }
    }

    /// The answer with one more header; its name is kept in lowercase.
    pub fn with_header(mut self, name: &str, value: impl Into<String>) -> Response {
        self.headers.push((name.to_ascii_lowercase(), value.into()));
        self
    }

    /// The value of the first header called `name`, in any case.
    pub fn header(&self, name: &str) -> Option<&str> {
        let name = name.to_ascii_lowercase();
        self.headers
            .iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| value.as_str())
    }
}

/// Why a connection gave no answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Offline {
    /// The host name did not resolve.
    Dns,
    /// The deadline passed.
    Timeout,
    /// The connection could not be made or broke off.
    Network,
}

/// Why a call has no usable answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetError {
    /// No answer: the name did not resolve, the connection failed or broke off, or the deadline passed.
    Offline {
        host: &'static str,
        reason: Offline,
        detail: String,
    },
    /// The host's rate limit: a 429, or a 403 with no requests left or a `Retry-After`. `reset_at` is GitHub's
    /// `X-RateLimit-Reset` (Unix seconds) and `retry_after` its `Retry-After` (seconds), when sent.
    RateLimited {
        host: &'static str,
        reset_at: Option<u64>,
        retry_after: Option<u64>,
    },
    /// The body is longer than the call's limit; reading stopped there.
    TooLarge { host: &'static str, limit: u64 },
    /// The secure connection could not be set up, or its certificate is not trusted by the system.
    Tls { host: &'static str, detail: String },
    /// An answer whose status the call does not accept.
    Status { host: &'static str, status: u16 },
    /// An answer that breaks the call's rules; `failure` is its v0.4.0 failure word.
    BadResponse {
        host: &'static str,
        failure: &'static str,
        detail: String,
    },
    /// Nothing was sent: the request breaks a rule of the client.
    Refused {
        failure: &'static str,
        detail: String,
    },
}

impl NetError {
    /// The stable code of the three failures the UI shows as states (`OAH_NET_*`); `None` for the others.
    pub fn code(&self) -> Option<&'static str> {
        match self {
            NetError::Offline { .. } => Some(codes::NET_OFFLINE),
            NetError::RateLimited { .. } => Some(codes::NET_RATE_LIMITED),
            NetError::TooLarge { .. } => Some(codes::NET_TOO_LARGE),
            _ => None,
        }
    }

    /// The v0.4.0 failure word a collector reports for this failure.
    pub fn failure(&self) -> &'static str {
        match self {
            NetError::Offline { reason, .. } => match reason {
                Offline::Dns => "dns",
                Offline::Timeout => "timeout",
                Offline::Network => "network",
            },
            NetError::RateLimited { .. } => "rate-limited",
            NetError::TooLarge { .. } => "oversize",
            NetError::Tls { .. } => "network",
            NetError::Status { status, .. } => match status {
                404 => "not-found",
                410 => "gone",
                300..=399 => "moved",
                _ => "http-error",
            },
            NetError::BadResponse { failure, .. } | NetError::Refused { failure, .. } => failure,
        }
    }
}

impl fmt::Display for NetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            NetError::Offline {
                host,
                reason: Offline::Dns,
                ..
            } => write!(
                f,
                "{host} could not be found; check the internet connection"
            ),
            NetError::Offline {
                host,
                reason: Offline::Timeout,
                ..
            } => write!(f, "{host} did not answer in time"),
            NetError::Offline {
                host,
                reason: Offline::Network,
                detail,
            } => write!(f, "{host} could not be reached ({detail})"),
            NetError::RateLimited {
                host,
                reset_at,
                retry_after,
            } => {
                write!(f, "{host} refused more requests for now (rate limit)")?;
                match (reset_at, retry_after) {
                    (Some(reset), _) => write!(
                        f,
                        "; try again after {}",
                        crate::dto::rfc3339_utc(UNIX_EPOCH + Duration::from_secs(*reset))
                    ),
                    (None, Some(seconds)) => write!(f, "; try again in {seconds} seconds"),
                    (None, None) => write!(f, "; try again later"),
                }
            }
            NetError::TooLarge { host, limit } => {
                write!(f, "the answer from {host} is larger than {limit} bytes")
            }
            NetError::Tls { host, detail } => {
                write!(
                    f,
                    "the secure connection to {host} could not be verified ({detail})"
                )
            }
            NetError::Status { host, status } => match status {
                404 => write!(f, "{host} answered HTTP 404: not found, or private"),
                300..=399 => write!(
                    f,
                    "{host} answered HTTP {status}: the address moved, and redirects are not followed"
                ),
                _ => write!(f, "{host} answered HTTP {status}"),
            },
            NetError::BadResponse {
                host,
                failure,
                detail,
            } => write!(f, "{host} sent an unusable answer ({failure}: {detail})"),
            NetError::Refused { detail, .. } => write!(f, "the request was not sent: {detail}"),
        }
    }
}

impl std::error::Error for NetError {}

/// Sends one checked request within `request.deadline`. Any status is an answer; only a call without an answer
/// is an error. The body of a 2xx answer is read up to `request.limit` bytes (a longer one is
/// [`NetError::TooLarge`]); the body of any other answer is not read.
pub trait Transport: Send + Sync {
    fn send(&self, request: &Request) -> Result<Response, NetError>;
}

/// The one way out (§9.3): every request is checked against its row, then sent through a transport.
#[derive(Clone, Copy)]
pub struct Client<'a> {
    transport: &'a dyn Transport,
}

impl<'a> Client<'a> {
    pub fn new(transport: &'a dyn Transport) -> Client<'a> {
        Client { transport }
    }

    /// Checks `request` ([`check`]), sends it, enforces its byte limit again, refuses a compressed 2xx body and
    /// turns a rate-limit answer into [`NetError::RateLimited`].
    pub fn send(&self, request: &Request) -> Result<Response, NetError> {
        check(request)?;
        let mut response = self.transport.send(request)?;
        let host = request.row.host;
        if !(200..300).contains(&response.status) {
            response.body.clear();
            return match rate_limit(host, &response) {
                Some(limited) => Err(limited),
                None => Ok(response),
            };
        }
        let length = u64::try_from(response.body.len()).unwrap_or(u64::MAX);
        if length > request.limit {
            return Err(NetError::TooLarge {
                host,
                limit: request.limit,
            });
        }
        let encoding = response.header("content-encoding").map(str::trim);
        if encoding
            .is_some_and(|value| !value.is_empty() && !value.eq_ignore_ascii_case("identity"))
        {
            return Err(NetError::BadResponse {
                host,
                failure: "content-encoding",
                detail: "the answer is compressed although identity was asked for".to_string(),
            });
        }
        Ok(response)
    }
}

fn refused(failure: &'static str, detail: impl Into<String>) -> NetError {
    NetError::Refused {
        failure,
        detail: detail.into(),
    }
}

/// The client's rules, checked before a request is sent: a declared row, an `https://` URL of exactly the row's
/// host with a path, a byte limit and a deadline, plain header names and values, and no token on a row without
/// tokens.
pub fn check(request: &Request) -> Result<(), NetError> {
    let row = request.row;
    if !ROWS.contains(&row) {
        return Err(refused(
            "attempt-failed",
            format!("{} is not a row of the network table", row.host),
        ));
    }
    let Some(rest) = request.url.strip_prefix("https://") else {
        return Err(refused("insecure-url", "only https:// addresses are sent"));
    };
    if !request.url.bytes().all(|byte| byte.is_ascii_graphic()) || request.url.contains('\\') {
        return Err(refused(
            "insecure-url",
            "the address holds characters an address may not hold",
        ));
    }
    let host = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if host != row.host || !rest[host.len()..].starts_with('/') {
        return Err(refused(
            "insecure-url",
            format!("only {} is reached for row {}", row.host, row.number),
        ));
    }
    if request.limit == 0 || request.deadline.is_zero() {
        return Err(refused(
            "attempt-failed",
            "every request needs a byte limit and a deadline",
        ));
    }
    let plain_name = |name: &str| {
        !name.is_empty()
            && name
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
    };
    let plain_value = |value: &str| {
        value
            .bytes()
            .all(|byte| byte == b' ' || byte.is_ascii_graphic())
    };
    if request
        .headers
        .iter()
        .any(|(name, value)| !plain_name(name) || !plain_value(value))
    {
        return Err(refused(
            "attempt-failed",
            "a header name or value holds characters a header may not hold",
        ));
    }
    if !row.token && request.header_value("authorization").is_some() {
        return Err(refused(
            "attempt-failed",
            format!("row {} never carries a token", row.number),
        ));
    }
    Ok(())
}

/// A 429, or a 403 with no requests left or a `Retry-After`, as GitHub reports its limits; another 403 is a
/// refusal the caller reads.
fn rate_limit(host: &'static str, response: &Response) -> Option<NetError> {
    if !matches!(response.status, 403 | 429) {
        return None;
    }
    let number = |name: &str| {
        response
            .header(name)
            .and_then(|value| value.trim().parse::<u64>().ok())
    };
    let exhausted = number("x-ratelimit-remaining") == Some(0);
    let retry_after = number("retry-after");
    if response.status == 403 && !exhausted && retry_after.is_none() {
        return None;
    }
    Some(NetError::RateLimited {
        host,
        reset_at: number("x-ratelimit-reset"),
        retry_after,
    })
}
