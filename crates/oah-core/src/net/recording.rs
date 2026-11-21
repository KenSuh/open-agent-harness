// 기록 전송: 주소마다 정해 둔 답을 돌려주고 보낸 요청을 모두 적어, 네트워크 없이 요청 모양과 실패를 시험한다.
//! [`RecordingTransport`]: answers from memory and records every request it receives, so request shapes, limits
//! and typed failures are tested without a network. An address has one scripted answer, given again on every
//! request for it; an address without one answers as offline. As with the real transport, only a 2xx answer has
//! a body: a scripted 2xx body longer than the request's limit is [`NetError::TooLarge`], and the scripted body
//! of any other status is dropped.

use super::{NetError, Offline, Request, Response, Transport};
use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, PoisonError};

/// A transport for tests and fixtures; it never opens a connection.
#[derive(Debug, Default)]
pub struct RecordingTransport {
    answers: Mutex<HashMap<String, Result<Response, NetError>>>,
    sent: Mutex<Vec<Request>>,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl RecordingTransport {
    pub fn new() -> RecordingTransport {
        RecordingTransport::default()
    }

    /// Answers every request for `url` with `response`, replacing an earlier script.
    pub fn answer(&self, url: impl Into<String>, response: Response) -> &RecordingTransport {
        lock(&self.answers).insert(url.into(), Ok(response));
        self
    }

    /// Fails every request for `url` with `error`, replacing an earlier script.
    pub fn fail(&self, url: impl Into<String>, error: NetError) -> &RecordingTransport {
        lock(&self.answers).insert(url.into(), Err(error));
        self
    }

    /// Every request received so far, in order.
    pub fn requests(&self) -> Vec<Request> {
        lock(&self.sent).clone()
    }

    /// Forgets the requests received so far; the scripts stay.
    pub fn clear(&self) {
        lock(&self.sent).clear();
    }
}

impl Transport for RecordingTransport {
    fn send(&self, request: &Request) -> Result<Response, NetError> {
        lock(&self.sent).push(request.clone());
        let host = request.row.host;
        let answer = lock(&self.answers).get(&request.url).cloned();
        let response = answer.unwrap_or_else(|| {
            Err(NetError::Offline {
                host,
                reason: Offline::Network,
                detail: "no recorded answer".to_string(),
            })
        })?;
        if !(200..300).contains(&response.status) {
            return Ok(Response {
                body: Vec::new(),
                ..response
            });
        }
        if u64::try_from(response.body.len()).unwrap_or(u64::MAX) > request.limit {
            return Err(NetError::TooLarge {
                host,
                limit: request.limit,
            });
        }
        Ok(response)
    }
}
