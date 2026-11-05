# 설계 출처 시험 — 관측한 태그 전체에서의 릴리스 선택과 메타데이터 대조, 변경 가능한 릴리스 노트, 등록 ABA, 공정한 예약 대기열, 설계 연결 CAS, 오프라인 diff 를 로컬 git 저장소와 가짜 GitHub API 로 본다.
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import pathlib
import subprocess
import unittest
import urllib.parse
from unittest import mock

from oah.design.sources import adapters, gitref, release, web
from oah.design.sources import state as state_module
from oah.design.sources.adapters import Transport
from oah.design.sources.check import Sources
from oah.design.sources.model import SourceError, build_spec, freshness
from tests.helpers import git_commit, git_env, make_git_repo, make_tmp_dir

LOCATOR = "https://github.com/acme/lib"
LICENSE = ("MIT License\n\nCopyright (c) 2026 Acme\n\n"
           "Permission is hereby granted, free of charge, to any person obtaining a copy\nof this software and "
           "associated documentation files (the \"Software\"), to deal\nin the Software without restriction, "
           "including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, "
           "and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, "
           "subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be "
           "included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED \"AS IS\", "
           "WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF "
           "MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS "
           "OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF "
           "CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR "
           "OTHER DEALINGS IN THE\nSOFTWARE.\n")
T0 = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=git_env()).stdout.strip()


class Clock:
    """A wall clock and a monotonic clock the test moves by hand."""

    def __init__(self, start: dt.datetime = T0) -> None:
        self.wall, self.mono = start, 1000.0

    def now(self) -> dt.datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += dt.timedelta(seconds=seconds)
        self.mono += seconds


class FakeGitHub:
    """The public release endpoints oah reads, answering from `releases` (tag -> release object)."""

    def __init__(self) -> None:
        self.releases: dict[str, dict] = {}
        self.requests: list[str] = []
        self.before: dict[int, callable] = {}
        self.fail: dict[str, web.WebFailure] = {}
        self.next_id = 100

    def add(self, tag: str, *, draft: bool = False, prerelease: bool = False,
            published: str | None = "2026-10-01T00:00:00Z", body: str | None = "notes", ident: int | None = None):
        self.next_id += 1
        self.releases[tag] = {"id": ident or self.next_id, "tag_name": tag, "draft": draft, "prerelease": prerelease,
                              "created_at": "2026-09-30T00:00:00Z", "published_at": published,
                              "html_url": f"https://github.com/acme/lib/releases/tag/{tag}", "body": body,
                              "assets": []}
        return self.releases[tag]

    def __call__(self, url: str, *, policy, headers=None) -> web.WebResult:
        self.requests.append(url)
        hook = self.before.pop(len(self.requests), None)
        if hook is not None:
            hook()
        for key, failure in self.fail.items():
            if key in url:
                raise failure
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        base = "/repos/acme/lib/releases"
        if path.startswith(base + "/tags/"):
            tag = urllib.parse.unquote(path[len(base + "/tags/"):])
            item = self.releases.get(tag)
            if item is None or item["draft"]:
                raise web.WebFailure("not-found", 404)
            body = item
        elif path == base and parts.query == "per_page=100":
            public = [item for item in self.releases.values() if not item["draft"]]
            body = sorted(public, key=lambda item: item["published_at"] or "", reverse=True)[:100]
        elif path.startswith(base + "/"):
            ident = int(path.rsplit("/", 1)[1])
            match = [item for item in self.releases.values() if item["id"] == ident and not item["draft"]]
            if not match:
                raise web.WebFailure("not-found", 404)
            body = match[0]
        else:
            raise AssertionError(f"unexpected request {url}")
        data = json.dumps(body).encode("utf-8")
        if len(data) > policy.max_bytes:
            raise web.WebFailure("oversize")
        return web.WebResult(200, url, {"content-type": "application/json"}, data)


class SourceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = make_git_repo(self)
        self.commits = {}
        self.clock = Clock()
        self.github = FakeGitHub()
        self.state = make_tmp_dir(self) / "state"
        self.fetches = []
        real_fetch = adapters.fetch

        def counting_fetch(*args, **kwargs):
            self.fetches.append(args[1])
            return real_fetch(*args, **kwargs)

        self.enterContext(mock.patch.object(adapters, "fetch", counting_fetch))

    def sources(self, **settings) -> Sources:
        transport = Transport(api_base="https://api.github.com", git_url=lambda locator: str(self.upstream),
                              getter=self.github)
        return Sources(self.state, settings=settings, transport=transport, now=self.clock.now,
                       clock=self.clock.monotonic)

    def commit(self, files: dict, message: str = "change") -> str:
        return git_commit(self.upstream, files, message)

    def tag(self, name: str, commit: str | None = None, *, annotated: bool = True) -> None:
        target = commit or git(self.upstream, "rev-parse", "HEAD")
        if annotated:
            git(self.upstream, "-c", "tag.gpgSign=false", "tag", "-a", "-m", name, name, target)
        else:
            git(self.upstream, "tag", name, target)

    def many_tags(self, names: list[str], commit: str) -> None:
        """Lightweight tags written straight into packed-refs (fast for thousands)."""
        lines = [f"{commit} refs/tags/{name}" for name in names]
        packed = self.upstream / ".git" / "packed-refs"
        existing = packed.read_text(encoding="utf-8").splitlines() if packed.exists() else ["# pack-refs with: peeled"]
        packed.write_text("\n".join(existing + lines) + "\n", encoding="utf-8")

    def register(self, sources: Sources, **kwargs):
        kwargs.setdefault("paths", ["src"])
        return sources.register(build_spec(url=LOCATOR, **kwargs))[1]

    def selection(self, sources: Sources, result) -> dict:
        return sources.record("selections", result.selection)


