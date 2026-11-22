// 실제 HTTPS 전송: ureq 와 rustls 로 운영체제 신뢰 저장소를 쓰고, 환경 프록시·리디렉션·자동 헤더 없이 요청 하나를 보낸다.
//! [`HttpsTransport`]: ureq 3 over rustls (ring) with `rustls-platform-verifier`, so the operating system's trust
//! store checks every certificate. The agent sends HTTPS only, never reads proxy variables from the environment
//! (`proxy(None)`), follows no redirect (a 3xx is the answer), treats every status as an answer and adds no
//! automatic `User-Agent`, `Accept` or `Accept-Encoding`: the request's own headers are all that is sent. Each
//! call is bounded by its deadline (`timeout_global`, connection to last body byte) and its byte limit.

use super::{NetError, Offline, Request, Response, Transport, check_content_encoding};
use std::io;
use ureq::config::AutoHeaderValue;
use ureq::http::header::{CONTENT_ENCODING, HeaderValue};
use ureq::tls::{RootCerts, TlsConfig, TlsProvider};

/// The real transport. One agent keeps its connections for the transport's lifetime.
#[derive(Debug)]
pub struct HttpsTransport {
    agent: ureq::Agent,
}

impl HttpsTransport {
    pub fn new() -> HttpsTransport {
        let tls = TlsConfig::builder()
            .provider(TlsProvider::Rustls)
            .root_certs(RootCerts::PlatformVerifier)
            .build();
        let config = ureq::Agent::config_builder()
            .https_only(true)
            .proxy(None)
            .max_redirects(0)
            .max_redirects_will_error(false)
            .save_redirect_history(false)
            .http_status_as_error(false)
            .user_agent(AutoHeaderValue::None)
            .accept(AutoHeaderValue::None)
            .accept_encoding(AutoHeaderValue::None)
            .tls_config(tls)
            .build();
        HttpsTransport {
            agent: ureq::Agent::new_with_config(config),
        }
    }
}

impl Default for HttpsTransport {
    fn default() -> HttpsTransport {
        HttpsTransport::new()
    }
}

impl Transport for HttpsTransport {
    fn send(&self, request: &Request) -> Result<Response, NetError> {
        let host = request.row.host;
        let mut builder = self.agent.get(request.url.as_str());
        for (name, value) in &request.headers {
            builder = builder.header(name.as_str(), value.as_str());
        }
        let answer = builder
            .config()
            .timeout_global(Some(request.deadline))
            .build()
            .call()
            .map_err(|error| failure(host, request.limit, error))?;
        read_response(answer, request)
    }
}

fn read_response(
    mut answer: ureq::http::Response<ureq::Body>,
    request: &Request,
) -> Result<Response, NetError> {
    let host = request.row.host;
    let status = answer.status();
    let body = if status.is_success() {
        check_content_encoding(
            host,
            answer
                .headers()
                .get_all(CONTENT_ENCODING)
                .iter()
                .map(HeaderValue::as_bytes),
        )?;
        answer
            .body_mut()
            .with_config()
            .limit(request.limit)
            .read_to_vec()
            .map_err(|error| failure(host, request.limit, error))?
    } else {
        Vec::new()
    };
    let headers = answer
        .headers()
        .iter()
        .filter_map(|(name, value)| {
            Some((name.as_str().to_string(), value.to_str().ok()?.to_string()))
        })
        .collect();
    Ok(Response {
        status: status.as_u16(),
        headers,
        body,
    })
}

/// A ureq failure as a [`NetError`]. A TLS handshake failure arrives as an I/O error of kind `InvalidData`.
fn failure(host: &'static str, limit: u64, error: ureq::Error) -> NetError {
    let detail = error.to_string();
    let offline = |reason| NetError::Offline {
        host,
        reason,
        detail: detail.clone(),
    };
    match error {
        ureq::Error::HostNotFound => offline(Offline::Dns),
        ureq::Error::Timeout(_) => offline(Offline::Timeout),
        ureq::Error::Io(io) => match io.kind() {
            io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => offline(Offline::Timeout),
            io::ErrorKind::InvalidData => NetError::Tls { host, detail },
            _ => offline(Offline::Network),
        },
        ureq::Error::ConnectionFailed => offline(Offline::Network),
        ureq::Error::BodyExceedsLimit(_) => NetError::TooLarge { host, limit },
        ureq::Error::Tls(_)
        | ureq::Error::Rustls(_)
        | ureq::Error::Pem(_)
        | ureq::Error::TlsRequired => NetError::Tls { host, detail },
        ureq::Error::TooManyRedirects | ureq::Error::RedirectFailed => NetError::BadResponse {
            host,
            failure: "too-many-redirects",
            detail,
        },
        ureq::Error::Protocol(_) | ureq::Error::LargeResponseHeader(..) => NetError::BadResponse {
            host,
            failure: "bad-response",
            detail,
        },
        ureq::Error::RequireHttpsOnly(_) => NetError::Refused {
            failure: "insecure-url",
            detail,
        },
        _ => NetError::BadResponse {
            host,
            failure: "attempt-failed",
            detail,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::net::GITHUB_API;
    use std::time::Duration;

    #[test]
    fn every_content_encoding_is_checked_before_reading_the_success_body() {
        let request = Request::get(
            GITHUB_API,
            "https://api.github.com/",
            1,
            Duration::from_secs(1),
        );
        let answer = ureq::http::Response::builder()
            .status(200)
            .header("content-encoding", "identity")
            .header("content-encoding", "gzip")
            .body(ureq::Body::builder().data("abc"))
            .expect("a response with a body longer than the limit");

        let error = read_response(answer, &request).expect_err("unsupported content encoding");
        assert!(
            matches!(
                error,
                NetError::BadResponse {
                    failure: "content-encoding",
                    ..
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn tls_required_is_a_tls_failure() {
        let error = failure(GITHUB_API.host, 1, ureq::Error::TlsRequired);
        assert!(
            matches!(
                error,
                NetError::Tls {
                    host: "api.github.com",
                    ..
                }
            ),
            "{error:?}"
        );
    }
}
