use super::detection::{Detection, Status};
use super::probe::{Probe, Resolution, Resolved};
use super::steps::semver;
use crate::config::AgentId;
use serde::de::{DeserializeOwned, IgnoredAny};
use serde::{Deserialize, Serialize};
use std::time::Duration;

const DEADLINE: Duration = Duration::from_secs(4);
const CONNECTIONS: &[&str] = &["auth", "list", "--format", "json", "--log-level", "error"];
const DEFAULT_MODEL: &[&str] = &["api", "GET", "/api/model/default", "--log-level", "error"];

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct OpenCodeDetails {
    pub saved_connections: Option<usize>,
    pub default_model: Option<String>,
}

pub(super) fn supports(detection: &Detection) -> bool {
    detection.agent == AgentId::Opencode
        && detection.status == Status::SignInUnknown
        && detection
            .version
            .as_deref()
            .and_then(|version| version.strip_prefix("opencode "))
            .and_then(semver)
            .is_some_and(|version| version[0] == 2 && version >= [2, 4, 1])
}

#[derive(Deserialize)]
struct Integration {
    connections: Vec<IgnoredAny>,
}

#[derive(Deserialize)]
struct DefaultModel {
    data: Model,
}

#[derive(Deserialize)]
struct Model {
    #[serde(rename = "providerID")]
    provider: String,
    #[serde(rename = "modelID")]
    model: String,
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty() && value.len() <= 160 && !value.chars().any(char::is_control)
}

fn read_json<T: DeserializeOwned>(
    probe: &dyn Probe,
    program: &Resolved,
    args: &[&str],
) -> Option<T> {
    let run = probe.run(program, args, DEADLINE).ok()?;
    if run.exit != Some(0) {
        return None;
    }
    serde_json::from_str(&run.stdout).ok()
}

pub(super) fn read(probe: &dyn Probe) -> OpenCodeDetails {
    let Resolution::Found(program) = probe.resolve("opencode") else {
        return OpenCodeDetails::default();
    };
    let saved_connections = read_json::<Vec<Integration>>(probe, &program, CONNECTIONS)
        .map(|integrations| integrations.iter().map(|item| item.connections.len()).sum());
    let default_model = read_json::<DefaultModel>(probe, &program, DEFAULT_MODEL).and_then(
        |DefaultModel {
             data: Model { provider, model },
         }| {
            (valid_identifier(&provider) && valid_identifier(&model))
                .then(|| format!("{provider}/{model}"))
        },
    );
    OpenCodeDetails {
        saved_connections,
        default_model,
    }
}
