// review 영역 어댑터: frontmatter·screening·references·identity·hit_paths 표 사례와 identity 거절, 기록 사례를 공개 검토 API 로 계산한다.
use super::{json_of, ok, only_keys, record, refused, table, text, texts};
use crate::runner::Case;
use oah_core::review::frontmatter::{frontmatter, parse_frontmatter};
use oah_core::review::identity::{hit_paths, identity};
use oah_core::review::screening::{references, screening};
use serde_json::{Map, Value as Json, json};
use std::collections::BTreeSet;

pub(super) fn run(case: &Case, input: &Map<String, Json>) -> Result<Json, String> {
    match case.function.as_str() {
        "oah.discovery.review._frontmatter" => table(input, &["text"], |row| {
            Ok(json_of!(frontmatter(text(row, "text")?)))
        }),
        "oah.skillsel.parse_frontmatter" => table(input, &["text"], |row| {
            Ok(json_of!(parse_frontmatter(text(row, "text")?)))
        }),
        "oah.discovery.review.screening" => table(input, &["text"], |row| {
            Ok(json_of!(screening(text(row, "text")?)))
        }),
        "oah.discovery.review.references" => table(input, &["text", "folder", "present"], |row| {
            let present: BTreeSet<String> =
                texts(row.get("present"), "present")?.into_iter().collect();
            Ok(json_of!(references(
                text(row, "text")?,
                text(row, "folder")?,
                &present
            )))
        }),
        "oah.discovery.review.identity" if input.contains_key("calls") => {
            table(input, &["repo", "path"], |row| {
                match identity(text(row, "repo")?, text(row, "path")?) {
                    Ok((found, _)) => Ok(json!([found.source_id, found.full, found.fields])),
                    Err(error) => Err(format!("identity refused a kept row: {error}")),
                }
            })
        }
        "oah.discovery.review.identity" => {
            only_keys(input, &["repo", "path"], "an identity case")?;
            Ok(match identity(text(input, "repo")?, text(input, "path")?) {
                Ok((found, _)) => ok(json!([found.source_id, found.full, found.fields])),
                Err(error) => refused(&error),
            })
        }
        "oah.discovery.review.hit_paths" => table(input, &["name"], |row| {
            Ok(json!(hit_paths(text(row, "name")?)))
        }),
        "oah.discovery.review.review" => record::run(input),
        other => Err(format!("the review area has no operation for {other:?}")),
    }
}
