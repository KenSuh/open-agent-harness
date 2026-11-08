# 출처 관측기 — GitHub 릴리스(관측한 태그 전체에서의 선택·메타데이터 대조)와 커밋(선택 경로), 공식 문서를 자격 증명 없이 남은 시간 안에서 읽어 불변 스냅숏과 관측 결과를 만든다.
"""One attempt per source, producing an `Observation` and, when something new was seen, an immutable snapshot.

GitHub sources use the kit's credential-free git (a bounded `ls-remote`, a shallow fetch, files read from the object
store) against the validated `https://github.com/<owner>/<repo>` locator with redirects off, so a moved repository
fails instead of answering from somewhere else. Release tracking selects through `release.Selector` (the observed
tag universe, by-tag and by-ID metadata read twice, advertisement read twice). Documents are one bounded GET each.
Every network step receives what is left of the caller's deadline.

A snapshot pins everything it is evidence of: release id, tag, tag object, commit and tree, the release observation
(metadata and complete notes), each selected file's blob id, size and sha256 (the bytes are stored), the license
evidence and the document bytes and visible text. Its `content_key` decides "unchanged": the release identity with
its metadata digest plus file evidence for releases, the file evidence for a path-scoped commit track (an unrelated
commit is quiet), the visible text for documents.
"""
from __future__ import annotations

import codecs
import dataclasses
import datetime as dt
import pathlib
import time
from typing import Callable, Mapping

from oah import scratch as oah_scratch
from oah.design.sources import gitref, release, web
from oah.design.sources import license as license_module
from oah.design.sources.model import Head, Observation, SourceSpec
from oah.design.sources.state import BLOB_LIMIT, State, StateProblem, digest_of, sha256_hex
from oah.toolkit.catalog import CatalogError
from oah.toolkit.fetch import FetchError, StageError, Tree, _checked, _git, check_portable_paths, fetch

API_BASE = "https://api.github.com"
FILE_LIMIT = 1 << 20
SNAPSHOT_LIMIT = 8 << 20
FILE_COUNT_LIMIT = 500
FETCH_TIMEOUT = 300.0
SOURCE_DEADLINE = 600.0
DOC_ACCEPT = "text/html, application/xhtml+xml, text/plain, text/markdown, application/json"


@dataclasses.dataclass(frozen=True)
class Transport:
    """Where requests go. Tests swap the git URL, the API base and the web policy; nothing in config can."""

    api_base: str = API_BASE
    git_url: Callable[[str], str] = staticmethod(lambda locator: locator)
    allow_loopback: bool = False
    resolve: Mapping[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)
    getter: Callable[..., web.WebResult] = staticmethod(web.get)


@dataclasses.dataclass(frozen=True)
class Settings:
    timeout: float = 30.0
    max_document_bytes: int = 1 << 20


def _failed(exc: Exception, selection: str | None = None) -> Observation:
    if isinstance(exc, web.WebFailure):
        try:
            retry = (dt.datetime.fromtimestamp(exc.retry_after, dt.timezone.utc)
                     if isinstance(exc.retry_after, (int, float)) else None)
        except (ValueError, OverflowError, OSError):
            retry = None
        return Observation("failed", code=exc.code, http=exc.http, retry_after=retry, selection=selection)
    if isinstance(exc, gitref.RefError) and exc.code == "ls-remote-timeout":
        return Observation("failed", code="timeout", selection=selection)
    return Observation("failed", code="git-failed", selection=selection)


