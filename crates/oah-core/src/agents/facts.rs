// 사실 파일: 컴파일에 넣은 facts.toml 을 (에이전트, 능력)마다 읽어 검사하고, 실행하는 시스템과 버전으로 확인됨·모름·없음을 정한다.
//! The facts file (CONTRACTS C8, §12.2): the one place a fact about an agent is written, compiled in, one entry per
//! (agent, capability) with its basis, source, systems and the open items it waits for. [`Facts::evaluate`]
//! applies the evidence rule of DECISIONS D-07 for the running system and the version a probe found.

use super::capability::{Basis, Capability, CapabilityName, Evidence, Os};
use super::steps::semver;
use crate::config::AgentId;
use std::collections::BTreeMap;
use std::sync::OnceLock;
use toml_edit::{Item, Table};

/// The shipped facts.
pub(crate) const FACTS_TOML: &str = include_str!("facts.toml");

const KEYS: [&str; 6] = [
    "status",
    "basis",
    "source",
    "os",
    "verified_on",
    "unverified",
];

/// Why a capability reads as unknown.
const NO_FACT: &str = "no fact is recorded for this capability";
const OPEN: &str = "the facts file records this as not verified";
const OTHER_SYSTEM: &str = "not verified on this system";
const DOCS_ONLY: &str =
    "documented, not measured; a write under the agent's home needs a measurement";
const NEWER_MAJOR: &str = "this version is a major step newer than the one measured";

/// What an entry says.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FactStatus {
    Verified,
    Unknown,
    None,
}

/// One (agent, capability) entry as written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fact {
    pub status: FactStatus,
    pub basis: Basis,
    pub source: String,
    pub os: Vec<Os>,
    /// The agent version a measurement saw; `None` unless the basis is `measured`.
    pub verified_on: Option<String>,
    /// The `[open]` items that must settle before the entry can be verified.
    pub unverified: Vec<String>,
}

/// A parsed facts file.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Facts {
    entries: BTreeMap<(AgentId, CapabilityName), Fact>,
    open: BTreeMap<String, String>,
}

/// A facts file that breaks the format; the message names the table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FactsError(pub String);

/// The shipped facts. A file that does not parse (the loader test prevents it) reads as no facts: every
/// capability unknown.
pub fn facts() -> &'static Facts {
    static FACTS: OnceLock<Facts> = OnceLock::new();
    FACTS.get_or_init(|| Facts::parse(FACTS_TOML).unwrap_or_default())
}

impl Facts {
    /// Reads `text`: one table per known agent with one entry per capability, all six keys in each, and an
    /// `[open]` table whose every item some entry references.
    pub fn parse(text: &str) -> Result<Facts, FactsError> {
        let document = toml_edit::ImDocument::parse(text).map_err(|error| {
            FactsError(format!("facts.toml is not valid TOML: {}", error.message()))
        })?;
        let root = document.as_table();
        let open = open_items(root)?;
        let mut entries = BTreeMap::new();
        for (key, item) in root.iter().filter(|(key, _)| *key != "open") {
            let agent = AgentId::parse(key)
                .ok_or_else(|| refuse(&format!("[{key}]"), "is not an agent"))?;
            for (name, entry) in table(item, &format!("[{key}]"))?.iter() {
                let where_ = format!("[{key}.{name}]");
                let name = CapabilityName::parse(name)
                    .ok_or_else(|| refuse(&where_, "is not a capability"))?;
                entries.insert((agent, name), fact(table(entry, &where_)?, &where_, &open)?);
            }
        }
        let missing = AgentId::ALL
            .into_iter()
            .flat_map(|agent| CapabilityName::ALL.map(|name| (agent, name)))
            .find(|pair| !entries.contains_key(pair));
        if let Some((agent, name)) = missing {
            return Err(refuse(
                &format!("[{agent}.{}]", name.as_str()),
                "is missing",
            ));
        }
        if let Some(item) = open
            .keys()
            .find(|item| !entries.values().any(|fact| fact.unverified.contains(item)))
        {
            return Err(refuse(
                "[open]",
                &format!("{item} is not referenced by any entry"),
            ));
        }
        Ok(Facts { entries, open })
    }

    pub fn get(&self, agent: AgentId, name: CapabilityName) -> Option<&Fact> {
        self.entries.get(&(agent, name))
    }

    /// The items still open, with what settles each.
    pub fn open(&self) -> &BTreeMap<String, String> {
        &self.open
    }

