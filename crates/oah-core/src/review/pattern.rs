// 작은 정규식 엔진: v0.4.0 이 쓰는 파이썬 re 문법의 부분집합을 같은 의미(되추적, 문자 단위, 대소문자 무시 규칙)로 해석한다.
//! A backtracking matcher for the subset of Python `re` syntax the v0.4.0 review and license code compiles.
//!
//! The crate has no regular-expression dependency, and the v0.4.0 patterns rely on Python's rules (Unicode `\w`,
//! `\s` and `\b`, case-insensitive literals and classes, `$` before a final newline, leftmost-first alternation,
//! greedy and lazy repeats), so the port keeps the original pattern text and reads it the same way.
//!
//! Supported: literals and escaped punctuation; `.`; `^`; `$`; `\A`; `\Z`; `\b`; `\B`; `\s`, `\S`,
//! `\w`, `\W`; classes with ranges, negation and those escapes; `(...)`, `(?:...)`, `(?P<name>...)`,
//! `(?i:...)`; `|`; `*`, `+`, `?`, `{n}`, `{n,}`, `{,m}`, `{n,m}` and their lazy forms. Anything
//! else (including `\d`, whose Unicode digit table the crate does not carry) is refused when the pattern is
//! compiled, so an unsupported construct is never read as something else. An unbounded repeat of something that
//! can match the empty string is refused too. Text is matched by `char`, as Python matches by code point.
//!
//! The backtracking runs on an explicit stack, and a repeat of one character keeps one stack entry, as Python's
//! `sre` does, so a long line costs heap, not call depth.

use super::pystr::{extra_case, is_space, is_word, lower_char, upper_char};
use std::collections::HashSet;
use std::fmt;

/// The most instructions one pattern may compile to; the v0.4.0 patterns need a few thousand.
const PROGRAM_LIMIT: usize = 100_000;
/// Maximum bitset size for failed-state memoization (8 MiB = 67_108_864 bits); larger matrices fall back to a set.
const BITSET_BIT_LIMIT: usize = 64 << 20;

/// A pattern that did not compile, with the character offset of the problem.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct PatternError {
    at: usize,
    message: String,
}

impl fmt::Display for PatternError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at position {}", self.message, self.at)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Look {
    Start,
    End,
    TextStart,
    TextEnd,
    Boundary,
    NotBoundary,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Category {
    Space,
    NotSpace,
    Word,
    NotWord,
}

