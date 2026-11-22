// 검토·라이선스 핵심 직접 하네스: crate 내부 도우미까지 닿도록 review 모듈과 로컬 수집기를 경로로 붙여, 고정 v0.4.0 사례와 무해한 로컬 픽스처로 시험한다.
//! Direct harness for the M1-S3 review and license core.
//!
//! `src/review` and `src/registry/local.rs` are public in `lib.rs` too, but this test crate mounts them by path, with
//! the crate-private process runner the local collector uses, so the guards reach crate-private helpers;
//! `crate::config`, `crate::error` and `crate::paths` resolve to the library's own modules through the imports below.
//! The runner's unit tests are compiled into this crate as well, so they run here a second time.
//!
//! * `conformance`: every case of the frozen `conformance/v0.4.0/review` and `license` areas through the ported
//!   modules; the record cases are read from one-commit fixture repositories by the local collector.
//! * `discovery_cases`: license discovery the frozen cases omit (notices, nested licenses, a LICENSES folder, size
//!   and mode limits), against the answers v0.4.0 gave for the same fixtures (`review_core/discovery.json`).
//! * `gate_cases`: the install gate over those records and over benign variations of them.
//! * `collector_cases`: the local collector's refusals and its isolation from inherited git variables.

use oah_core::{config, error, paths};

#[expect(dead_code, reason = "the store only uses the regular-file opener here")]
#[path = "../src/fsx"]
mod fsx {
    mod atomic;
    mod error;
    mod selfwrite;

    pub(crate) use atomic::{RegularOpenError, open_regular};
    pub(crate) use selfwrite::{SelfWriteMatch, SelfWrites};
}

#[allow(dead_code)]
#[path = "../src/proc/mod.rs"]
mod proc;
#[allow(dead_code)]
#[path = "review_core/registry.rs"]
mod registry;
#[allow(dead_code, unused_imports)]
#[path = "../src/review/mod.rs"]
mod review;

#[path = "review_core/collector_cases.rs"]
mod collector_cases;
#[path = "review_core/conformance.rs"]
mod conformance;
#[path = "review_core/discovery_cases.rs"]
mod discovery_cases;
#[path = "review_core/fixtures.rs"]
mod fixtures;
#[path = "review_core/gate_cases.rs"]
mod gate_cases;
#[path = "review_core/guards.rs"]
mod guards;