    /// The capability on `os` for an agent whose probe found `version`. An entry is used only for the systems its
    /// basis covers; a write-bearing capability with a documented basis reads as unknown; a measured one reads as
    /// unknown once `version` is a major step above the measured version (D-07, §12.2).
    pub fn evaluate(
        &'static self,
        agent: AgentId,
        name: CapabilityName,
        os: Os,
        version: Option<&str>,
    ) -> Capability<()> {
        let Some(fact) = self.get(agent, name) else {
            return Capability::Unknown { why: NO_FACT };
        };
        let why = if fact.status == FactStatus::Unknown {
            OPEN
        } else if !fact.os.contains(&os) {
            OTHER_SYSTEM
        } else if name.writes_agent_home() && fact.basis == Basis::Docs {
            DOCS_ONLY
        } else if fact.basis == Basis::Measured && newer_major(version, fact.verified_on.as_deref())
        {
            NEWER_MAJOR
        } else if fact.status == FactStatus::None {
            return Capability::None;
        } else {
            let evidence = Evidence {
                basis: fact.basis,
                source: &fact.source,
                verified_on: fact.verified_on.as_deref(),
                os: &fact.os,
            };
            return Capability::Verified {
                value: (),
                evidence,
            };
        };
        Capability::Unknown { why }
    }
}

fn newer_major(found: Option<&str>, measured: Option<&str>) -> bool {
    match (found.and_then(semver), measured.and_then(semver)) {
        (Some(found), Some(measured)) => found[0] > measured[0],
        _ => false,
    }
}

fn refuse(where_: &str, problem: &str) -> FactsError {
    FactsError(format!("facts.toml {where_} {problem}"))
}

fn table<'a>(item: &'a Item, where_: &str) -> Result<&'a Table, FactsError> {
    item.as_table()
        .ok_or_else(|| refuse(where_, "must be a table"))
}

fn open_items(root: &Table) -> Result<BTreeMap<String, String>, FactsError> {
    let Some(item) = root.get("open") else {
        return Ok(BTreeMap::new());
    };
    table(item, "[open]")?
        .iter()
        .map(|(key, value)| match value.as_str() {
            Some(text) => Ok((key.to_string(), text.to_string())),
            None => Err(refuse("[open]", &format!("{key} must be a string"))),
        })
        .collect()
}

fn fact(entry: &Table, where_: &str, open: &BTreeMap<String, String>) -> Result<Fact, FactsError> {
    if let Some((key, _)) = entry.iter().find(|(key, _)| !KEYS.contains(key)) {
        return Err(refuse(where_, &format!("has the unknown key {key}")));
    }
    let status = match text(entry, "status", where_)? {
        "verified" => FactStatus::Verified,
        "unknown" => FactStatus::Unknown,
        "none" => FactStatus::None,
        _ => return Err(refuse(where_, "status must be verified, unknown or none")),
    };
    let basis = match text(entry, "basis", where_)? {
        "carried" => Basis::Carried,
        "docs" => Basis::Docs,
        "measured" => Basis::Measured,
        _ => return Err(refuse(where_, "basis must be carried, docs or measured")),
    };
    let source = text(entry, "source", where_)?.to_string();
    let mut os = Vec::new();
    for name in texts(entry, "os", where_)? {
        match Os::parse(name) {
            Some(system) if !os.contains(&system) => os.push(system),
            _ => {
                return Err(refuse(
                    where_,
                    &format!("os has an unknown or repeated system {name}"),
                ));
            }
        }
    }
    let verified_on =
        Some(text(entry, "verified_on", where_)?).filter(|version| !version.is_empty());
    let unverified: Vec<String> = texts(entry, "unverified", where_)?
        .into_iter()
        .map(str::to_string)
        .collect();
    if let Some(item) = unverified.iter().find(|item| !open.contains_key(*item)) {
        return Err(refuse(
            where_,
            &format!("unverified names {item}, which [open] does not list"),
        ));
    }
    let measured = basis == Basis::Measured;
    if source.is_empty() {
        return Err(refuse(where_, "source is empty"));
    }
    if measured != verified_on.is_some_and(|version| semver(version).is_some()) {
        return Err(refuse(
            where_,
            "verified_on must be the measured version exactly when the basis is measured",
        ));
    }
    if status != FactStatus::Unknown && !unverified.is_empty() {
        return Err(refuse(
            where_,
            "unverified must be empty unless the status is unknown",
        ));
    }
    if status != FactStatus::Unknown && os.is_empty() {
        return Err(refuse(where_, "os must name the systems the basis covers"));
    }
    let verified_on = verified_on.map(str::to_string);
    Ok(Fact {
        status,
        basis,
        source,
        os,
        verified_on,
        unverified,
    })
}

fn text<'a>(entry: &'a Table, key: &str, where_: &str) -> Result<&'a str, FactsError> {
    entry
        .get(key)
        .and_then(Item::as_str)
        .ok_or_else(|| refuse(where_, &format!("{key} must be a string")))
}

fn texts<'a>(entry: &'a Table, key: &str, where_: &str) -> Result<Vec<&'a str>, FactsError> {
    let wrong = || refuse(where_, &format!("{key} must be a list of strings"));
    let list = entry.get(key).and_then(Item::as_array).ok_or_else(wrong)?;
    list.iter()
        .map(|value| value.as_str().ok_or_else(wrong))
        .collect()
}

#[cfg(test)]
#[path = "facts_tests.rs"]
mod tests;
