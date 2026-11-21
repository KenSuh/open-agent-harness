// 관리 블록 계획: v0.4.0 rules.py 의 표시줄 문법·한도·줄끝 규칙을 바이트 단위로 옮긴 순수 함수다. 아무것도 쓰지 않는다.
//! The v0.4.0 managed-block algorithm (v0.4:src/oah/rules.py:19-166), ported byte for byte. The block body is an
//! input, so the frozen fixtures pin the algorithm independently of the 1.0 body.

use super::error::{RulesError, RulesErrorKind as Kind};
use super::input::{AGENTS_MD, CLAUDE_MD, LOCAL_RULES, ProjectFile, RulesFiles};
use super::markers::{BEGIN, END, RESERVED, find_block};
use super::text::{Eol, eol_of, has_non_ascii_space, py_rstrip, py_strip, split_lines, strip_eol};
use super::thin::import_line;

const LANG_PREFIX: &str = "<!-- oah rules lang=";
pub const LANGS: [&str; 2] = ["en", "ko"];
/// Codex reads at most 32 KiB of project docs by default.
pub const AGENTS_BUDGET: usize = 32 * 1024;
pub const BLOCK_LIMIT: usize = 6 * 1024;

/// The block's language, detected from its second line or given.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lang {
    En,
    Ko,
}

impl Lang {
    pub fn as_str(self) -> &'static str {
        match self {
            Lang::En => "en",
            Lang::Ko => "ko",
        }
    }

    pub fn parse(text: &str) -> Option<Lang> {
        [Lang::En, Lang::Ko]
            .into_iter()
            .find(|lang| lang.as_str() == text)
    }
}

/// The body text per language (v0.4.0's packaged `block.<lang>.md`; 1.0's rendered workflow body).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockBodies {
    pub en: String,
    pub ko: String,
}

impl BlockBodies {
    pub fn get(&self, lang: Lang) -> &str {
        match lang {
            Lang::En => &self.en,
            Lang::Ko => &self.ko,
        }
    }
}

/// Everything [`plan`] reads. `lang` is the `--lang` value; `None` or empty means "detect, else `en`".
#[derive(Debug, Clone, Copy)]
pub struct RulesInput<'a> {
    pub files: &'a RulesFiles,
    pub lang: Option<&'a str>,
    pub bodies: &'a BlockBodies,
}

/// Both files' current and new bytes. A file whose `after` equals its `before` needs no write.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RulesPlan {
    pub lang: Lang,
    pub agents_before: Option<Vec<u8>>,
    pub agents_after: Vec<u8>,
    pub claude_before: Option<Vec<u8>>,
    pub claude_after: Vec<u8>,
    pub block_bytes: usize,
}

impl RulesPlan {
    /// The files that would change, as (project-relative path, new bytes), `AGENTS.md` first.
    pub fn writes(&self) -> Vec<(&'static str, &[u8])> {
        [
            (AGENTS_MD, &self.agents_before, &self.agents_after),
            (CLAUDE_MD, &self.claude_before, &self.claude_after),
        ]
        .into_iter()
        .filter(|(_, before, after)| before.as_deref() != Some(after.as_slice()))
        .map(|(name, _, after)| (name, after.as_slice()))
        .collect()
    }
}

pub(crate) fn decode<'a>(data: &'a [u8], name: &str) -> Result<&'a str, RulesError> {
    std::str::from_utf8(data).map_err(|error| {
        let message = format!("{name} is not valid UTF-8 ({error}); save it as UTF-8 and retry");
        RulesError::new(Kind::Encoding, message)
    })
}

/// The managed block, markers included, without a trailing line ending.
pub fn render_block(
    lang: &str,
    bodies: &BlockBodies,
    local: &str,
    eol: Eol,
) -> Result<String, RulesError> {
    let Some(parsed) = Lang::parse(lang) else {
        let message = format!("unknown language {lang:?}; use one of {}", LANGS.join(", "));
        return Err(RulesError::new(Kind::Lang, message));
    };
    let mut lines = vec![
        BEGIN.to_string(),
        format!(
            "{LANG_PREFIX}{lang}; managed by `oah rules sync`. Put project rules in {LOCAL_RULES}, not here. -->"
        ),
    ];
    lines.extend(
        bodies
            .get(parsed)
            .trim_end_matches('\n')
            .split('\n')
            .map(str::to_string),
    );
    let local = local.replace("\r\n", "\n").replace('\r', "\n");
    let local = local.trim_matches('\n');
    if local
        .split('\n')
        .any(|line| py_rstrip(line).starts_with(RESERVED))
    {
        let message = format!(
            "{LOCAL_RULES} contains a reserved rules marker line. Remove that marker, then run `oah rules sync` \
             again. Nothing was written."
        );
        return Err(RulesError::new(Kind::MarkerBroken, message));
    }
    if !py_strip(local).is_empty() {
        lines.extend([
            "".to_string(),
            format!("### Project rules ({LOCAL_RULES})"),
            "".to_string(),
        ]);
        lines.extend(local.split('\n').map(str::to_string));
    }
    lines.push(END.to_string());
    Ok(lines.join(eol.as_str()))
}

