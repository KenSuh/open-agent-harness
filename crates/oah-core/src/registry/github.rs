// GitHub 수집기: 제한된 HTTPS 클라이언트로 기본 브랜치를 커밋에 고정하고, 트리 목록과 받은 파일을 git SHA-1 로 검증해 검토 핵심에 넘긴다.
//! [`GitHub`]: the collector of a skill folder in a public GitHub repository (DESIGN-C2 §5.2; §9.3 rows 2 and 3).
//!
//! One observation sends, through the one bounded [`Client`]:
//!
//! 1. `GET https://api.github.com/repos/{owner}/{repo}/commits/HEAD` with `Accept: application/vnd.github.sha`:
//!    the default branch's commit as 40 hex digits. Every later request names that commit, so the review reads one
//!    commit however the branch moves meanwhile. An empty repository (409) or a `HEAD` GitHub cannot resolve (422)
//!    is blocked as `ref-missing`, as the local collector blocks an unborn `HEAD`.
//! 2. `GET https://api.github.com/repos/{owner}/{repo}/git/trees/{commit}?recursive=1`: the whole tree at that
//!    commit. A truncated listing fails as `oversize`. Every tree object is rebuilt from the listing and hashed with
//!    Git's SHA-1; each must equal its listed id and the root the answer's `sha`, so no entry can be added, dropped
//!    or changed without the ids disagreeing.
//! 3. `GET https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{path}` for each file the review reads (the
//!    skill folder, the governing license and notice files), never with a token. Each body must have its listed size
//!    and hash to its blob id (`sha1("blob <len>\\0" + data)`) before the snapshot records its SHA-256. A blob read
//!    twice is fetched once.
//!
//! The commit object itself is not fetched or hashed: GitHub resolves the commit to its root tree, and the listing
//! is verified from that root down. The user's token, when set, goes only on the two API requests. A failed
//! observation keeps its typed cause for [`GitHub::take_cause`], so a caller can report `OAH_NET_*` with the
//! retry time rather than only the v0.4.0 failure word the observation carries.

use crate::net::{
    Client, GITHUB_API, GITHUB_RAW, NetError, Offline, RecordingTransport, Request, Response,
    Token, Transport,
};
use crate::review::identity::SourceSpec;
use crate::review::snapshot::{
    CollectError, Collector, Entry, FILE_LIMIT, Observation, Tree, snapshot,
};
use crate::tasks::CancelToken;
use serde::Deserialize;
use serde_json::{Value, json};
use sha1::{Digest, Sha1};
use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// The REST API version every API request names.
const API_VERSION: &str = "2022-11-28";
/// The longest one API request may take.
const API_DEADLINE: Duration = Duration::from_secs(30);
/// The longest one file download may take.
const RAW_DEADLINE: Duration = Duration::from_secs(60);
/// The longest a whole observation may take, as for the local collector.
const OBSERVE_DEADLINE: Duration = Duration::from_secs(120);
/// The largest commit answer read: 40 hex digits and some room.
const COMMIT_LIMIT: u64 = 4 << 10;
/// The largest tree listing read, as the local collector bounds `ls-tree`.
const LISTING_LIMIT: u64 = 16 << 20;

/// Why an observation failed, kept for [`GitHub::take_cause`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Cause {
    Cancelled,
    /// The client's typed failure, or an answer whose status the collector does not accept.
    Net(NetError),
    /// GitHub answered, but the answer does not verify; `failure` is its v0.4.0 failure word.
    Invalid {
        failure: &'static str,
        detail: String,
    },
}

impl Cause {
    /// The stable `OAH_NET_*` code of the cause, when it has one.
    pub fn code(&self) -> Option<&'static str> {
        match self {
            Cause::Net(error) => error.code(),
            Cause::Cancelled | Cause::Invalid { .. } => None,
        }
    }

    /// The v0.4.0 failure word the observation reports.
    pub fn failure(&self) -> &'static str {
        match self {
            Cause::Cancelled => "cancelled",
            Cause::Net(error) => error.failure(),
            Cause::Invalid { failure, .. } => failure,
        }
    }
}

