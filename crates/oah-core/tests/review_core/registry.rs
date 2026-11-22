// 하네스용 registry 자리: src/registry/mod.rs 대신 로컬 수집기만 같은 경로 이름으로 붙인다.
//! Stands in for `src/registry/mod.rs` so this harness mounts only the local collector, at its `registry::local` path.

#[path = "../../src/registry/local.rs"]
pub mod local;