fn refuse_link(file: &ProjectFile, name: &str) -> Result<(), RulesError> {
    match file {
        ProjectFile::Link { target } => Err(RulesError::new(
            Kind::Link,
            format!(
                "{name} is a link to {}; oah never writes through links, which could reach global files. Replace \
                 it with a regular file, or add the block by hand.",
                target.display()
            ),
        )),
        _ => Ok(()),
    }
}

/// The language a found block declares on its second line, when it is a known one.
fn detected_lang(lines: &[&str], begin: usize, end: usize) -> Option<&'static str> {
    let line = strip_eol(lines.get(begin + 1).filter(|_| begin + 1 < end)?);
    let rest = line.strip_prefix(LANG_PREFIX)?;
    let word = &rest[..rest
        .find(|c: char| !c.is_ascii_lowercase())
        .unwrap_or(rest.len())];
    let found = (!word.is_empty() && rest[word.len()..].starts_with(';')).then_some(word)?;
    Lang::parse(found).map(Lang::as_str)
}

/// Computes `AGENTS.md`'s and `CLAUDE.md`'s new bytes and validates every limit; writes nothing.
pub fn plan(input: &RulesInput<'_>) -> Result<RulesPlan, RulesError> {
    let files = input.files;
    refuse_link(&files.agents, AGENTS_MD)?;
    refuse_link(&files.claude, CLAUDE_MD)?;
    let local = match &files.local_rules {
        Some(bytes) => decode(bytes, LOCAL_RULES)?,
        None => "",
    };
    let given = input.lang.filter(|lang| !lang.is_empty());
    let (lang, block, after) = match files.agents.bytes() {
        None => {
            let lang = given.unwrap_or("en");
            let block = render_block(lang, input.bodies, local, Eol::Lf)?;
            let after = format!("{block}\n").into_bytes();
            (lang, block, after)
        }
        Some(agents) => {
            let lines = split_lines(decode(agents, AGENTS_MD)?);
            match find_block(&lines)? {
                None => append(agents, given.unwrap_or("en"), input.bodies, local)?,
                Some((begin, end)) => {
                    let lang = given
                        .or_else(|| detected_lang(&lines, begin, end))
                        .unwrap_or("en");
                    let eol = match lines[begin] {
                        line if line.ends_with("\r\n") => Eol::Crlf,
                        line if line.ends_with('\n') => Eol::Lf,
                        _ => eol_of(agents),
                    };
                    let block = render_block(lang, input.bodies, local, eol)?;
                    let tail = &lines[end][strip_eol(lines[end]).len()..];
                    let after = [
                        lines[..begin].concat(),
                        block.clone(),
                        tail.to_string(),
                        lines[end + 1..].concat(),
                    ];
                    (lang, block, after.concat().into_bytes())
                }
            }
        }
    };
    find_block(&split_lines(decode(&after, AGENTS_MD)?))?;
    check_limits(&block, &after)?;
    let (claude_before, claude_after) = import_line(&files.claude, CLAUDE_MD)?;
    Ok(RulesPlan {
        lang: Lang::parse(lang).expect("render_block accepted the language"),
        agents_before: files.agents.bytes().map(<[u8]>::to_vec),
        agents_after: after,
        claude_before,
        claude_after,
        block_bytes: block.len(),
    })
}

/// A file without a block: append, keeping the file's line ending and adding one blank line when needed.
fn append<'l>(
    agents: &[u8],
    lang: &'l str,
    bodies: &BlockBodies,
    local: &str,
) -> Result<(&'l str, String, Vec<u8>), RulesError> {
    let eol = eol_of(agents);
    let block = render_block(lang, bodies, local, eol)?;
    let eol = eol.as_str().as_bytes();
    let mut after = agents.to_vec();
    if !after.is_empty() && !after.ends_with(b"\n") {
        after.extend_from_slice(eol);
    }
    if has_non_ascii_space(&after) && !after.ends_with(&[eol, eol].concat()) {
        after.extend_from_slice(eol);
    }
    after.extend_from_slice(block.as_bytes());
    after.extend_from_slice(eol);
    Ok((lang, block, after))
}

fn check_limits(block: &str, after: &[u8]) -> Result<(), RulesError> {
    if block.len() >= BLOCK_LIMIT {
        let message = format!(
            "the managed block would be {} bytes, over the {BLOCK_LIMIT}-byte limit. Shorten {LOCAL_RULES} (link a \
             wiki note for long project rules) and retry. Nothing was written.",
            block.len()
        );
        return Err(RulesError::new(Kind::BlockTooLarge, message));
    }
    if after.len() > AGENTS_BUDGET {
        let message = format!(
            "AGENTS.md would be {} bytes, over Codex's {AGENTS_BUDGET}-byte project-doc limit. Move long sections \
             into docs/wiki/ and link them, then retry. Nothing was written.",
            after.len()
        );
        return Err(RulesError::new(Kind::Budget, message));
    }
    Ok(())
}
