// 스킬 내용 해시: skills CLI 가 공개한 computedHash 형식(경로·길이·내용을 NUL 로 잇고 SHA-256)으로 모은 파일을 해시한다.
//! The `computedHash` the `skills` CLI publishes for a skill folder (its README, section "Lock File Content Hash").
//!
//! Files are taken in the byte order of their UTF-8 `/`-separated paths. A file without a NUL byte has every CRLF
//! turned into LF first; a file with one is kept as it is. Each file adds `path NUL length NUL data`, with the
//! decimal length of the data hashed, to one SHA-256, whose lower-case hex is the hash. No files hash to the
//! SHA-256 of nothing. Collecting the files (which folder entries count) is the caller's part.

use sha2::{Digest, Sha256};
use std::borrow::Cow;
use std::collections::BTreeMap;

/// The `computedHash` of collected files, keyed by their `/`-separated path inside the skill folder.
pub fn computed_hash(files: &BTreeMap<String, Vec<u8>>) -> String {
    let mut digest = Sha256::new();
    // A `String` key orders by its UTF-8 bytes, the published order.
    for (path, data) in files {
        let data = normalized(data);
        digest.update(path.as_bytes());
        digest.update([0]);
        digest.update(data.len().to_string().as_bytes());
        digest.update([0]);
        digest.update(&data);
    }
    digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Text (no NUL byte) with each CRLF read left to right turned into LF; anything else unchanged.
fn normalized(data: &[u8]) -> Cow<'_, [u8]> {
    if data.contains(&0) || !data.windows(2).any(|pair| pair == b"\r\n") {
        return Cow::Borrowed(data);
    }
    let mut out = Vec::with_capacity(data.len());
    let mut at = 0;
    while at < data.len() {
        if data[at] == b'\r' && data.get(at + 1) == Some(&b'\n') {
            at += 1;
            continue;
        }
        out.push(data[at]);
        at += 1;
    }
    Cow::Owned(out)
}

#[cfg(test)]
mod tests {
    use super::computed_hash;
    use std::collections::BTreeMap;

    fn files(entries: &[(&str, &[u8])]) -> BTreeMap<String, Vec<u8>> {
        entries
            .iter()
            .map(|(path, data)| (path.to_string(), data.to_vec()))
            .collect()
    }

    // The expected values below were computed by `tools/conformance/area_hashes.py` `computed_hash`, the
    // published algorithm the frozen vectors are checked with; the frozen vectors themselves run in the
    // conformance runner.

    #[test]
    fn crlf_text_hashes_as_lf_with_the_shorter_length() {
        let lf = "5805a9e9de77be7c41d25e5a04587010cfae6b51ffc64b5da9111845a86aa39e";
        assert_eq!(computed_hash(&files(&[("SKILL.md", b"a\nb\n")])), lf);
        assert_eq!(computed_hash(&files(&[("SKILL.md", b"a\r\nb\r\n")])), lf);
        assert_eq!(
            computed_hash(&files(&[("SKILL.md", b"\r\r\n")])),
            "e3ff8c1b1e7c258421af478a7d05e6d08108bb91404fb8cd08aa46148f17a3b1"
        );
    }

    #[test]
    fn a_file_with_a_nul_byte_keeps_its_crlf() {
        assert_eq!(
            computed_hash(&files(&[("bin/x", b"\0\r\n")])),
            "17a6695a5ddbf3b9b8a226e220a537e02c82bae7c3dc81f1c77d91c5565a3abf"
        );
    }

    #[test]
    fn paths_order_by_utf8_bytes() {
        assert_eq!(
            computed_hash(&files(&[
                ("a.md", b"y"),
                ("B.md", b"x"),
                ("docs/e.md", b"z")
            ])),
            "77d757fb91189ba9185fa7294a712e760393ab87ed82afebf2e0ee5e0833572b"
        );
        assert_eq!(
            computed_hash(&files(&[
                ("z.md", b"2"),
                ("\u{ac00}.md", b"3"),
                ("\u{e9}.md", b"1")
            ])),
            "fe355e8bdcb3abb9d0ead8ac2b6f5307e49cdb09e051a125bcbb2c390063e7e5"
        );
    }
}