impl Category {
    fn contains(self, c: char) -> bool {
        match self {
            Category::Space => is_space(c),
            Category::NotSpace => !is_space(c),
            Category::Word => is_word(c),
            Category::NotWord => !is_word(c),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Item {
    Char(char),
    Range(char, char),
    Category(Category),
}

impl Item {
    fn contains(&self, c: char) -> bool {
        match *self {
            Item::Char(item) => item == c,
            Item::Range(low, high) => low <= c && c <= high,
            Item::Category(category) => category.contains(c),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Class {
    negated: bool,
    items: Vec<Item>,
}

impl Class {
    /// Python's rule: with case ignored, the lower-case form of the character is tested, and a literal or range
    /// also accepts it through its upper-case form or Python's extra case pairs.
    fn contains(&self, c: char, fold: bool) -> bool {
        let hit = if fold {
            let lower = lower_char(c);
            self.items.iter().any(|item| match item {
                Item::Category(category) => category.contains(lower),
                _ => {
                    item.contains(lower)
                        || item.contains(upper_char(lower))
                        || extra_case(lower).is_some_and(|other| item.contains(other))
                }
            })
        } else {
            self.items.iter().any(|item| item.contains(c))
        };
        hit != self.negated
    }
}

/// Something that matches exactly one character.
#[derive(Debug, Clone)]
enum Single {
    Char(char, bool),
    Any,
    Class(usize, bool),
}

#[derive(Debug, Clone)]
enum Node {
    Empty,
    One(Single),
    Look(Look),
    Group(Box<Node>, Option<usize>),
    Concat(Vec<Node>),
    Alternate(Vec<Node>),
    Repeat {
        node: Box<Node>,
        min: usize,
        max: Option<usize>,
        greedy: bool,
    },
}

fn nullable(node: &Node) -> bool {
    match node {
        Node::Empty | Node::Look(_) => true,
        Node::One(_) => false,
        Node::Group(inner, _) => nullable(inner),
        Node::Concat(items) => items.iter().all(nullable),
        Node::Alternate(branches) => branches.iter().any(nullable),
        Node::Repeat { node, min, .. } => *min == 0 || nullable(node),
    }
}

#[derive(Debug, Clone)]
enum Inst {
    One(Single),
    Repeat {
        one: Single,
        min: usize,
        max: usize,
        greedy: bool,
    },
    Split(usize, usize),
    Jump(usize),
    Save(usize),
    Look(Look),
    Match,
}

/// A compiled pattern.
#[derive(Debug, Clone)]
pub(super) struct Pattern {
    insts: Vec<Inst>,
    classes: Vec<Class>,
    slots: usize,
    names: Vec<(String, usize)>,
    memo_slot: Vec<Option<usize>>,
    memo_count: usize,
    anchored_start: bool,
}

/// Failed `(memo_slot, pos)` states for one search or `all` pass, preventing repeated scanning work.
enum Failed {
    Bits { stride: usize, words: Vec<u64> },
    Sparse(HashSet<(usize, usize)>),
}

impl Failed {
    fn new(memo_count: usize, text_len: usize) -> Self {
        let stride = text_len + 1;
        match memo_count.checked_mul(stride) {
            Some(bits) if bits <= BITSET_BIT_LIMIT => Failed::Bits {
                stride,
                words: vec![0u64; bits.div_ceil(64)],
            },
            _ => Failed::Sparse(HashSet::new()),
        }
    }

    fn contains(&self, slot: usize, pos: usize) -> bool {
        match self {
            Failed::Bits { stride, words } => {
                let index = slot * *stride + pos;
                (words[index / 64] & (1u64 << (index % 64))) != 0
            }
            Failed::Sparse(set) => set.contains(&(slot, pos)),
        }
    }

    fn insert(&mut self, slot: usize, pos: usize) {
        match self {
            Failed::Bits { stride, words } => {
                let index = slot * *stride + pos;
                words[index / 64] |= 1u64 << (index % 64);
            }
            Failed::Sparse(set) => {
                set.insert((slot, pos));
            }
        }
    }
}

fn build_memo_slots(insts: &[Inst]) -> (Vec<Option<usize>>, usize) {
    let mut marked = vec![false; insts.len()];
    for (pc, inst) in insts.iter().enumerate() {
        match *inst {
            Inst::Repeat { .. } => {
                marked[pc] = true;
                if pc + 1 < insts.len() {
                    marked[pc + 1] = true;
                }
            }
            Inst::Split(first, second) => {
                marked[pc] = true;
                if first < insts.len() {
                    marked[first] = true;
                }
                if second < insts.len() {
                    marked[second] = true;
                }
            }
            Inst::Jump(target) => {
                if target < insts.len() {
                    marked[target] = true;
                }
            }
            Inst::One(_) | Inst::Save(_) | Inst::Look(_) | Inst::Match => {}
        }
    }
    let mut next = 0;
    let slots = marked
        .into_iter()
        .map(|is_memo| {
            if is_memo {
                let id = next;
                next += 1;
                Some(id)
            } else {
                None
            }
        })
        .collect();
    (slots, next)
}

/// One match: group spans in characters of the searched text.
#[derive(Debug)]
pub(super) struct Found<'a> {
    chars: &'a [char],
    slots: Vec<Option<usize>>,
    names: &'a [(String, usize)],
}

impl Found<'_> {
    /// The character offset where the match ends.
    pub(super) fn end(&self) -> usize {
        self.slots.get(1).copied().flatten().unwrap_or(0)
    }

    /// The text of group `index` (0 is the whole match), or `None` when the group did not take part.
    pub(super) fn group(&self, index: usize) -> Option<String> {
        let start = self.slots.get(2 * index).copied().flatten()?;
        let end = self.slots.get(2 * index + 1).copied().flatten()?;
        (start <= end).then(|| self.chars[start..end].iter().collect())
    }

    /// The text of a named group.
    pub(super) fn name(&self, name: &str) -> Option<String> {
        let index = self.names.iter().find(|(known, _)| known == name)?.1;
        self.group(index)
    }

    /// The numbered groups after group 0, as Python's `match.groups()`.
    pub(super) fn groups(&self) -> Vec<Option<String>> {
        (1..self.slots.len() / 2)
            .map(|index| self.group(index))
            .collect()
    }
}

impl Pattern {
    /// Compiles `source`; `ignore_case` is Python's `re.IGNORECASE` for the whole pattern.
    pub(super) fn new(source: &str, ignore_case: bool) -> Result<Pattern, PatternError> {
        let mut parser = Parser {
            chars: source.chars().collect(),
            at: 0,
            classes: Vec::new(),
            groups: 0,
            names: Vec::new(),
        };
        let root = parser.alternation(ignore_case)?;
        if parser.at < parser.chars.len() {
            return Err(parser.error("unbalanced parenthesis"));
        }
        let mut program = Program {
            insts: vec![Inst::Save(0)],
        };
        program.emit(&root);
        program.insts.push(Inst::Save(1));
        program.insts.push(Inst::Match);
        if program.insts.len() > PROGRAM_LIMIT {
            return Err(PatternError {
                at: 0,
                message: "the pattern is too large".to_string(),
            });
        }
        let (memo_slot, memo_count) = build_memo_slots(&program.insts);
        let anchored_start = matches!(
            program.insts.get(1),
            Some(Inst::Look(Look::Start | Look::TextStart))
        );
        Ok(Pattern {
            insts: program.insts,
            classes: parser.classes,
            slots: 2 * (parser.groups + 1),
            names: parser.names,
            memo_slot,
            memo_count,
            anchored_start,
        })
    }

    /// A pattern fixed in the source; it compiles, as every one is tested.
    pub(super) fn fixed(source: &str, ignore_case: bool) -> Pattern {
        Pattern::new(source, ignore_case).unwrap_or_else(|error| {
            panic!("the fixed pattern {source:?} does not compile: {error}")
        })
    }

    /// Python's `pattern.search(text, from)`: the leftmost match starting at or after `from`.
    pub(super) fn search<'a>(&'a self, chars: &'a [char], from: usize) -> Option<Found<'a>> {
        self.search_steps(chars, from).0
    }

    /// [`Self::search`] returning both the match and the exact VM step count (for deterministic work checks).
    pub(super) fn search_steps<'a>(
        &'a self,
        chars: &'a [char],
        from: usize,
    ) -> (Option<Found<'a>>, usize) {
        let mut failed = Failed::new(self.memo_count, chars.len());
        let mut steps = 0usize;
        let found = self.search_with_memo(chars, from, &mut failed, &mut steps);
        (found, steps)
    }

    fn search_with_memo<'a>(
        &'a self,
        chars: &'a [char],
        from: usize,
        failed: &mut Failed,
        steps: &mut usize,
    ) -> Option<Found<'a>> {
        if self.anchored_start {
            return if from == 0 {
                self.run(chars, 0, false, failed, steps)
            } else {
                None
            };
        }
        (from..=chars.len()).find_map(|start| self.run(chars, start, false, failed, steps))
    }

    /// Python's `pattern.fullmatch(text)`.
    pub(super) fn full<'a>(&'a self, chars: &'a [char]) -> Option<Found<'a>> {
        let mut failed = Failed::new(self.memo_count, chars.len());
        let mut steps = 0usize;
        self.run(chars, 0, true, &mut failed, &mut steps)
    }

    /// Python's `pattern.match(text)`: a match that starts at the beginning.
    pub(super) fn prefix<'a>(&'a self, chars: &'a [char]) -> Option<Found<'a>> {
        let mut failed = Failed::new(self.memo_count, chars.len());
        let mut steps = 0usize;
        self.run(chars, 0, false, &mut failed, &mut steps)
    }

    /// Python's `finditer`: every match, left to right, each search starting where the last match ended.
    #[cfg(test)]
    pub(super) fn all<'a>(&'a self, chars: &'a [char]) -> Vec<Found<'a>> {
        self.all_steps(chars).0
    }

    /// [`Self::all`] returning both matches and exact VM step count (for deterministic work checks).
    pub(super) fn all_steps<'a>(&'a self, chars: &'a [char]) -> (Vec<Found<'a>>, usize) {
        let mut found = Vec::new();
        let mut failed = Failed::new(self.memo_count, chars.len());
        let mut steps = 0usize;
        let mut from = 0;
        while from <= chars.len() {
            let Some(next) = self.search_with_memo(chars, from, &mut failed, &mut steps) else {
                break;
            };
            let start = next.slots[0].unwrap_or(from);
            let end = next.end();
            from = if end == start { end + 1 } else { end };
            found.push(next);
        }
        (found, steps)
    }