class Observer:
    def __init__(self, state: State, transport: Transport, settings: Settings,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.state, self.transport, self.settings, self.clock = state, transport, settings, clock

    # ---- shared helpers ------------------------------------------------------------------------------------

    def verified_manifest(self, head: Head | None) -> dict | None:
        if head is None or head.verified is None:
            return None
        return self.state.get_record("snapshots", head.verified.snapshot)

    def _api_policy(self, max_bytes: int, timeout: float) -> web.WebPolicy:
        return web.WebPolicy(max_bytes=max_bytes, timeout=max(timeout, 0.001), allowed_types=("application/json",),
                             redirects="none", allow_loopback=self.transport.allow_loopback,
                             resolve=dict(self.transport.resolve))

    def _store(self, data: bytes) -> str:
        if len(data) > BLOB_LIMIT:
            raise web.WebFailure("oversize")
        return self.state.put_blob(data)

    def _left(self, deadline: float) -> float:
        left = deadline - self.clock()
        if left <= 0:
            raise web.WebFailure("timeout")
        return left

    def _manifest(self, spec: SourceSpec, *, observed: dict, files: list, license_evidence: dict | None,
                  document: dict | None, problems: list[str], content: dict, notices: list[str] = ()) -> dict:
        return {"version": 1, "kind": "oah-source-snapshot", "spec": spec.verification(),
                "spec_digest": spec.spec_digest, "observed": observed, "files": files, "license": license_evidence,
                "document": document, "verified": not problems, "problems": sorted(set(problems)),
                "notices": sorted(set(notices)), "content_key": digest_of(content)}

    def _finish(self, manifest: dict, verified: dict | None, seen: str, deadline: float,
                selection: str | None = None) -> Observation:
        self._left(deadline)
        if verified is not None and manifest["verified"] and verified.get("content_key") == manifest["content_key"]:
            return Observation("unchanged", seen=seen, selection=selection)
        digest = self.state.put_record("snapshots", manifest)
        self._left(deadline)
        return Observation("snapshot", snapshot=digest, verified=manifest["verified"],
                           problems=tuple(manifest["problems"]), seen=seen, selection=selection)

    def read_paths(self, tree: Tree, paths: tuple[str, ...]) -> tuple[list[dict], list[str]]:
        files, problems, total = [], set(), 0
        for path in paths:
            tree.check_deadline()
            entries = tree.entries(path)
            if not entries:
                problems.add("path-missing")
                continue
            try:
                check_portable_paths([name for _mode, _oid, name in entries], staging=False)
            except StageError:
                problems.add("unsafe-entry")
                continue
            for mode, oid, name in entries:
                tree.check_deadline()
                if mode in ("120000", "160000") or not oid:
                    problems.add("unsafe-entry")
                    continue
                size = tree.size(oid)
                total += size
                if size > FILE_LIMIT or total > SNAPSHOT_LIMIT or len(files) >= FILE_COUNT_LIMIT:
                    problems.add("oversize")
                    continue
                files.append({"path": name, "mode": mode, "blob": oid, "size": size,
                              "sha256": self._store(tree.read(oid))})
        files.sort(key=lambda item: item["path"])
        tree.check_deadline()
        return files, sorted(problems)

    # ---- entry point ------------------------------------------------------------------------------------------

    def observe(self, spec: SourceSpec, head: Head | None, *, deadline: float | None = None) -> Observation:
        if deadline is None:
            deadline = self.clock() + SOURCE_DEADLINE
        try:
            verified = self.verified_manifest(head)
        except StateProblem:
            verified = None  # a damaged verified manifest is reported by status; a fresh observation still runs
        try:
            if spec.track == "release":
                return self._release(spec, verified, deadline)
            if spec.track == "commit":
                return self._commit(spec, verified, deadline)
            return self._document(spec, verified, deadline)
        except (web.WebFailure, gitref.RefError, FetchError, CatalogError) as exc:
            return _failed(exc)

    # ---- GitHub releases ------------------------------------------------------------------------------------

    def _release(self, spec: SourceSpec, verified: dict | None, deadline: float) -> Observation:
        git_url = self.transport.git_url(spec.locator)
        selector = release.Selector(spec, api_base=self.transport.api_base, git_url=git_url,
                                    getter=self.transport.getter, policy=self._api_policy, deadline=deadline,
                                    timeout=self.settings.timeout, put_blob=self.state.put_blob, clock=self.clock)
        old = ((verified or {}).get("observed") or {}).get("release") or {}
        problems: list[str] = []
        try:
            selector.observe(old.get("tag"))
            chosen = selector.selected
            if chosen is None:
                selector.confirm()
                selector.outcome = "no-matching-release"
                extra = ("tag-disappeared",) if selector.reason == "tag-disappeared" else ()
                return self._blocked(selector, ("no-matching-release", *extra))
            if chosen.notes is not None and len(chosen.notes) > release.NOTES_LIMIT:
                selector.outcome, selector.reason = "unverified", "release-notes-too-large"
                return self._blocked(selector, ("release-notes-too-large",))
            commit = chosen.peeled or chosen.tag_object
            tag_object = chosen.tag_object if chosen.peeled else None
            if old.get("tag") == chosen.tag and (old.get("commit") != commit or old.get("tag_object") != tag_object):
                problems.append("tag-moved")
            reuse = (verified is not None and not problems and old.get("commit") == commit
                     and old.get("tag_object") == tag_object and verified.get("spec_digest") == spec.spec_digest)
            if reuse:
                files, license_evidence, tree_id = verified.get("files") or [], verified.get("license"), old.get("tree")
                fetched = commit
            else:
                with oah_scratch.directory("oah-source-") as scratch:
                    tree = fetch(git_url, f"refs/tags/{chosen.tag}", pathlib.Path(scratch) / "repo",
                                 timeout=min(FETCH_TIMEOUT, self._left(deadline)), follow_redirects=False,
                                 remaining=lambda: self._left(deadline))
                    fetched = tree.commit
                    tag_head = _checked(_git(tree.repo, "rev-parse", "--verify", "FETCH_HEAD", remaining=tree.remaining),
                                        "reading the fetched tag").strip()
                    tree_id = _checked(_git(tree.repo, "rev-parse", "--verify", "FETCH_HEAD^{tree}", remaining=tree.remaining),
                                       "reading the fetched tree").strip()
                    if fetched != commit or tag_head != chosen.tag_object:
                        problems.append("ref-mismatch")
                    files, path_problems = self.read_paths(tree, spec.paths)
                    problems += path_problems
                    license_evidence = license_module.discover(tree, spec.paths, spec.manifest_path, self._store,
                                                               remaining=tree.remaining)
            selector.content = {"commit": fetched, "tree": tree_id}
            selector.confirm()
        except release.Changed:
            selector.outcome = "changed-during-check"
            return self._blocked(selector, ("changed-during-check",))
        except release.Incomplete as exc:
            selector.outcome, selector.reason = "selection-incomplete", exc.reason
            return self._blocked(selector, (exc.problem,))
        except (web.WebFailure, gitref.RefError, FetchError, CatalogError) as exc:
            selector.outcome = "unavailable"
            return _failed(exc, self.state.put_record("selections", selector.record()))
        notes_blob = None if chosen.notes is None else self.state.put_blob(chosen.notes)
        observation = selector.release_observation_record(notes_blob)
        selector.release_observation = self.state.put_record("release-observations", observation)
        selector.outcome = "unverified" if problems else "latest-observed"
        selection = self.state.put_record("selections", selector.record())
        identity = {"id": chosen.fields["id"], "tag": chosen.tag, "tag_object": tag_object, "commit": commit,
                    "metadata_digest": observation["metadata_digest"]}
        info = {**identity, "tree": tree_id, "prerelease": chosen.fields["prerelease"],
                "published_at": chosen.fields["published_at"], "release_observation": selector.release_observation,
                "notes": observation["notes"], "selection": selection}
        notices = ["tag-disappeared"] if selector.reason == "tag-disappeared" else []
        manifest = self._manifest(spec, observed={"commit": commit, "ref": None, "release": info}, files=files,
                                  license_evidence=license_evidence, document=None, problems=problems,
                                  content={"spec": spec.spec_digest, "release": identity, "files": files,
                                           "license": license_evidence}, notices=notices)
        return self._finish(manifest, verified, commit, deadline, selection)

    def _blocked(self, selector: release.Selector, problems: tuple[str, ...]) -> Observation:
        return Observation("blocked", problems=problems,
                           selection=self.state.put_record("selections", selector.record()))

    # ---- GitHub commits -------------------------------------------------------------------------------------

    def _commit(self, spec: SourceSpec, verified: dict | None, deadline: float) -> Observation:
        git_url = self.transport.git_url(spec.locator)
        advertisement = gitref.ls_remote(git_url, (spec.ref,), timeout=min(gitref.TIMEOUT, self._left(deadline)))
        self._left(deadline)
        commit = advertisement.refs.get(spec.ref)
        if commit is None:
            return Observation("blocked", problems=("ref-missing",))
        if verified is not None and (verified.get("observed") or {}).get("commit") == commit:
            return Observation("unchanged", seen=commit)
        problems: list[str] = []
        with oah_scratch.directory("oah-source-") as scratch:
            tree = fetch(git_url, spec.ref, pathlib.Path(scratch) / "repo",
                         timeout=min(FETCH_TIMEOUT, self._left(deadline)), follow_redirects=False,
                         remaining=lambda: self._left(deadline))
            if tree.commit != commit:
                problems.append("ref-mismatch")
            tree_id = _checked(_git(tree.repo, "rev-parse", "--verify", "FETCH_HEAD^{tree}", remaining=tree.remaining),
                               "reading the fetched tree").strip()
            files, path_problems = self.read_paths(tree, spec.paths)
            problems += path_problems
            license_evidence = license_module.discover(tree, spec.paths, spec.manifest_path, self._store,
                                                       remaining=tree.remaining)
        content = {"spec": spec.spec_digest, "files": files, "license": license_evidence}
        if not spec.paths:
            content["commit"] = tree.commit  # without selected paths, every commit is new content
        manifest = self._manifest(spec, observed={"commit": tree.commit, "tree": tree_id, "ref": spec.ref,
                                                  "release": None},
                                  files=files, license_evidence=license_evidence, document=None, problems=problems,
                                  content=content)
        return self._finish(manifest, verified, tree.commit, deadline)

    # ---- documents ----------------------------------------------------------------------------------------

    def _document(self, spec: SourceSpec, verified: dict | None, deadline: float) -> Observation:
        policy = web.WebPolicy(max_bytes=self.settings.max_document_bytes,
                               timeout=min(self.settings.timeout, self._left(deadline)),
                               allow_loopback=self.transport.allow_loopback, resolve=dict(self.transport.resolve))
        result = self.transport.getter(spec.locator, policy=policy, headers={"Accept": DOC_ACCEPT})
        self._left(deadline)
        header = result.headers.get("content-type", "")
        kind = header.split(";", 1)[0].strip().lower()
        charset = "utf-8"
        for part in header.split(";")[1:]:
            name, _, value = part.strip().partition("=")
            if name.strip().lower() == "charset":
                try:
                    charset = codecs.lookup(value.strip().strip('"')).name
                except LookupError:
                    charset = "utf-8"
        try:
            if charset in ("undefined", "idna", "punycode", "unicode-escape", "raw-unicode-escape"):
                raise web.WebFailure("bad-response")
            try:
                text = result.body.decode(charset)
                text.encode("utf-8")
            except UnicodeError:
                charset = "utf-8"
                text = result.body.decode(charset, errors="replace")
        except (LookupError, UnicodeError):
            raise web.WebFailure("bad-response") from None
        if kind in ("text/html", "application/xhtml+xml"):
            text = web.visible_text(text)
        else:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        text_bytes = text.encode("utf-8")
        if len(text_bytes) > BLOB_LIMIT:
            raise web.WebFailure("oversize")
        text_sha = sha256_hex(text_bytes)
        content = {"spec": spec.spec_digest, "type": kind, "text_sha256": text_sha}
        self._left(deadline)
        if verified is not None and verified.get("content_key") == digest_of(content):
            return Observation("unchanged", seen=text_sha)
        document = {"url": spec.locator, "final_url": result.final_url, "status": result.status, "content_type": kind,
                    "charset": charset, "bytes": len(result.body), "sha256": self._store(result.body),
                    "text_sha256": self._store(text_bytes), "text_bytes": len(text_bytes)}
        manifest = self._manifest(spec, observed={"commit": None, "ref": None, "release": None}, files=[],
                                  license_evidence=None, document=document, problems=[], content=content)
        return self._finish(manifest, verified, text_sha, deadline)