class ReleaseSelectionTest(SourceCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = self.commit({"src/lib.rs": "pub fn a() {}\n", "LICENSE": LICENSE})

    def test_simple_latest_observed_with_both_reads_repeated(self) -> None:
        self.tag("v1.0.0")
        newer = self.commit({"src/lib.rs": "pub fn b() {}\n"})
        self.tag("v1.1.0")
        self.github.add("v1.0.0")
        self.github.add("v1.1.0")
        sources = self.sources()
        spec = self.register(sources)
        result = sources.check_one(spec)
        self.assertEqual("changed", result.result)
        selection = self.selection(sources, result)
        self.assertEqual("latest-observed", selection["outcome"])
        self.assertEqual("observed-complete", selection["universe"]["completeness"])
        self.assertEqual("v1.1.0", selection["selected"]["tag"])
        self.assertEqual(newer, selection["selected"]["peeled_commit"])
        self.assertEqual(["R0", "R1"], [item["round"] for item in selection["ref_observations"]])
        self.assertEqual(4, selection["budget"]["requests_used"])  # by-tag + by-ID, twice
        manifest = sources.snapshot(result.snapshot)
        self.assertTrue(manifest["verified"])
        self.assertEqual(["src/lib.rs"], [item["path"] for item in manifest["files"]])
        self.assertEqual("v1.1.0", manifest["observed"]["release"]["tag"])
        record = sources.observation(result.observation)
        self.assertEqual("established", record["outcome"])
        self.assertEqual(result.snapshot, record["snapshot"])
        self.assertIsNone(record["previous"])

    def test_thousands_of_canaries_and_stable_releases_need_one_visit(self) -> None:
        stable = [f"v1.{minor}.0" for minor in range(1201)]
        canaries = [f"v2.0.0-canary.{number}" for number in range(1300)]
        self.many_tags(stable + canaries, self.base)
        for name in ("v1.1200.0", "v1.1199.0"):
            self.github.add(name)
        sources = self.sources()
        result = sources.check_one(self.register(sources))
        selection = self.selection(sources, result)
        self.assertEqual("latest-observed", selection["outcome"])
        self.assertEqual("v1.1200.0", selection["selected"]["tag"])
        self.assertEqual(1300, selection["universe"]["syntactic_exclusions_count"])
        self.assertEqual(1201, selection["universe"]["matching_count"])
        self.assertEqual(1, len(selection["candidates"]))

    def test_monorepo_prefix_with_hundreds_of_versions(self) -> None:
        mine = [f"pkg-a@1.{minor}.0" for minor in range(251)]
        others = [f"pkg-b@9.{minor}.0" for minor in range(300)]
        self.many_tags(mine + others, self.base)
        self.github.add("pkg-a@1.250.0")
        self.github.add("pkg-b@9.299.0")
        sources = self.sources()
        result = sources.check_one(self.register(sources, tag_prefix="pkg-a@"))
        selection = self.selection(sources, result)
        self.assertEqual("pkg-a@1.250.0", selection["selected"]["tag"])
        self.assertEqual(251, selection["universe"]["matching_count"])

    def test_prereleases_before_the_latest_stable_are_skipped_or_excluded(self) -> None:
        self.many_tags([f"v3.0.0-rc.{n}" for n in range(25)], self.base)
        self.many_tags([f"v2.{minor}.0" for minor in range(1, 11)], self.base)
        self.tag("v2.0.0")
        for minor in range(1, 11):
            self.github.add(f"v2.{minor}.0", prerelease=True)
        self.github.add("v2.0.0")
        sources = self.sources()
        result = sources.check_one(self.register(sources))
        selection = self.selection(sources, result)
        self.assertEqual("v2.0.0", selection["selected"]["tag"])
        self.assertEqual(11, len(selection["candidates"]))
        self.assertEqual({"channel"}, {item["exclusion_reason"] for item in selection["candidates"][:10]})

    def test_more_than_twenty_higher_releases_without_public_metadata_is_incomplete(self) -> None:
        self.tag("v1.0.0")
        self.github.add("v1.0.0")
        sources = self.sources()
        spec = self.register(sources)
        first = sources.check_one(spec)
        self.assertEqual("changed", first.result)
        self.many_tags([f"v1.{minor}.0" for minor in range(1, 22)], self.base)
        self.clock.advance(3600)
        result = sources.check_one(spec)
        self.assertEqual("blocked", result.result)
        self.assertEqual(("selection-incomplete",), result.problems)
        selection = self.selection(sources, result)
        self.assertEqual("candidate-limit", selection["reason"])
        self.assertEqual(20, len(selection["candidates"]))
        head = sources.head(spec)
        self.assertEqual(first.snapshot, head.verified.snapshot)
        self.assertEqual("2026-11-01T12:00:00+00:00", head.confirmed_at)
        self.assertEqual("unverified", freshness(head, self.clock.now(), 24))

    def test_new_higher_ref_between_advertisements_refuses(self) -> None:
        self.tag("v1.0.0")
        self.github.add("v1.0.0")
        self.github.before[2] = lambda: self.tag("v9.0.0")
        sources = self.sources()
        result = sources.check_one(self.register(sources))
        self.assertEqual("blocked", result.result)
        self.assertEqual(("changed-during-check",), result.problems)
        self.assertEqual("changed-during-check", self.selection(sources, result)["outcome"])

    def test_higher_release_published_on_an_advertised_tag_refuses(self) -> None:
        self.tag("v1.0.0")
        self.tag("v2.0.0")
        self.github.add("v1.0.0")
        self.github.before[4] = lambda: self.github.add("v2.0.0")  # after M0 visits and the by-ID read
        sources = self.sources()
        result = sources.check_one(self.register(sources))
        self.assertEqual(("changed-during-check",), result.problems)

    def test_explicit_semver_policy_excludes_non_version_tags(self) -> None:
        self.tag("v1.0.0")
        self.tag("nightly")
        self.github.add("v1.0.0")
        self.github.add("nightly", published="2026-10-30T00:00:00Z")
        sources = self.sources()
        result = sources.check_one(self.register(sources, release_order="semver"))
        selection = self.selection(sources, result)
        self.assertEqual("v1.0.0", selection["selected"]["tag"])
        self.assertEqual("semver", selection["policy"]["effective_order"])
        self.assertEqual(1, selection["universe"]["syntactic_exclusions_count"])

    def test_auto_order_with_a_non_version_tag_uses_published_order(self) -> None:
        self.tag("v1.0.0")
        self.tag("nightly")
        self.github.add("v1.0.0", published="2026-10-01T00:00:00Z")
        self.github.add("nightly", published="2026-10-30T00:00:00Z")
        sources = self.sources()
        result = sources.check_one(self.register(sources))
        selection = self.selection(sources, result)
        self.assertEqual("published", selection["policy"]["effective_order"])
        self.assertEqual("nightly", selection["selected"]["tag"])
        self.assertEqual(6, selection["budget"]["requests_used"])

    def test_published_order_over_twenty_tags_uses_the_short_list(self) -> None:
        names = [f"build-{n:03d}" for n in range(30)]
        self.many_tags(names, self.base)
        for n, name in enumerate(names):
            self.github.add(name, published=f"2026-10-{n % 28 + 1:02d}T00:00:00Z")
        sources = self.sources()
        result = sources.check_one(self.register(sources))
        selection = self.selection(sources, result)
        self.assertEqual("short-list", selection["policy"]["universe_kind"])
        self.assertEqual(99, selection["budget"]["candidate_limit"])
        self.assertEqual(4, selection["budget"]["requests_used"])
        self.assertEqual("build-027", selection["selected"]["tag"])

    def test_full_first_page_is_not_complete(self) -> None:
        names = [f"build-{n:03d}" for n in range(120)]
        self.many_tags(names, self.base)
        for name in names:
            self.github.add(name)
        sources = self.sources()
        result = sources.check_one(self.register(sources))
        self.assertEqual(("selection-incomplete",), result.problems)
        self.assertEqual("short-list-full-page", self.selection(sources, result)["reason"])

    def test_rate_limit_keeps_prior_proof_and_records_retry(self) -> None:
        self.tag("v1.0.0")
        self.github.add("v1.0.0")
        sources = self.sources()
        spec = self.register(sources)
        first = sources.check_one(spec)
        self.github.fail["/releases/tags/"] = web.WebFailure("rate-limited", 403, (T0 + dt.timedelta(hours=2))
                                                             .timestamp())
        self.clock.advance(60)
        result = sources.check_one(spec)
        self.assertEqual("failed", result.result)
        self.assertEqual(("rate-limited",), result.problems)
        head = sources.head(spec)
        self.assertEqual(first.snapshot, head.verified.snapshot)
        self.assertEqual("2026-11-01T14:00:00+00:00", head.retry_after)
        self.assertEqual("skipped-rate-limited", sources.check_one(spec).result)

    def test_tag_moved_is_unverified_and_confirms_nothing(self) -> None:
        self.tag("v1.0.0")
        self.github.add("v1.0.0")
        sources = self.sources()
        spec = self.register(sources)
        first = sources.check_one(spec)
        other = self.commit({"src/lib.rs": "pub fn moved() {}\n"})
        git(self.upstream, "tag", "-d", "v1.0.0")
        self.tag("v1.0.0", other)
        self.clock.advance(60)
        result = sources.check_one(spec)
        self.assertEqual("unverified", result.result)
        self.assertIn("tag-moved", result.problems)
        head = sources.head(spec)
        self.assertEqual(first.snapshot, head.verified.snapshot)
        self.assertEqual("2026-11-01T12:00:00+00:00", head.confirmed_at)

    def test_lightweight_tag_is_verified_by_the_fetched_commit(self) -> None:
        self.tag("v1.0.0", annotated=False)
        self.github.add("v1.0.0")
        sources = self.sources()
        result = sources.check_one(self.register(sources))
        manifest = sources.snapshot(result.snapshot)
        self.assertTrue(manifest["verified"])
        self.assertIsNone(manifest["observed"]["release"]["tag_object"])
        self.assertEqual(self.base, manifest["observed"]["release"]["commit"])

    def test_no_matching_release_only_after_a_complete_walk(self) -> None:
        self.tag("v1.0.0")
        sources = self.sources()
        result = sources.check_one(self.register(sources))
        self.assertEqual(("no-matching-release",), result.problems)
        self.assertEqual("no-matching-release", self.selection(sources, result)["outcome"])

    def test_semver_parser_is_strict(self) -> None:
        for good in ("1.2.3", "0.0.0", "1.2.3-rc.1", "1.2.3-alpha-1.x", "1.2.3+build.5"):
            self.assertIsNotNone(release.parse_semver(good), good)
        for bad in ("1.2", "01.2.3", "1.2.3-01", "1.2.3-", "1.2.3+", "1.2.3-a..b", "v1.2.3", "1.2.3.4",
                    "9" * 19 + ".0.0"):
            self.assertIsNone(release.parse_semver(bad), bad)
        ordered = release.semver_order(["v1.0.0-rc.1", "v1.0.0", "v1.0.0-alpha", "v1.0.0-rc.10", "v1.0.0-rc.2",
                                        "v0.9.9", "v1.0.0+b"], "")
        self.assertEqual(["v1.0.0", "v1.0.0+b", "v1.0.0-rc.10", "v1.0.0-rc.2", "v1.0.0-rc.1", "v1.0.0-alpha",
                          "v0.9.9"], ordered)


class ReleaseMetadataTest(SourceCase):
    def setUp(self) -> None:
        super().setUp()
        self.commit({"src/lib.rs": "pub fn a() {}\n", "LICENSE": LICENSE})
        self.tag("v1.0.0")
        self.github.add("v1.0.0", body="first notes")

    def test_identical_observation_is_quiet_and_reuses_content(self) -> None:
        sources = self.sources()
        spec = self.register(sources)
        first = sources.check_one(spec)
        self.assertEqual(1, len(self.fetches))
        self.clock.advance(3600)
        again = sources.check_one(spec)
        self.assertEqual("unchanged", again.result)
        self.assertFalse(again.notable)
        self.assertEqual(1, len(self.fetches))
        head = sources.head(spec)
        self.assertEqual("2026-11-01T13:00:00+00:00", head.confirmed_at)
        chain = sources.observations(spec)
        self.assertEqual(["confirmed", "established"], [item["outcome"] for item in chain])
        self.assertEqual(first.snapshot, chain[0]["snapshot"])
        self.assertEqual(chain[1]["digest"], chain[0]["previous"])

    def test_changed_notes_at_the_same_tag_and_commit_are_new_evidence(self) -> None:
        sources = self.sources()
        spec = self.register(sources)
        first = sources.check_one(spec)
        self.github.releases["v1.0.0"]["body"] = "first notes\n\nSecurity advisory: upgrade now."
        self.clock.advance(3600)
        result = sources.check_one(spec)
        self.assertEqual("changed", result.result)
        self.assertEqual(1, len(self.fetches))  # content reused after digest checks
        before, after = sources.snapshot(first.snapshot), sources.snapshot(result.snapshot)
        self.assertEqual(before["files"], after["files"])
        self.assertNotEqual(before["observed"]["release"]["metadata_digest"],
                            after["observed"]["release"]["metadata_digest"])
        self.assertEqual("release-metadata-changed", sources._kind("release", before, after))
        notes = sources.record("release-observations", after["observed"]["release"]["release_observation"])["notes"]
        self.assertIn(b"Security advisory", sources.blob(notes["sha256"]))

    def test_oversize_notes_keep_prior_proof(self) -> None:
        sources = self.sources()
        spec = self.register(sources)
        first = sources.check_one(spec)
        self.github.releases["v1.0.0"]["body"] = "x" * (64 * 1024 + 1)
        self.clock.advance(3600)
        result = sources.check_one(spec)
        self.assertEqual("blocked", result.result)
        self.assertEqual(("release-notes-too-large",), result.problems)
        head = sources.head(spec)
        self.assertEqual(first.snapshot, head.verified.snapshot)
        self.assertEqual("2026-11-01T12:00:00+00:00", head.confirmed_at)

    def test_failed_by_id_read_keeps_prior_proof(self) -> None:
        sources = self.sources()
        spec = self.register(sources)
        first = sources.check_one(spec)
        ident = self.github.releases["v1.0.0"]["id"]
        self.github.fail[f"/releases/{ident}"] = web.WebFailure("http-error", 502)
        self.clock.advance(3600)
        result = sources.check_one(spec)
        self.assertEqual("failed", result.result)
        self.assertEqual(first.snapshot, sources.head(spec).verified.snapshot)
        self.assertEqual("unavailable", self.selection(sources, result)["outcome"])


class AdvertisementTest(unittest.TestCase):
    def test_parser_refuses_malformed_or_truncated_output(self) -> None:
        oid = "a" * 40
        good = f"{oid}\trefs/tags/v1\n{'b' * 40}\trefs/tags/v1^{{}}\n".encode()
        self.assertEqual({"refs/tags/v1": oid, "refs/tags/v1^{}": "b" * 40}, gitref.parse(good, tags_only=True))
        for bad in (good[:-1], f"{oid} refs/tags/v1\n".encode(), f"{oid}\trefs/tags/v1\n{oid}\trefs/tags/v1\n"
                    .encode(), f"{'b' * 40}\trefs/tags/v2^{{}}\n".encode(), f"{oid}\trefs/heads/main\n".encode(),
                    f"{'A' * 40}\trefs/tags/v1\n".encode(), b"\xff\trefs/tags/v1\n"):
            with self.assertRaises(gitref.RefError, msg=bad):
                gitref.parse(bad, tags_only=True)
        with self.assertRaises(gitref.RefError) as caught:
            gitref.parse(good, tags_only=True, row_limit=1)
        self.assertEqual("ls-remote-overflow", caught.exception.code)

    def test_collector_caps_bytes(self) -> None:
        repo = make_git_repo(self)
        head = git_commit(repo, {"a": "1\n"})
        lines = [f"{head} refs/tags/t{n:05d}" for n in range(2000)]
        (repo / ".git" / "packed-refs").write_text("# pack-refs with: peeled\n" + "\n".join(lines) + "\n",
                                                   encoding="utf-8")
        with self.assertRaises(gitref.RefError) as caught:
            gitref.ls_remote(str(repo), tags=True, byte_limit=4096)
        self.assertEqual("ls-remote-overflow", caught.exception.code)
        complete = gitref.ls_remote(str(repo), tags=True)
        self.assertEqual(2000, complete.rows)



class Docs:
    """Official document pages; a page can take time (moving the clock) or fail."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.pages: dict[str, str] = {}
        self.slow: dict[str, float] = {}
        self.fail: dict[str, web.WebFailure] = {}
        self.requests: list[str] = []

    def __call__(self, url: str, *, policy, headers=None) -> web.WebResult:
        self.requests.append(url)
        if url in self.slow:
            self.clock.advance(self.slow[url])
            raise web.WebFailure("timeout")
        if url in self.fail:
            raise self.fail[url]
        body = self.pages.get(url, f"page {url}").encode("utf-8")
        return web.WebResult(200, url, {"content-type": "text/plain; charset=utf-8"}, body)


class FairQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.docs = Docs(self.clock)
        self.state = make_tmp_dir(self) / "state"

    def sources(self, **settings) -> Sources:
        transport = Transport(getter=self.docs)
        return Sources(self.state, settings=settings, transport=transport, now=self.clock.now,
                       clock=self.clock.monotonic)

    def register_docs(self, sources: Sources, count: int) -> list[str]:
        ids = []
        for number in range(count):
            spec = build_spec(url=f"https://docs.example.org/p{number:02d}", source_id=f"s{number:02d}")
            ids.append(sources.register(spec)[1].id)
        return ids

    def attempted(self, sources: Sources) -> set[str]:
        registry, _raw = sources.registry()
        return {key for key, spec in registry.sources.items()
                if sources.head(spec) is not None and sources.head(spec).attempted_at is not None}

    def test_early_timeouts_never_starve_later_sources(self) -> None:
        sources = self.sources()
        ids = self.register_docs(sources, 30)
        for number in range(10):
            self.docs.slow[f"https://docs.example.org/p{number:02d}"] = 400.0
        outcomes = []
        for _run in range(4):
            report = sources.scheduled()
            outcomes.append(report.outcome)
            self.clock.advance(3600)
        self.assertEqual("partial(over-budget)", outcomes[0])
        self.assertIn("complete", outcomes)
        self.assertEqual(set(ids), self.attempted(sources))
        state = sources.schedule_state()
        self.assertIsNotNone(state["scan"]["last_complete_scan_at"])
        self.assertIsNone(state["scan"]["reservation"])

    def test_partial_run_continues_where_it_stopped(self) -> None:
        sources = self.sources()
        self.register_docs(sources, 8)
        for number in range(8):
            self.docs.slow[f"https://docs.example.org/p{number:02d}"] = 700.0
        first = sources.scheduled()
        self.assertEqual(["s00", "s01", "s02"], [item.source for item in first.results])
        self.clock.advance(60)
        second = sources.scheduled()
        self.assertEqual(["s03", "s04", "s05"], [item.source for item in second.results])
        self.assertEqual(("s06", "s07"), second.outstanding)

    def test_new_cycle_rotates_after_the_last_attempted_source(self) -> None:
        sources = self.sources(check_interval_hours=1, fresh_hours=1)
        self.register_docs(sources, 4)
        self.docs.slow["https://docs.example.org/p01"] = 1800.0
        first = sources.scheduled()
        self.assertEqual(["s00", "s01"], [item.source for item in first.results])
        self.docs.slow.clear()
        self.clock.advance(60)
        rest = sources.scheduled()
        self.assertEqual("complete", rest.outcome)
        self.clock.advance(7200)
        third = sources.scheduled()
        self.assertEqual("s00", third.results[0].source)
        self.assertEqual("complete", third.outcome)

    def test_rate_limited_entries_wait_while_others_run(self) -> None:
        sources = self.sources()
        self.register_docs(sources, 3)
        retry = (self.clock.now() + dt.timedelta(hours=2)).timestamp()
        self.docs.fail["https://docs.example.org/p00"] = web.WebFailure("rate-limited", 429, retry)
        report = sources.scheduled()
        self.assertEqual("partial(rate-limited)", report.outcome)
        self.assertEqual(("s00",), report.outstanding)
        self.assertEqual("2026-11-01T14:00:00+00:00", report.retry_after)
        self.assertEqual(["s00", "s01", "s02"], [item.source for item in report.results])
        state = sources.schedule_state()
        self.assertIsNone(state["scan"]["last_complete_scan_at"])
        self.clock.advance(60)
        waiting = sources.scheduled()
        self.assertEqual((), waiting.results)
        self.assertEqual("partial(rate-limited)", waiting.outcome)
        del self.docs.fail["https://docs.example.org/p00"]
        self.clock.advance(7200)
        self.assertEqual("complete", sources.scheduled().outcome)

    def test_interrupted_run_is_requeued_after_its_reservation_expires(self) -> None:
        sources = self.sources()
        self.register_docs(sources, 2)

        def crash(step: str) -> None:
            if step == "after-store":
                raise RuntimeError("killed")

        with self.assertRaises(RuntimeError):
            sources.scheduled(fault=crash)
        self.clock.advance(60)
        self.assertEqual("busy", sources.scheduled().outcome)
        self.clock.advance(1800 + 200)
        report = sources.scheduled()
        self.assertEqual("complete", report.outcome)
        cycle = sources.schedule_state()["scan"]["cycle"]
        self.assertEqual("interrupted", cycle["done"][0]["outcome"])
        self.assertEqual("s00", cycle["done"][0]["id"])
        self.assertEqual(3, len(cycle["done"]))
        self.assertEqual({"s00", "s01"}, {item.source for item in report.results})

    def test_removed_and_re_registered_source_keeps_only_its_new_token(self) -> None:
        sources = self.sources()
        self.register_docs(sources, 3)
        for number in range(3):
            self.docs.slow[f"https://docs.example.org/p{number:02d}"] = 1800.0
        sources.scheduled()  # attempts s00, then the budget is gone
        sources.remove("s01")
        sources.register(build_spec(url="https://docs.example.org/p01", source_id="s01"))
        state = sources.schedule_state()
        sources_registry = sources.registry()[0].sources
        self.docs.slow.clear()
        self.clock.advance(60)
        report = sources.scheduled()
        self.assertEqual(["s02", "s01"], [item.source for item in report.results])
        done = sources.schedule_state()["scan"]["cycle"]["done"]
        self.assertEqual(sources_registry["s01"].registration, done[-1]["registration"])
        self.assertEqual(3, len(state["scan"]["cycle"]["pending"]) + len(state["scan"]["cycle"]["done"]))

    def test_fresh_sources_are_reused_with_their_original_times(self) -> None:
        sources = self.sources(check_interval_hours=1, fresh_hours=24)
        self.register_docs(sources, 2)
        self.assertEqual("complete", sources.scheduled().outcome)
        self.assertEqual("not-due", sources.scheduled().outcome)
        confirmed = sources.head(sources.spec("s00")).confirmed_at
        self.clock.advance(7200)
        report = sources.scheduled()
        self.assertEqual("complete", report.outcome)
        self.assertEqual({"skipped-fresh"}, {item.result for item in report.results})
        self.assertEqual(confirmed, sources.head(sources.spec("s00")).confirmed_at)
        self.assertEqual({"reused": 2}, sources.schedule_state()["scan"]["counts"])

    def test_disabled_runs_nothing_and_records_the_command(self) -> None:
        sources = self.sources(auto_check=False)
        self.register_docs(sources, 1)
        self.assertEqual("disabled", sources.scheduled().outcome)
        self.assertEqual([], self.docs.requests)
        state = sources.schedule_state()
        self.assertEqual("disabled", state["source_command_run"]["outcome"])
        self.assertEqual("--scheduled", state["cooperative_scheduled_run"]["flag"])


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.docs = Docs(self.clock)
        self.state = make_tmp_dir(self) / "state"
        self.sources = Sources(self.state, transport=Transport(getter=self.docs), now=self.clock.now,
                               clock=self.clock.monotonic)
        self.spec = self.sources.register(build_spec(url="https://docs.example.org/guide", source_id="guide"))[1]

    def test_re_registration_during_a_check_supersedes_it(self) -> None:
        def swap(step: str) -> None:
            if step == "after-store":
                self.sources.remove("guide")
                self.sources.register(build_spec(url="https://docs.example.org/guide", source_id="guide"))

        result = self.sources.check_one(self.spec, fault=swap)
        self.assertEqual("superseded", result.result)
        new = self.sources.spec("guide")
        self.assertNotEqual(self.spec.registration, new.registration)
        self.assertIsNone(self.sources.head(new))

    def test_a_concurrent_commit_supersedes_the_slower_check(self) -> None:
        def race(step: str) -> None:
            if step == "after-store":
                self.sources.check_one(self.spec)

        result = self.sources.check_one(self.spec, fault=race)
        self.assertEqual("superseded", result.result)
        self.assertEqual(1, self.sources.head(self.spec).generation)

    def test_a_crash_after_the_head_keeps_the_commit_and_its_record(self) -> None:
        def crash(step: str) -> None:
            if step == "after-head":
                raise RuntimeError("killed")

        with self.assertRaises(RuntimeError):
            self.sources.check_one(self.spec, fault=crash)
        head = self.sources.head(self.spec)
        self.assertEqual("changed", head.outcome)
        self.assertEqual("established", self.sources.observation(head.observation)["outcome"])

    def test_a_failed_attempt_never_moves_confirmation(self) -> None:
        first = self.sources.check_one(self.spec)
        self.clock.advance(3600)
        self.docs.fail["https://docs.example.org/guide"] = web.WebFailure("network")
        self.assertEqual("failed", self.sources.check_one(self.spec).result)
        head = self.sources.head(self.spec)
        self.assertEqual("2026-11-01T12:00:00+00:00", head.confirmed_at)
        self.assertEqual("2026-11-01T13:00:00+00:00", head.attempted_at)
        self.assertEqual(first.snapshot, head.verified.snapshot)
        self.assertEqual("unavailable", freshness(head, self.clock.now(), 24))
        self.assertEqual(1, len(self.sources.observations(self.spec)))



class OfflineDiffTest(SourceCase):
    """`Sources.diff` compares two immutable snapshots with every network and save path set to fail."""

    def setUp(self) -> None:
        super().setUp()
        self.commit({"src/lib.rs": "pub fn a() {}\n", "src/data.bin": b"\x00\x01\x02", "LICENSE": LICENSE})
        self.tag("v1.0.0")
        self.github.add("v1.0.0", body="first notes")
        self.store = self.sources()
        self.spec = self.register(self.store)
        self.first = self.store.check_one(self.spec)

    def second(self, files: dict, *, body: str = "second notes") -> str:
        self.clock.advance(3600)
        self.commit(files)
        self.tag("v1.1.0")
        self.github.add("v1.1.0", body=body)
        result = self.store.check_one(self.spec)
        self.assertEqual("changed", result.result)
        return result.snapshot

    def offline(self, sources: Sources | None = None) -> contextlib.ExitStack:
        """Network, fetch, check and every state write raise while the diff runs."""
        stack = contextlib.ExitStack()
        boom = AssertionError("diff touched the network or saved")
        for target, name in ((adapters, "fetch"), (gitref, "ls_remote"), (FakeGitHub, "__call__"),
                             (state_module.State, "put_blob"), (state_module.State, "put_record"),
                             (state_module.State, "write_json"), (Sources, "check_one")):
            stack.enter_context(mock.patch.object(target, name, side_effect=boom))
        return stack

    def test_identical_snapshots_are_a_complete_empty_diff(self) -> None:
        head = self.store.head(self.spec)
        with self.offline():
            report = self.store.diff(self.spec.id, self.first.snapshot, self.first.snapshot)
        self.assertEqual("complete", report["completeness"])
        self.assertEqual({"added": 0, "removed": 0, "changed": 0, "unchanged": 2}, report["paths"]["counts"])
        self.assertEqual([], report["text_diffs"])
        self.assertEqual([], report["release"]["changed"])
        self.assertFalse(report["license"]["source_bytes_changed"])
        self.assertEqual("same-spec", report["scope"]["compared"])
        self.assertEqual(head, self.store.head(self.spec))

    def test_changed_snapshot_reports_paths_release_license_and_original_times(self) -> None:
        second = self.second({"src/lib.rs": "pub fn a() {}\npub fn b() {}\n", "src/new.rs": "x\n",
                              "src/data.bin": b"\x00\x03", "LICENSE": LICENSE.replace("2026 Acme", "2026 Acme Inc")})
        requests, fetches = len(self.github.requests), len(self.fetches)
        confirmed = self.store.head(self.spec).confirmed_at
        with self.offline():
            report = self.store.diff(self.spec.id, self.first.snapshot, second)
        self.assertEqual((requests, fetches), (len(self.github.requests), len(self.fetches)))
        self.assertEqual(confirmed, self.store.head(self.spec).confirmed_at)
        self.assertEqual("complete", report["completeness"], report["problems"])
        self.assertEqual({"added": 1, "removed": 0, "changed": 2, "unchanged": 0}, report["paths"]["counts"])
        diffs = {item["item"]: item for item in report["text_diffs"]}
        self.assertIn("+pub fn b() {}", diffs["path:src/lib.rs"]["lines"])
        self.assertEqual("external-data", diffs["path:src/lib.rs"]["label"])
        self.assertEqual("binary-no-text-diff", diffs["path:src/data.bin"]["status"])
        self.assertIn("license:LICENSE", diffs)
        self.assertIn("release-notes", diffs)
        self.assertTrue(report["license"]["source_bytes_changed"])
        self.assertTrue({"id", "tag", "tag_object", "commit", "metadata_digest", "notes"}
                        <= set(report["release"]["changed"]))
        self.assertEqual("2026-11-01T12:00:00+00:00", report["observation_times"]["from"]["verified_at"])
        self.assertEqual("2026-11-01T13:00:00+00:00", report["observation_times"]["to"]["verified_at"])

    def test_terminal_controls_and_bidi_are_escaped(self) -> None:
        second = self.second({"src/lib.rs": "pub fn a() {}\n\x1b[31mred\u202eevil\n"})
        report = self.store.diff(self.spec.id, self.first.snapshot, second)
        lines = next(item for item in report["text_diffs"] if item["item"] == "path:src/lib.rs")["lines"]
        self.assertIn("+\\u001b[31mred\\u202eevil", lines)
        self.assertFalse(any("\x1b" in line or "\u202e" in line for line in lines))

    def test_policy_only_eligibility_change_is_not_a_byte_change(self) -> None:
        second = self.second({"src/lib.rs": "pub fn a() {}\n// same license\n"})
        mit = self.store.diff(self.spec.id, self.first.snapshot, second)["license"]
        strict = self.sources(license_allow=["Apache-2.0"]).diff(self.spec.id, self.first.snapshot, second)["license"]
        self.assertFalse(mit["source_bytes_changed"])
        self.assertFalse(strict["source_bytes_changed"])
        self.assertEqual("eligible", mit["eligibility"]["to"])
        self.assertNotEqual(mit["eligibility"]["to"], strict["eligibility"]["to"])
        self.assertNotEqual(mit["eligibility"]["policy"], strict["eligibility"]["policy"])
        self.assertFalse(strict["eligibility"]["changed"])

    def test_missing_bytes_make_the_item_unavailable_and_the_diff_partial(self) -> None:
        second = self.second({"src/lib.rs": "pub fn a() {}\npub fn c() {}\n"})
        new = next(item for item in self.store.snapshot(second)["files"] if item["path"] == "src/lib.rs")
        self.store.state.path(self.store.state._addressed("blobs", new["sha256"])).unlink()
        report = self.store.diff(self.spec.id, self.first.snapshot, second)
        self.assertEqual("partial", report["completeness"])
        self.assertIn("bytes-unavailable", report["problems"])
        item = next(item for item in report["text_diffs"] if item["item"] == "path:src/lib.rs")
        self.assertEqual("unavailable", item["status"])

    def test_text_diff_limits_are_counted_and_named(self) -> None:
        files = {f"src/f{index:02}.rs": f"// file {index}\n" for index in range(60)}
        files["src/f00.rs"] = "".join(f"line {index}\n" for index in range(300))
        self.clock.advance(60)
        self.commit(files)
        self.tag("v1.0.1")
        self.github.add("v1.0.1", body="first notes")
        before = self.store.check_one(self.spec).snapshot
        changed = {name: text + "// changed\n" for name, text in files.items()}
        changed["src/f00.rs"] = "".join(f"line {index} changed\n" for index in range(300))
        second = self.second(changed, body="first notes")
        report = self.store.diff(self.spec.id, before, second)
        self.assertEqual("partial", report["completeness"])
        self.assertIn("text-diff-limited", report["problems"])
        self.assertEqual(60, report["paths"]["counts"]["changed"])
        self.assertEqual(60, len(report["paths"]["entries"]))
        statuses = [item["status"] for item in report["text_diffs"]]
        self.assertEqual(10, statuses.count("omitted"))
        self.assertEqual(10, len(report["omitted_diffs"]))
        first = next(item for item in report["text_diffs"] if item["item"] == "path:src/f00.rs")
        self.assertEqual("truncated", first["status"])
        self.assertEqual(200, len(first["lines"]))
        self.assertGreater(first["total_lines"], 200)

    def test_snapshots_of_another_source_or_a_reused_id_are_refused(self) -> None:
        other = self.store.register(build_spec(url="https://github.com/acme/other", source_id="other",
                                               track="commit"))[1]
        result = self.store.check_one(other)
        self.assertEqual("changed", result.result)
        with self.assertRaises(SourceError) as caught:
            self.store.diff(self.spec.id, self.first.snapshot, result.snapshot)
        self.assertEqual("OAH_SOURCES_SOURCE_MISMATCH", caught.exception.code)
        self.store.remove(self.spec.id)
        reused = self.store.register(build_spec(url="https://github.com/acme/third", source_id=self.spec.id,
                                                track="commit"))
        self.assertEqual("registered", reused[0])
        with self.assertRaises(SourceError) as caught:
            self.store.diff(self.spec.id, self.first.snapshot, self.first.snapshot)
        self.assertEqual("OAH_SOURCES_SOURCE_MISMATCH", caught.exception.code)
        missing = "sha256:" + "0" * 64
        self.assertEqual("unavailable", self.store.diff("other", missing, result.snapshot)["completeness"])

    def test_portable_envelopes_stand_in_for_a_lost_store(self) -> None:
        second = self.second({"src/lib.rs": "pub fn a() {}\npub fn b() {}\n"})
        envelopes = [self.store.portable(self.spec, digest) for digest in (self.first.snapshot, second)]
        local = self.store.diff(self.spec.id, self.first.snapshot, second)
        empty = Sources(make_tmp_dir(self) / "empty", transport=Transport(getter=self.github), now=self.clock.now,
                        clock=self.clock.monotonic)
        with self.offline():
            report = empty.diff(self.spec.id, self.first.snapshot, second, evidence=envelopes)
        self.assertEqual("complete", report["completeness"])
        self.assertEqual(local["paths"], report["paths"])
        self.assertEqual(local["text_diffs"], report["text_diffs"])
        self.assertEqual("portable-evidence", report["observation_times"]["from"]["origin"])
        self.assertEqual("2026-11-01T12:00:00+00:00", report["observation_times"]["from"]["verified_at"])
        tampered = json.loads(json.dumps(envelopes[1]))
        tampered["blobs"][0]["data"] = base64.b64encode(b"other bytes").decode("ascii")
        with self.assertRaises(SourceError) as caught:
            empty.diff(self.spec.id, self.first.snapshot, second, evidence=[envelopes[0], tampered])
        self.assertEqual("OAH_SOURCES_EVIDENCE_INVALID", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