impl fmt::Display for Cause {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Cause::Cancelled => {
                f.write_str("the review was cancelled before sending another request")
            }
            Cause::Net(error) => error.fmt(f),
            Cause::Invalid { detail, .. } => f.write_str(detail),
        }
    }
}

impl std::error::Error for Cause {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Cause::Net(error) => Some(error),
            Cause::Cancelled | Cause::Invalid { .. } => None,
        }
    }
}

fn invalid(detail: impl Into<String>) -> Cause {
    Cause::Invalid {
        failure: "bad-response",
        detail: detail.into(),
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Reads one GitHub skill folder at the default branch's current commit through a transport.
pub struct GitHub<'a> {
    client: Client<'a>,
    token: Option<Token>,
    cancel: Option<&'a CancelToken>,
    cause: Mutex<Option<Cause>>,
}

impl fmt::Debug for GitHub<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("GitHub")
            .field("token", &self.token.as_ref().map(|_| "<redacted>"))
            .finish_non_exhaustive()
    }
}

impl<'a> GitHub<'a> {
    /// A collector that sends through `transport`; `token` goes only on API requests.
    pub fn new(transport: &'a dyn Transport, token: Option<Token>) -> GitHub<'a> {
        GitHub {
            client: Client::new(transport),
            token,
            cancel: None,
            cause: Mutex::new(None),
        }
    }

    pub(crate) fn with_cancel(mut self, cancel: &'a CancelToken) -> Self {
        self.cancel = Some(cancel);
        self
    }

    /// The cause of the last failed observation, once.
    pub fn take_cause(&self) -> Option<Cause> {
        lock(&self.cause).take()
    }

    /// Keeps the first cause of this observation and gives the collector error that carries it.
    fn fail(&self, cause: Cause) -> CollectError {
        let message = cause.to_string();
        let mut slot = lock(&self.cause);
        if slot.is_none() {
            *slot = Some(cause);
        }
        CollectError { message }
    }

    /// A request's deadline: its own, cut to what is left of the observation's.
    fn deadline(
        &self,
        host: &'static str,
        until: Instant,
        own: Duration,
    ) -> Result<Duration, CollectError> {
        remaining(until, own).ok_or_else(|| {
            self.fail(Cause::Net(NetError::Offline {
                host,
                reason: Offline::Timeout,
                detail: format!("the review ran out of its {} s", OBSERVE_DEADLINE.as_secs()),
            }))
        })
    }

    fn send(&self, request: &Request) -> Result<Response, Cause> {
        if self.cancel.is_some_and(CancelToken::is_cancelled) {
            return Err(Cause::Cancelled);
        }
        self.client.send(request).map_err(Cause::Net)
    }

    /// One API request with GitHub's headers and, when set, the token.
    fn api(
        &self,
        url: String,
        accept: &str,
        limit: u64,
        until: Instant,
    ) -> Result<Response, CollectError> {
        let deadline = self.deadline(GITHUB_API.host, until, API_DEADLINE)?;
        let mut request = Request::get(GITHUB_API, url, limit, deadline)
            .header("accept", accept)
            .header("x-github-api-version", API_VERSION);
        if let Some(token) = &self.token {
            request = request.bearer(token);
        }
        self.send(&request).map_err(|cause| self.fail(cause))
    }

    fn attempt(&self, spec: &SourceSpec) -> Result<Observation, CollectError> {
        let until = Instant::now() + OBSERVE_DEADLINE;
        let Some((owner, repo)) = repository(&spec.locator) else {
            return Err(self.fail(Cause::Invalid {
                failure: "attempt-failed",
                detail: format!(
                    "{:?} is not a GitHub repository address this collector reads",
                    spec.locator
                ),
            }));
        };
        let base = format!("https://api.github.com/repos/{owner}/{repo}");
        let answer = self.api(
            format!("{base}/commits/HEAD"),
            "application/vnd.github.sha",
            COMMIT_LIMIT,
            until,
        )?;
        let commit = match answer.status {
            200 => {
                let text = std::str::from_utf8(&answer.body).unwrap_or_default().trim();
                if !is_sha1(text) {
                    return Err(self.fail(invalid(
                        "GitHub answered the commit of HEAD with something that is not a commit id",
                    )));
                }
                text.to_string()
            }
            409 | 422 => {
                return Ok(Observation::Blocked {
                    problems: vec!["ref-missing".to_string()],
                });
            }
            status => {
                return Err(self.fail(Cause::Net(NetError::Status {
                    host: GITHUB_API.host,
                    status,
                })));
            }
        };
        let answer = self.api(
            format!("{base}/git/trees/{commit}?recursive=1"),
            "application/vnd.github+json",
            LISTING_LIMIT,
            until,
        )?;
        if answer.status != 200 {
            return Err(self.fail(Cause::Net(NetError::Status {
                host: GITHUB_API.host,
                status: answer.status,
            })));
        }
        let listing = Listing::parse(&answer.body).map_err(|cause| self.fail(cause))?;
        let tree = AtCommit {
            github: self,
            owner,
            repo,
            commit: &commit,
            listing: &listing,
            until,
            raw_retry_used: Cell::new(false),
            fetched: RefCell::new(HashMap::new()),
        };
        Ok(Observation::Snapshot(snapshot(
            &tree,
            &commit,
            &listing.root,
            spec,
        )?))
    }
}

impl Collector for GitHub<'_> {
    fn observe(&self, spec: &SourceSpec) -> Observation {
        *lock(&self.cause) = None;
        self.attempt(spec).unwrap_or_else(|error| {
            let mut slot = lock(&self.cause);
            let cause = slot.get_or_insert_with(|| invalid(error.message));
            Observation::Failed {
                code: cause.failure().to_string(),
            }
        })
    }
}