    /// Whether the whole text matches.
    pub(super) fn is_full(&self, text: &str) -> bool {
        let chars: Vec<char> = text.chars().collect();
        self.full(&chars).is_some()
    }

    fn one(&self, one: &Single, c: char) -> bool {
        match *one {
            Single::Char(expected, fold) => same_char(expected, c, fold),
            Single::Any => c != '\n',
            Single::Class(index, fold) => self.classes[index].contains(c, fold),
        }
    }

    fn run<'a>(
        &'a self,
        chars: &'a [char],
        start: usize,
        whole: bool,
        failed: &mut Failed,
        steps: &mut usize,
    ) -> Option<Found<'a>> {
        enum Frame {
            Branch {
                pc: usize,
                pos: usize,
            },
            Restore {
                slot: usize,
                value: Option<usize>,
            },
            MarkFailed {
                slot: usize,
                pos: usize,
            },
            Repeat {
                pc: usize,
                start: usize,
                count: usize,
            },
        }
        let mut slots = vec![None; self.slots];
        let mut stack: Vec<Frame> = Vec::new();
        let (mut pc, mut pos) = (0, start);
        loop {
            *steps += 1;
            let memo = self.memo_slot[pc];
            let advanced = if let Some(slot) = memo
                && failed.contains(slot, pos)
            {
                false
            } else {
                match &self.insts[pc] {
                    Inst::One(one) => {
                        if let Some(slot) = memo {
                            stack.push(Frame::MarkFailed { slot, pos });
                        }
                        let hit = pos < chars.len() && self.one(one, chars[pos]);
                        if hit {
                            pos += 1;
                            pc += 1;
                        }
                        hit
                    }
                    Inst::Repeat {
                        one,
                        min,
                        max,
                        greedy,
                    } => {
                        let unbounded = *min == 0 && *max == usize::MAX;
                        if unbounded {
                            let repeat_slot = memo.expect("repeat has memo slot");
                            if *greedy {
                                let mut count = 0;
                                while pos + count < chars.len()
                                    && self.one(one, chars[pos + count])
                                    && !failed.contains(repeat_slot, pos + count + 1)
                                {
                                    *steps += 1;
                                    count += 1;
                                }
                                stack.push(Frame::Repeat {
                                    pc,
                                    start: pos,
                                    count,
                                });
                                pos += count;
                                pc += 1;
                                true
                            } else {
                                stack.push(Frame::Repeat {
                                    pc,
                                    start: pos,
                                    count: 0,
                                });
                                pc += 1;
                                true
                            }
                        } else {
                            if let Some(slot) = memo {
                                stack.push(Frame::MarkFailed { slot, pos });
                            }
                            let reach = if *greedy { *max } else { *min };
                            let mut count = 0;
                            while count < reach
                                && pos + count < chars.len()
                                && self.one(one, chars[pos + count])
                            {
                                *steps += 1;
                                count += 1;
                            }
                            if count < *min {
                                false
                            } else {
                                stack.push(Frame::Repeat {
                                    pc,
                                    start: pos,
                                    count,
                                });
                                pos += count;
                                pc += 1;
                                true
                            }
                        }
                    }
                    Inst::Split(first, second) => {
                        if let Some(slot) = memo {
                            stack.push(Frame::MarkFailed { slot, pos });
                        }
                        stack.push(Frame::Branch { pc: *second, pos });
                        pc = *first;
                        true
                    }
                    Inst::Jump(target) => {
                        if let Some(slot) = memo {
                            stack.push(Frame::MarkFailed { slot, pos });
                        }
                        pc = *target;
                        true
                    }
                    Inst::Save(slot_idx) => {
                        if let Some(slot) = memo {
                            stack.push(Frame::MarkFailed { slot, pos });
                        }
                        stack.push(Frame::Restore {
                            slot: *slot_idx,
                            value: slots[*slot_idx],
                        });
                        slots[*slot_idx] = Some(pos);
                        pc += 1;
                        true
                    }
                    Inst::Look(look) => {
                        if let Some(slot) = memo {
                            stack.push(Frame::MarkFailed { slot, pos });
                        }
                        let hit = holds(*look, chars, pos);
                        if hit {
                            pc += 1;
                        }
                        hit
                    }
                    Inst::Match => {
                        if !whole || pos == chars.len() {
                            return Some(Found {
                                chars,
                                slots,
                                names: &self.names,
                            });
                        }
                        false
                    }
                }
            };
            if advanced {
                continue;
            }
            loop {
                match stack.pop()? {
                    Frame::Restore { slot, value } => slots[slot] = value,
                    Frame::MarkFailed { slot, pos: at } => failed.insert(slot, at),
                    Frame::Branch { pc: next, pos: at } => {
                        pc = next;
                        pos = at;
                        break;
                    }
                    Frame::Repeat {
                        pc: at,
                        start: from,
                        count,
                    } => {
                        let Inst::Repeat {
                            one,
                            min,
                            max,
                            greedy,
                        } = &self.insts[at]
                        else {
                            continue;
                        };
                        let unbounded = *min == 0 && *max == usize::MAX;
                        if unbounded {
                            let repeat_slot = self.memo_slot[at].expect("repeat has memo slot");
                            if *greedy {
                                failed.insert(repeat_slot, from + count);
                                if count > 0 {
                                    let next_count = count - 1;
                                    stack.push(Frame::Repeat {
                                        pc: at,
                                        start: from,
                                        count: next_count,
                                    });
                                    pc = at + 1;
                                    pos = from + next_count;
                                    break;
                                }
                            } else {
                                let more = from + count < chars.len()
                                    && self.one(one, chars[from + count])
                                    && !failed.contains(repeat_slot, from + count + 1);
                                if more {
                                    *steps += 1;
                                    let next_count = count + 1;
                                    stack.push(Frame::Repeat {
                                        pc: at,
                                        start: from,
                                        count: next_count,
                                    });
                                    pc = at + 1;
                                    pos = from + next_count;
                                    break;
                                }
                                for k in 0..=count {
                                    failed.insert(repeat_slot, from + k);
                                }
                            }
                        } else {
                            let next = if *greedy {
                                (count > *min).then(|| count - 1)
                            } else {
                                let more = count < *max
                                    && from + count < chars.len()
                                    && self.one(one, chars[from + count]);
                                more.then_some(count + 1)
                            };
                            if let Some(next_count) = next {
                                stack.push(Frame::Repeat {
                                    pc: at,
                                    start: from,
                                    count: next_count,
                                });
                                pc = at + 1;
                                pos = from + next_count;
                                break;
                            }
                        }
                    }
                }
            }
        }
    }
}

