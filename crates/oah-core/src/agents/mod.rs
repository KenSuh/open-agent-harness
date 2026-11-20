// 에이전트 어댑터: 일곱 에이전트의 설치·로그인 확인을 한 확인 통로로 하고, 능력 근거와 v0.4.0 doctor 이식을 둔다.
//! The seven agent adapters (CONTRACTS C8, M1-S2): detection and sign-in through the [`Probe`] seam only,
//! capability evidence from one compiled-in facts file, and the v0.4.0 [`doctor`] functions the conformance area
//! pins. Nothing here writes: a missing agent gets no folder, link or file, and only a detection builds the
//! [`InstalledAgent`] witness later writers require.

mod adapters;
mod antigravity;
mod capability;
mod catalog;
mod check;
mod cli;
mod cursor;
mod detection;
pub mod doctor;
mod facts;
mod probe;
mod problem;
mod steps;
mod system;

pub use adapters::{AgentAdapter, Profile, adapter, profile};
pub use capability::{Basis, Capability, CapabilityName, CapabilityState, Evidence, Os};
pub use catalog::{Guide, Requirement, StatusCommand, Tool, catalog};
pub use check::{Check, SignInCheck, check_sign_in, detect};
pub use detection::{Detection, InstalledAgent, Kind, LoginHint, SignIn, Status, installed};
pub use facts::{Fact, FactStatus, Facts, FactsError, facts};
pub use probe::{Probe, Resolution, Resolved, RunError, RunOutput};
pub use problem::{AgentError, Failure};
pub use steps::{first_line, signed_in};
pub use system::{Configured, SystemProbe};