fn remaining(until: Instant, limit: Duration) -> Option<Duration> {
    until
        .checked_duration_since(Instant::now())
        .filter(|left| !left.is_zero())
        .map(|left| left.min(limit))
}

fn immediate_raw_limit(cause: &Cause) -> bool {
    matches!(cause, Cause::Net(NetError::RateLimited { host, retry_after: Some(0), reset_at })
    if *host == GITHUB_RAW.host && reset_at.is_none_or(|reset| {
        SystemTime::now().duration_since(UNIX_EPOCH).is_ok_and(|elapsed| elapsed.as_secs() >= reset)
    }))
}

/// `(owner, repo)` of a canonical `https://github.com/<owner>/<repo>` locator, as the review spec writes it.
fn repository(locator: &str) -> Option<(&str, &str)> {
    let (owner, repo) = locator
        .strip_prefix("https://github.com/")?
        .split_once('/')?;
    let owner_ok = !owner.is_empty()
        && owner
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-');
    let repo_ok = !repo.is_empty()
        && !matches!(repo, "." | "..")
        && repo.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        });
    (owner_ok && repo_ok).then_some((owner, repo))
}

fn is_sha1(text: &str) -> bool {
    text.len() == 40
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// The Git object id of `content` stored as `kind`: `sha1("<kind> <len>\\0" + content)`.
fn object_id(kind: &str, content: &[u8]) -> String {
    let mut hasher = Sha1::new();
    hasher.update(format!("{kind} {}\0", content.len()).as_bytes());
    hasher.update(content);
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// The 20 bytes of a checked 40-digit id.
fn id_bytes(id: &str) -> Vec<u8> {
    id.as_bytes()
        .chunks(2)
        .map(|pair| {
            let digit = |byte: u8| match byte {
                b'0'..=b'9' => byte - b'0',
                _ => byte - b'a' + 10,
            };
            digit(pair[0]) << 4 | digit(pair[1])
        })
        .collect()
}

/// The folder of a listed path; `""` for the top folder.
fn parent(path: &str) -> &str {
    path.rsplit_once('/').map_or("", |(folder, _)| folder)
}

fn base_name(path: &str) -> &str {
    path.rsplit_once('/').map_or(path, |(_, name)| name)
}

/// What a name sorts by inside its tree: a tree's name with a `/` after it (Git's tree order).
fn tree_key(item: &Item) -> Vec<u8> {
    let mut key = base_name(&item.path).as_bytes().to_vec();
    if item.kind == Kind::Tree {
        key.push(b'/');
    }
    key
}

/// A listed path the raw host can be asked for: no `.` or `..` part it could resolve away.
fn fetchable(path: &str) -> bool {
    path.split('/').all(|part| !matches!(part, "" | "." | ".."))
}

/// Each part of `path` percent-encoded, every byte outside the unreserved set.
fn encode_path(path: &str) -> String {
    let mut out = String::with_capacity(path.len());
    for (index, part) in path.split('/').enumerate() {
        if index > 0 {
            out.push('/');
        }
        for byte in part.bytes() {
            if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
                out.push(char::from(byte));
            } else {
                out.push_str(&format!("%{byte:02X}"));
            }
        }
    }
    out
}

#[derive(Deserialize)]
struct TreeAnswer {
    sha: String,
    tree: Vec<TreeItem>,
    truncated: bool,
}

#[derive(Deserialize)]
struct TreeItem {
    path: String,
    mode: String,
    #[serde(rename = "type")]
    kind: String,
    sha: String,
    #[serde(default)]
    size: Option<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Blob,
    Tree,
    Commit,
}

/// One verified listing entry.
struct Item {
    path: String,
    mode: String,
    kind: Kind,
    oid: String,
}

struct Blob {
    size: u64,
    regular_paths: Vec<String>,
}

/// A verified recursive listing: the root tree id and every entry in path order.
struct Listing {
    root: String,
    items: Vec<Item>,
    blobs: HashMap<String, Blob>,
}

impl Listing {
    fn parse(body: &[u8]) -> Result<Listing, Cause> {
        let answer: TreeAnswer = serde_json::from_slice(body)
            .map_err(|_| invalid("the tree listing is not GitHub's tree answer"))?;
        if answer.truncated {
            return Err(Cause::Invalid {
                failure: "oversize",
                detail: "GitHub truncated the tree listing (over 100,000 entries or 7 MB), so it cannot be \
                         verified"
                    .to_string(),
            });
        }
        if !is_sha1(&answer.sha) {
            return Err(invalid("the tree listing names no root tree id"));
        }
        let mut items = Vec::with_capacity(answer.tree.len());
        let mut blobs: HashMap<String, Blob> = HashMap::new();
        let mut seen = HashSet::new();
        for raw in answer.tree {
            let path = raw.path;
            let unusable = path.is_empty()
                || path.starts_with('/')
                || path.ends_with('/')
                || path.contains("//")
                || path.contains('\0');
            if unusable {
                return Err(invalid(format!(
                    "the tree listing holds an unusable path {path:?}"
                )));
            }
            let kind = match raw.kind.as_str() {
                "blob" => Kind::Blob,
                "tree" => Kind::Tree,
                "commit" => Kind::Commit,
                other => {
                    return Err(invalid(format!(
                        "{path} is listed with the unknown type {other:?}"
                    )));
                }
            };
            let octal =
                raw.mode.len() == 6 && raw.mode.bytes().all(|byte| matches!(byte, b'0'..=b'7'));
            let mode_ok = match kind {
                Kind::Tree => raw.mode == "040000",
                Kind::Commit => raw.mode == "160000",
                Kind::Blob => octal && !matches!(raw.mode.as_str(), "040000" | "160000"),
            };
            if !mode_ok {
                return Err(invalid(format!(
                    "{path} is listed with mode {:?} for its type",
                    raw.mode
                )));
            }
            if !is_sha1(&raw.sha) {
                return Err(invalid(format!(
                    "{path} is listed without a commit-style object id"
                )));
            }
            if kind == Kind::Blob {
                let Some(size) = raw.size else {
                    return Err(invalid(format!("{path} is listed without its size")));
                };
                let blob = blobs.entry(raw.sha.clone()).or_insert(Blob {
                    size,
                    regular_paths: Vec::new(),
                });
                if blob.size != size {
                    return Err(invalid(format!(
                        "blob {} is listed with two sizes",
                        raw.sha
                    )));
                }
                if matches!(raw.mode.as_str(), "100644" | "100755") {
                    blob.regular_paths.push(path.clone());
                }
            }
            if !seen.insert(path.clone()) {
                return Err(invalid(format!("{path} is listed twice")));
            }
            items.push(Item {
                path,
                mode: raw.mode,
                kind,
                oid: raw.sha,
            });
        }
        items.sort_by(|left, right| left.path.as_bytes().cmp(right.path.as_bytes()));
        for blob in blobs.values_mut() {
            blob.regular_paths.sort();
        }
        verify_trees(&answer.sha, &items)?;
        Ok(Listing {
            root: answer.sha,
            items,
            blobs,
        })
    }
}

/// Rebuilds every tree object from the listing and checks it against its listed id, the root against `root`.
fn verify_trees(root: &str, items: &[Item]) -> Result<(), Cause> {
    let mut folders: BTreeMap<&str, (&str, Vec<&Item>)> = BTreeMap::new();
    folders.insert("", (root, Vec::new()));
    for item in items.iter().filter(|item| item.kind == Kind::Tree) {
        folders.insert(item.path.as_str(), (item.oid.as_str(), Vec::new()));
    }
    for item in items {
        match folders.get_mut(parent(&item.path)) {
            Some((_, children)) => children.push(item),
            None => {
                return Err(invalid(format!(
                    "{} is listed without its folder",
                    item.path
                )));
            }
        }
    }
    for (folder, (listed, mut children)) in folders {
        children.sort_by_cached_key(|item| tree_key(item));
        let mut content = Vec::new();
        for child in children {
            content.extend_from_slice(child.mode.trim_start_matches('0').as_bytes());
            content.push(b' ');
            content.extend_from_slice(base_name(&child.path).as_bytes());
            content.push(0);
            content.extend_from_slice(&id_bytes(&child.oid));
        }
        let computed = object_id("tree", &content);
        if computed != listed {
            let name = if folder.is_empty() {
                "the top tree"
            } else {
                folder
            };
            return Err(invalid(format!(
                "the tree listing does not verify: {name} is listed as {listed}, its entries hash to {computed}"
            )));
        }
    }
    Ok(())
}

/// The verified tree of one commit, read the way the local collector reads one.
struct AtCommit<'c, 'a> {
    github: &'c GitHub<'a>,
    owner: &'c str,
    repo: &'c str,
    commit: &'c str,
    listing: &'c Listing,
    until: Instant,
    raw_retry_used: Cell<bool>,
    fetched: RefCell<HashMap<String, Vec<u8>>>,
}

impl AtCommit<'_, '_> {
    fn blob(&self, oid: &str) -> Result<&Blob, CollectError> {
        self.listing.blobs.get(oid).ok_or_else(|| {
            self.github
                .fail(invalid(format!("blob {oid} is not in the tree listing")))
        })
    }
}

impl Tree for AtCommit<'_, '_> {
    /// Blobs and submodules at or under `path` (all of them for `""`) in path order, as `ls-tree -r` lists them;
    /// a submodule has no blob id.
    fn entries(&self, path: &str) -> Result<Vec<Entry>, CollectError> {
        let prefix = format!("{}/", path.trim_end_matches('/'));
        Ok(self
            .listing
            .items
            .iter()
            .filter(|item| item.kind != Kind::Tree)
            .filter(|item| path.is_empty() || item.path == path || item.path.starts_with(&prefix))
            .map(|item| Entry {
                mode: item.mode.clone(),
                oid: if item.kind == Kind::Blob {
                    item.oid.clone()
                } else {
                    String::new()
                },
                path: item.path.clone(),
            })
            .collect())
    }

    /// The direct children of `folder` with their ids, in Git's tree order, as `ls-tree` lists them.
    fn children(&self, folder: &str) -> Result<Vec<Entry>, CollectError> {
        let folder = folder.trim_end_matches('/');
        let mut found: Vec<&Item> = self
            .listing
            .items
            .iter()
            .filter(|item| parent(&item.path) == folder)
            .collect();
        found.sort_by_cached_key(|item| tree_key(item));
        Ok(found
            .into_iter()
            .map(|item| Entry {
                mode: item.mode.clone(),
                oid: item.oid.clone(),
                path: item.path.clone(),
            })
            .collect())
    }

    fn size(&self, blob: &str) -> Result<u64, CollectError> {
        Ok(self.blob(blob)?.size)
    }

    fn read(&self, blob: &str) -> Result<Vec<u8>, CollectError> {
        if let Some(data) = self.fetched.borrow().get(blob) {
            return Ok(data.clone());
        }
        let listed = self.blob(blob)?;
        if listed.size > FILE_LIMIT {
            return Err(self.github.fail(Cause::Invalid {
                failure: "oversize",
                detail: format!("blob {blob} is larger than {FILE_LIMIT} bytes"),
            }));
        }
        let Some(path) = listed.regular_paths.iter().find(|path| fetchable(path)) else {
            return Err(self.github.fail(invalid(format!(
                "blob {blob} is listed at no path the file host can be asked for"
            ))));
        };
        let url = format!(
            "https://raw.githubusercontent.com/{}/{}/{}/{}",
            self.owner,
            self.repo,
            self.commit,
            encode_path(path)
        );
        let deadline = self
            .github
            .deadline(GITHUB_RAW.host, self.until, RAW_DEADLINE)?;
        let mut request = Request::get(GITHUB_RAW, url, listed.size.max(1), deadline);
        let mut answer = self.github.send(&request);
        if !self.raw_retry_used.get()
            && answer.as_ref().is_err_and(immediate_raw_limit)
            && let Some(deadline) = remaining(self.until, RAW_DEADLINE)
        {
            self.raw_retry_used.set(true);
            request.deadline = deadline;
            answer = self.github.send(&request);
        }
        let answer = answer.map_err(|cause| self.github.fail(cause))?;
        if answer.status != 200 {
            return Err(self.github.fail(Cause::Net(NetError::Status {
                host: GITHUB_RAW.host,
                status: answer.status,
            })));
        }
        let length = u64::try_from(answer.body.len()).unwrap_or(u64::MAX);
        if length != listed.size || object_id("blob", &answer.body) != blob {
            return Err(self.github.fail(invalid(format!(
                "{path} from {} does not match its blob id {blob}",
                GITHUB_RAW.host
            ))));
        }
        self.fetched
            .borrow_mut()
            .insert(blob.to_string(), answer.body.clone());
        Ok(answer.body)
    }
}

/// A public GitHub repository as recorded answers, for tests and fixtures without a network. From file contents it
/// computes the blob and tree ids Git gives them, then the commit answer, the recursive tree listing and the raw
/// file answers one review asks for, and scripts them onto a [`RecordingTransport`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Recorded {
    owner: String,
    repo: String,
    commit: String,
    entries: BTreeMap<String, RecordedEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum RecordedEntry {
    Blob { mode: &'static str, data: Vec<u8> },
    Submodule { commit: String },
}

/// One node of a recorded tree while its ids are computed.
struct Node {
    path: String,
    mode: &'static str,
    kind: Kind,
    oid: String,
    size: Option<u64>,
}

impl Recorded {
    /// An empty repository `owner/repo` whose default branch is at `commit` (40 hex digits). The names are kept
    /// as given; the review spec writes them in lowercase.
    pub fn new(repository: &str, commit: &str) -> Recorded {
        let (owner, repo) = repository.split_once('/').unwrap_or((repository, ""));
        Recorded {
            owner: owner.to_string(),
            repo: repo.to_string(),
            commit: commit.to_string(),
            entries: BTreeMap::new(),
        }
    }

    /// Adds a regular file (mode `100644`).
    pub fn file(self, path: &str, data: impl Into<Vec<u8>>) -> Recorded {
        self.blob(path, "100644", data.into())
    }

    /// Adds an executable file (mode `100755`).
    pub fn executable(self, path: &str, data: impl Into<Vec<u8>>) -> Recorded {
        self.blob(path, "100755", data.into())
    }

    /// Adds a symbolic link to `target` (mode `120000`).
    pub fn symlink(self, path: &str, target: &str) -> Recorded {
        self.blob(path, "120000", target.as_bytes().to_vec())
    }

    /// Adds a submodule at `commit` (mode `160000`).
    pub fn submodule(mut self, path: &str, commit: &str) -> Recorded {
        let entry = RecordedEntry::Submodule {
            commit: commit.to_string(),
        };
        self.entries.insert(path.to_string(), entry);
        self
    }

    fn blob(mut self, path: &str, mode: &'static str, data: Vec<u8>) -> Recorded {
        self.entries
            .insert(path.to_string(), RecordedEntry::Blob { mode, data });
        self
    }

    /// The default branch's commit.
    pub fn commit(&self) -> &str {
        &self.commit
    }

    fn api_base(&self) -> String {
        format!("https://api.github.com/repos/{}/{}", self.owner, self.repo)
    }

    /// The commit request's address.
    pub fn commit_url(&self) -> String {
        format!("{}/commits/HEAD", self.api_base())
    }

    /// The tree listing request's address.
    pub fn listing_url(&self) -> String {
        format!("{}/git/trees/{}?recursive=1", self.api_base(), self.commit)
    }

    /// The raw file request's address for `path`.
    pub fn raw_url(&self, path: &str) -> String {
        format!(
            "https://raw.githubusercontent.com/{}/{}/{}/{}",
            self.owner,
            self.repo,
            self.commit,
            encode_path(path)
        )
    }

    /// Every listed node in Git's depth-first tree order, and the root tree id.
    fn nodes(&self) -> (Vec<Node>, String) {
        let mut folders: BTreeMap<String, Vec<Node>> = BTreeMap::new();
        folders.insert(String::new(), Vec::new());
        for path in self.entries.keys() {
            let mut folder = parent(path);
            while !folder.is_empty() {
                folders.entry(folder.to_string()).or_default();
                folder = parent(folder);
            }
        }
        for (path, entry) in &self.entries {
            let node = match entry {
                RecordedEntry::Blob { mode, data } => Node {
                    path: path.clone(),
                    mode,
                    kind: Kind::Blob,
                    oid: object_id("blob", data),
                    size: Some(u64::try_from(data.len()).unwrap_or(u64::MAX)),
                },
                RecordedEntry::Submodule { commit } => Node {
                    path: path.clone(),
                    mode: "160000",
                    kind: Kind::Commit,
                    oid: commit.clone(),
                    size: None,
                },
            };
            folders
                .entry(parent(path).to_string())
                .or_default()
                .push(node);
        }
        let mut names: Vec<String> = folders.keys().cloned().collect();
        names.sort_by_key(|name| {
            std::cmp::Reverse(name.matches('/').count() + usize::from(!name.is_empty()))
        });
        let mut ids: BTreeMap<String, String> = BTreeMap::new();
        for name in &names {
            let mut children: Vec<(Vec<u8>, &'static str, String, String)> = folders[name]
                .iter()
                .map(|node| {
                    (
                        base_name(&node.path).as_bytes().to_vec(),
                        node.mode,
                        base_name(&node.path).to_string(),
                        node.oid.clone(),
                    )
                })
                .collect();
            for (folder, id) in &ids {
                if !folder.is_empty() && parent(folder) == name {
                    let mut key = base_name(folder).as_bytes().to_vec();
                    key.push(b'/');
                    children.push((key, "40000", base_name(folder).to_string(), id.clone()));
                }
            }
            children.sort_by(|left, right| left.0.cmp(&right.0));
            let mut content = Vec::new();
            for (_, mode, child, oid) in &children {
                content.extend_from_slice(mode.as_bytes());
                content.push(b' ');
                content.extend_from_slice(child.as_bytes());
                content.push(0);
                content.extend_from_slice(&id_bytes(oid));
            }
            ids.insert(name.clone(), object_id("tree", &content));
        }
        let mut order = Vec::new();
        self.walk("", &mut folders, &ids, &mut order);
        let root = ids.get("").cloned().unwrap_or_default();
        (order, root)
    }

    fn walk(
        &self,
        folder: &str,
        folders: &mut BTreeMap<String, Vec<Node>>,
        ids: &BTreeMap<String, String>,
        order: &mut Vec<Node>,
    ) {
        let mut children = folders.remove(folder).unwrap_or_default();
        for (name, id) in ids {
            if !name.is_empty() && parent(name) == folder {
                children.push(Node {
                    path: name.clone(),
                    mode: "040000",
                    kind: Kind::Tree,
                    oid: id.clone(),
                    size: None,
                });
            }
        }
        children.sort_by_cached_key(|node| {
            let mut key = base_name(&node.path).as_bytes().to_vec();
            if node.kind == Kind::Tree {
                key.push(b'/');
            }
            key
        });
        for node in children {
            let path = node.path.clone();
            let tree = node.kind == Kind::Tree;
            order.push(node);
            if tree {
                self.walk(&path, folders, ids, order);
            }
        }
    }

    /// The root tree id Git gives these files.
    pub fn tree_id(&self) -> String {
        self.nodes().1
    }

    /// The listing GitHub answers for the commit: `{sha, url, tree, truncated}`.
    pub fn listing(&self) -> Value {
        let (nodes, root) = self.nodes();
        let base = self.api_base();
        let tree: Vec<Value> = nodes
            .iter()
            .map(|node| {
                let (kind, url) = match node.kind {
                    Kind::Blob => ("blob", format!("{base}/git/blobs/{}", node.oid)),
                    Kind::Tree => ("tree", format!("{base}/git/trees/{}", node.oid)),
                    Kind::Commit => ("commit", String::new()),
                };
                let mut item =
                    json!({"path": node.path, "mode": node.mode, "type": kind, "sha": node.oid});
                if let (Some(size), Some(map)) = (node.size, item.as_object_mut()) {
                    map.insert("size".to_string(), json!(size));
                }
                if let (false, Some(map)) = (url.is_empty(), item.as_object_mut()) {
                    map.insert("url".to_string(), json!(url));
                }
                item
            })
            .collect();
        json!({"sha": root, "url": format!("{base}/git/trees/{root}"), "tree": tree, "truncated": false})
    }

    /// Scripts the commit answer, the listing and every file's raw answer onto `transport`.
    pub fn script(&self, transport: &RecordingTransport) {
        transport.answer(
            self.commit_url(),
            Response::new(200, self.commit.as_bytes())
                .with_header("content-type", "application/vnd.github.sha; charset=utf-8"),
        );
        let listing = serde_json::to_vec(&self.listing()).unwrap_or_default();
        transport.answer(
            self.listing_url(),
            Response::new(200, listing)
                .with_header("content-type", "application/json; charset=utf-8"),
        );
        for (path, entry) in &self.entries {
            if let RecordedEntry::Blob { data, .. } = entry {
                transport.answer(
                    self.raw_url(path),
                    Response::new(200, data.clone())
                        .with_header("content-type", "text/plain; charset=utf-8"),
                );
            }
        }
    }
}