/// Python's literal comparison: with case ignored, the lower-case forms are equal, or form one of the extra pairs.
fn same_char(expected: char, c: char, fold: bool) -> bool {
    if expected == c {
        return true;
    }
    if !fold {
        return false;
    }
    let (expected, c) = (lower_char(expected), lower_char(c));
    expected == c || extra_case(expected) == Some(c)
}

fn holds(look: Look, chars: &[char], pos: usize) -> bool {
    let len = chars.len();
    match look {
        Look::Start | Look::TextStart => pos == 0,
        Look::End => pos == len || (pos + 1 == len && chars[pos] == '\n'),
        Look::TextEnd => pos == len,
        Look::Boundary | Look::NotBoundary => {
            // As in sre, neither assertion holds in an empty text.
            if len == 0 {
                return false;
            }
            let before = pos > 0 && is_word(chars[pos - 1]);
            let after = pos < len && is_word(chars[pos]);
            (before != after) == (look == Look::Boundary)
        }
    }
}

struct Parser {
    chars: Vec<char>,
    at: usize,
    classes: Vec<Class>,
    groups: usize,
    names: Vec<(String, usize)>,
}

impl Parser {
    fn error(&self, message: impl Into<String>) -> PatternError {
        PatternError {
            at: self.at,
            message: message.into(),
        }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.at).copied()
    }

    fn eat(&mut self, c: char) -> bool {
        if self.peek() == Some(c) {
            self.at += 1;
            true
        } else {
            false
        }
    }

    fn next_char(&mut self) -> Option<char> {
        let c = self.peek()?;
        self.at += 1;
        Some(c)
    }

    fn expect_close(&mut self) -> Result<(), PatternError> {
        if self.eat(')') {
            Ok(())
        } else {
            Err(self.error("missing ), unterminated subpattern"))
        }
    }

    fn push_class(&mut self, class: Class) -> usize {
        self.classes.push(class);
        self.classes.len() - 1
    }

    fn alternation(&mut self, fold: bool) -> Result<Node, PatternError> {
        let mut branches = vec![self.concat(fold)?];
        while self.eat('|') {
            branches.push(self.concat(fold)?);
        }
        Ok(if branches.len() == 1 {
            branches.swap_remove(0)
        } else {
            Node::Alternate(branches)
        })
    }

    fn concat(&mut self, fold: bool) -> Result<Node, PatternError> {
        let mut items = Vec::new();
        while let Some(c) = self.peek() {
            if c == '|' || c == ')' {
                break;
            }
            let atom = self.atom(fold)?;
            items.push(self.quantified(atom)?);
        }
        Ok(match items.len() {
            0 => Node::Empty,
            1 => items.swap_remove(0),
            _ => Node::Concat(items),
        })
    }

    /// `{n}`, `{n,}`, `{,m}` or `{n,m}` at the current position, consumed; `None` (nothing consumed) when the brace is a
    /// literal, as in Python.
    fn counted(&mut self) -> Result<Option<(usize, Option<usize>)>, PatternError> {
        let here = self.at;
        if !self.eat('{') || self.peek() == Some('}') {
            self.at = here;
            return Ok(None);
        }
        let low = self.digits();
        let high = if self.eat(',') {
            self.digits()
        } else {
            low.clone()
        };
        if !self.eat('}') {
            self.at = here;
            return Ok(None);
        }
        let number = |text: &str| {
            text.parse::<usize>().map_err(|_| PatternError {
                at: here,
                message: "the repeat count is too large".to_string(),
            })
        };
        let min = if low.is_empty() { 0 } else { number(&low)? };
        let max = if high.is_empty() {
            None
        } else {
            Some(number(&high)?)
        };
        if max.is_some_and(|max| max < min) {
            return Err(PatternError {
                at: here,
                message: "min repeat greater than max repeat".to_string(),
            });
        }
        Ok(Some((min, max)))
    }

    fn digits(&mut self) -> String {
        let mut text = String::new();
        while let Some(c) = self.peek().filter(char::is_ascii_digit) {
            text.push(c);
            self.at += 1;
        }
        text
    }

    fn quantified(&mut self, node: Node) -> Result<Node, PatternError> {
        let (min, max) = match self.peek() {
            Some('*') => (0, None),
            Some('+') => (1, None),
            Some('?') => (0, Some(1)),
            Some('{') => match self.counted()? {
                Some(bounds) => {
                    self.at -= 1;
                    bounds
                }
                None => return Ok(node),
            },
            _ => return Ok(node),
        };
        self.at += 1;
        let greedy = !self.eat('?');
        if matches!(node, Node::Look(_) | Node::Empty) {
            return Err(self.error("nothing to repeat"));
        }
        if max.is_none() && nullable(&node) {
            return Err(self.error("an unbounded repeat of something that can match nothing"));
        }
        if matches!(self.peek(), Some('*' | '+' | '?'))
            || (self.peek() == Some('{') && self.counted()?.is_some())
        {
            return Err(self.error("multiple repeat"));
        }
        Ok(Node::Repeat {
            node: Box::new(node),
            min,
            max,
            greedy,
        })
    }

    fn atom(&mut self, fold: bool) -> Result<Node, PatternError> {
        if self.peek() == Some('{') {
            if self.counted()?.is_some() {
                return Err(self.error("nothing to repeat"));
            }
            self.at += 1;
            return Ok(Node::One(Single::Char('{', fold)));
        }
        let Some(c) = self.next_char() else {
            return Ok(Node::Empty);
        };
        Ok(match c {
            '(' => self.group(fold)?,
            '[' => {
                let class = self.class()?;
                Node::One(Single::Class(self.push_class(class), fold))
            }
            '.' => Node::One(Single::Any),
            '^' => Node::Look(Look::Start),
            '$' => Node::Look(Look::End),
            '\\' => self.escape(fold)?,
            '*' | '+' | '?' => return Err(self.error("nothing to repeat")),
            c => Node::One(Single::Char(c, fold)),
        })
    }

    fn group(&mut self, fold: bool) -> Result<Node, PatternError> {
        if !self.eat('?') {
            self.groups += 1;
            let index = self.groups;
            let inner = self.alternation(fold)?;
            self.expect_close()?;
            return Ok(Node::Group(Box::new(inner), Some(index)));
        }
        if self.eat(':') {
            let inner = self.alternation(fold)?;
            self.expect_close()?;
            return Ok(Node::Group(Box::new(inner), None));
        }
        if self.eat('P') {
            if !self.eat('<') {
                return Err(self.error("unsupported group syntax"));
            }
            let mut name = String::new();
            loop {
                match self.next_char() {
                    Some('>') => break,
                    Some(c) if c == '_' || c.is_alphanumeric() => name.push(c),
                    _ => return Err(self.error("bad character in group name")),
                }
            }
            if name.is_empty() || name.starts_with(|c: char| c.is_ascii_digit()) {
                return Err(self.error("bad group name"));
            }
            if self.names.iter().any(|(known, _)| *known == name) {
                return Err(self.error("redefinition of a group name"));
            }
            self.groups += 1;
            let index = self.groups;
            self.names.push((name, index));
            let inner = self.alternation(fold)?;
            self.expect_close()?;
            return Ok(Node::Group(Box::new(inner), Some(index)));
        }
        // Scoped flags: only `(?i:...)` and `(?-i:...)` are supported.
        let scoped = if self.eat('i') {
            true
        } else if self.eat('-') && self.eat('i') {
            false
        } else {
            return Err(self.error("unsupported group syntax"));
        };
        if !self.eat(':') {
            return Err(self.error("unsupported group syntax"));
        }
        let inner = self.alternation(scoped)?;
        self.expect_close()?;
        Ok(Node::Group(Box::new(inner), None))
    }

    fn escape(&mut self, fold: bool) -> Result<Node, PatternError> {
        let Some(c) = self.next_char() else {
            return Err(self.error("bad escape (end of pattern)"));
        };
        let category = match c {
            'b' => return Ok(Node::Look(Look::Boundary)),
            'B' => return Ok(Node::Look(Look::NotBoundary)),
            'A' => return Ok(Node::Look(Look::TextStart)),
            'Z' => return Ok(Node::Look(Look::TextEnd)),
            's' => Category::Space,
            'S' => Category::NotSpace,
            'w' => Category::Word,
            'W' => Category::NotWord,
            c => return Ok(Node::One(Single::Char(self.escaped(c)?, fold))),
        };
        let class = Class {
            negated: false,
            items: vec![Item::Category(category)],
        };
        Ok(Node::One(Single::Class(self.push_class(class), fold)))
    }

    /// The character an escape stands for: a control-character letter, or escaped punctuation as itself.
    fn escaped(&self, c: char) -> Result<char, PatternError> {
        Ok(match c {
            'n' => '\n',
            't' => '\t',
            'r' => '\r',
            'f' => '\u{c}',
            'v' => '\u{b}',
            'a' => '\u{7}',
            c if c.is_ascii_alphanumeric() => {
                return Err(self.error(format!("unsupported escape \\{c}")));
            }
            c => c,
        })
    }

    /// An escape inside a class: a category, or a character (where `\b` is a backspace, as in Python).
    fn class_escape(&mut self) -> Result<Result<Item, char>, PatternError> {
        let Some(c) = self.next_char() else {
            return Err(self.error("unterminated character set"));
        };
        Ok(match c {
            's' => Ok(Item::Category(Category::Space)),
            'S' => Ok(Item::Category(Category::NotSpace)),
            'w' => Ok(Item::Category(Category::Word)),
            'W' => Ok(Item::Category(Category::NotWord)),
            'b' => Err('\u{8}'),
            c => Err(self.escaped(c)?),
        })
    }

    fn class(&mut self) -> Result<Class, PatternError> {
        let negated = self.eat('^');
        let mut items = Vec::new();
        loop {
            let Some(c) = self.next_char() else {
                return Err(self.error("unterminated character set"));
            };
            if c == ']' && !items.is_empty() {
                break;
            }
            let start = if c == '\\' {
                match self.class_escape()? {
                    Ok(item) => {
                        items.push(item);
                        continue;
                    }
                    Err(c) => c,
                }
            } else {
                c
            };
            let range = self.peek() == Some('-')
                && self.chars.get(self.at + 1).is_some_and(|&next| next != ']');
            if !range {
                items.push(Item::Char(start));
                continue;
            }
            self.at += 1;
            let end = match self.next_char() {
                Some('\\') => match self.class_escape()? {
                    Err(c) => c,
                    Ok(_) => return Err(self.error("bad character range")),
                },
                Some(c) => c,
                None => return Err(self.error("unterminated character set")),
            };
            if end < start {
                return Err(self.error("bad character range"));
            }
            items.push(Item::Range(start, end));
        }
        Ok(Class { negated, items })
    }
}

