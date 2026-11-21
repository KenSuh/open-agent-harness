// 규칙 적합성 어댑터: 동결된 사례를 공개 규칙 API에 전달하고 읽기 및 출력 결과를 대조한다.
use super::{Case, Materialized};
use oah_core::rules::{
    BlockBodies, Eol, RulesFiles, RulesInput, RulesPlan, find_block, plan, render_block,
    split_lines,
};
use serde_json::{Map, Value as Json, json};

pub(super) fn run_case(
    case: &Case,
    deviation: bool,
    input: &Map<String, Json>,
    expect: &Json,
) -> Result<(), String> {
    if case.function != "oah.rules.plan" {
        return Err(format!(
            "the rules area has no operation for {:?}",
            case.function
        ));
    }
    if deviation {
        return Err("the rules area has no deviation adapters".into());
    }
    for key in input.keys() {
        if !["lang", "block_bodies", "files", "bytes"].contains(&key.as_str()) {
            return Err(format!("unknown rules input key {key:?}"));
        }
    }
    let lang = match input.get("lang") {
        Some(Json::Null) => None,
        Some(Json::String(lang)) => Some(lang.as_str()),
        _ => return Err("rules input lang must be null or a string".into()),
    };
    let block_bodies = input
        .get("block_bodies")
        .and_then(Json::as_object)
        .ok_or("rules input has no block_bodies object")?;
    let body = |lang: &str| {
        block_bodies
            .get(lang)
            .and_then(Json::as_str)
            .map(str::to_owned)
            .ok_or_else(|| format!("rules input has no {lang} block body"))
    };
    let bodies = BlockBodies {
        en: body("en")?,
        ko: body("ko")?,
    };
    let files = Materialized::new(input)?;
    let root = files.folder.path();
    let before = RulesFiles::read(root).map_err(|error| error.to_string())?;
    let result = plan(&RulesInput {
        files: &before,
        lang,
        bodies: &bodies,
    });
    if !files.unchanged() || RulesFiles::read(root).ok().as_ref() != Some(&before) {
        return Err("planning changed the project files".into());
    }
    match (result, expect.get("ok"), expect.get("error")) {
        (Ok(plan), Some(expected), None) => {
            let actual = planned_output(&plan, &bodies, before.local_rules.as_deref())?;
            if actual != *expected {
                return Err(format!("expected {expected}, found {actual}"));
            }
        }
        (Err(error), None, Some(expected)) => {
            if expected["code"].as_str() != Some(error.code())
                || expected["message"]
                    .as_str()
                    .is_some_and(|text| text != error.to_string())
            {
                return Err(format!(
                    "expected {expected}, found {}: {error}",
                    error.code()
                ));
            }
        }
        (Ok(_), _, _) => return Err("rules plan succeeded when an error was expected".into()),
        (Err(error), _, _) => return Err(format!("rules plan failed: {error}")),
    }
    Ok(())
}

fn planned_output(
    plan: &RulesPlan,
    bodies: &BlockBodies,
    local: Option<&[u8]>,
) -> Result<Json, String> {
    let agents = std::str::from_utf8(&plan.agents_after).map_err(|error| error.to_string())?;
    let claude = std::str::from_utf8(&plan.claude_after).map_err(|error| error.to_string())?;
    let local =
        std::str::from_utf8(local.unwrap_or_default()).map_err(|error| error.to_string())?;
    let lines = split_lines(agents);
    let (begin, end) = find_block(&lines)
        .map_err(|error| error.to_string())?
        .ok_or("the planned AGENTS.md has no managed block")?;
    let eol = if lines[begin].ends_with("\r\n") {
        Eol::Crlf
    } else {
        Eol::Lf
    };
    let rendered =
        render_block(plan.lang.as_str(), bodies, local, eol).map_err(|error| error.to_string())?;
    let span = lines[begin..=end].concat();
    if span.strip_suffix(eol.as_str()).unwrap_or(&span) != rendered {
        return Err("rendered block differs from the planned AGENTS.md block".into());
    }
    Ok(
        json!({ "lang": plan.lang.as_str(), "agents_after": agents, "claude_after": claude, "block_bytes": plan.block_bytes }),
    )
}
