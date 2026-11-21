// license 영역 어댑터: 라이선스 본문 분류·표현식·판정과 자격·매니페스트 선언 사례를 공개 라이선스 API 로 계산한다.
use super::{json_of, ok, only_keys, table, text, texts};
use crate::runner::{Case, decode_hex};
use oah_core::review::license::{
    classify_text, declared_from_manifest, eligibility, parse_expression, verdict,
};
use serde_json::{Map, Value as Json, json};
use std::collections::BTreeMap;

pub(super) fn run(case: &Case, input: &Map<String, Json>) -> Result<Json, String> {
    match case.function.as_str() {
        "oah.design.sources.license.classify_text" if input.contains_key("calls") => {
            table(input, &["text"], |row| {
                Ok(json_of!(classify_text(text(row, "text")?.as_bytes())))
            })
        }
        "oah.design.sources.license.classify_text" => {
            only_keys(input, &["bytes"], "a raw classify case")?;
            let bytes = input
                .get("bytes")
                .and_then(Json::as_object)
                .ok_or("a raw classify case has no bytes object")?;
            let [(name, hex)] = bytes.iter().collect::<Vec<_>>()[..] else {
                return Err("a raw classify case holds exactly one file".into());
            };
            if name != "LICENSE" {
                return Err(format!("a raw classify case reads LICENSE, not {name:?}"));
            }
            let data = decode_hex(hex.as_str().ok_or("input bytes LICENSE is not hex text")?)?;
            Ok(ok(json_of!(classify_text(&data))))
        }
        "oah.design.sources.license.parse_expression" => table(input, &["text"], |row| {
            Ok(match parse_expression(text(row, "text")?) {
                Some((form, ids)) => json!([form.as_str(), ids]),
                None => Json::Null,
            })
        }),
        "oah.design.sources.license.eligibility" => {
            table(input, &["evidence", "blobs", "allow"], eligible)
        }
        "oah.design.sources.license.declared_from_manifest" => {
            table(input, &["path", "text"], |row| {
                let (found, problem) =
                    declared_from_manifest(text(row, "path")?, text(row, "text")?.as_bytes());
                Ok(json!({"declared": found, "problem": problem}))
            })
        }
        other => Err(format!("the license area has no operation for {other:?}")),
    }
}

/// The verdict over a row's evidence and stored blobs, then eligibility under each allow list.
fn eligible(row: &Map<String, Json>) -> Result<Json, String> {
    let mut blobs: BTreeMap<String, Vec<u8>> = BTreeMap::new();
    if let Some(stored) = row.get("blobs") {
        let stored = stored.as_object().ok_or("blobs is not an object")?;
        for (sha256, data) in stored {
            let data = data
                .as_str()
                .ok_or_else(|| format!("blob {sha256} is not text"))?;
            blobs.insert(sha256.clone(), data.as_bytes().to_vec());
        }
    }
    let read = |sha256: &str| blobs.get(sha256).cloned();
    let found = verdict(row.get("evidence"), &read);
    let allows = row
        .get("allow")
        .and_then(Json::as_array)
        .ok_or("allow is not a list of allow lists")?;
    let mut answers = Vec::new();
    for allow in allows {
        answers.push(json_of!(eligibility(&found, &texts(Some(allow), "allow")?)));
    }
    Ok(json!({
        "verdict": {"expression": found.expression, "form": found.form, "ids": found.ids,
                    "problems": found.problems, "strict": found.strict},
        "eligibility": answers,
    }))
}