struct Program {
    insts: Vec<Inst>,
}

impl Program {
    fn emit(&mut self, node: &Node) {
        match node {
            Node::Empty => {}
            Node::One(one) => self.insts.push(Inst::One(one.clone())),
            Node::Look(look) => self.insts.push(Inst::Look(*look)),
            Node::Group(inner, Some(index)) => {
                self.insts.push(Inst::Save(2 * index));
                self.emit(inner);
                self.insts.push(Inst::Save(2 * index + 1));
            }
            Node::Group(inner, None) => self.emit(inner),
            Node::Concat(items) => items.iter().for_each(|item| self.emit(item)),
            Node::Alternate(branches) => {
                let mut jumps = Vec::new();
                for (index, branch) in branches.iter().enumerate() {
                    if index + 1 == branches.len() {
                        self.emit(branch);
                        break;
                    }
                    let split = self.insts.len();
                    self.insts.push(Inst::Split(split + 1, split + 1));
                    self.emit(branch);
                    jumps.push(self.insts.len());
                    self.insts.push(Inst::Jump(0));
                    self.insts[split] = Inst::Split(split + 1, self.insts.len());
                }
                let end = self.insts.len();
                for jump in jumps {
                    self.insts[jump] = Inst::Jump(end);
                }
            }
            Node::Repeat {
                node,
                min,
                max,
                greedy,
            } => {
                if let Node::One(one) = node.as_ref() {
                    if let Some(max) = *max {
                        self.insts.push(Inst::Repeat {
                            one: one.clone(),
                            min: *min,
                            max,
                            greedy: *greedy,
                        });
                    } else {
                        if *min <= 8 {
                            for _ in 0..*min {
                                self.insts.push(Inst::One(one.clone()));
                            }
                        } else {
                            self.insts.push(Inst::Repeat {
                                one: one.clone(),
                                min: *min,
                                max: *min,
                                greedy: true,
                            });
                        }
                        self.insts.push(Inst::Repeat {
                            one: one.clone(),
                            min: 0,
                            max: usize::MAX,
                            greedy: *greedy,
                        });
                    }
                    return;
                }
                for _ in 0..*min {
                    self.emit(node);
                    if self.insts.len() > PROGRAM_LIMIT {
                        return;
                    }
                }
                let split = |at: usize, out: usize| {
                    if *greedy {
                        Inst::Split(at + 1, out)
                    } else {
                        Inst::Split(out, at + 1)
                    }
                };
                match max {
                    None => {
                        let at = self.insts.len();
                        self.insts.push(Inst::Jump(0));
                        self.emit(node);
                        self.insts.push(Inst::Jump(at));
                        self.insts[at] = split(at, self.insts.len());
                    }
                    Some(max) => {
                        let mut splits = Vec::new();
                        for _ in *min..*max {
                            splits.push(self.insts.len());
                            self.insts.push(Inst::Jump(0));
                            self.emit(node);
                            if self.insts.len() > PROGRAM_LIMIT {
                                return;
                            }
                        }
                        let out = self.insts.len();
                        for at in splits {
                            self.insts[at] = split(at, out);
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
#[path = "pattern_tests.rs"]
mod tests;
