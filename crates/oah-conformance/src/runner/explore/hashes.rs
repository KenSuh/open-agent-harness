// hashes 영역 어댑터: 사례가 준 스킬 파일들을 실제 oah-core computedHash 로 해시해 공개 벡터와 대조한다.
use super::{ok, only_keys};
use crate::runner::{Case, safe_relative};
use oah_core::skills::computed_hash;
use serde_json::{Map, Value as Json};
use std::collections::BTreeMap;

pub(super) fn run(case: &Case, input: &Map<String, Json>) -> Result<Json, String> {
    if case.function != "skills computedHash" {
        return Err(format!(
            "the hashes area has no operation for {:?}",
            case.function
        ));
    }
    only_keys(input, &["files"], "a hashes case")?;
    let given = input
        .get("files")
        .and_then(Json::as_object)
        .ok_or("a hashes case has no files object")?;
    let mut files = BTreeMap::new();
    for (path, data) in given {
        safe_relative(path)?;
        let data = data
            .as_str()
            .ok_or_else(|| format!("input file {path} is not text"))?;
        files.insert(path.clone(), data.as_bytes().to_vec());
    }
    Ok(ok(Json::String(computed_hash(&files))))
}
