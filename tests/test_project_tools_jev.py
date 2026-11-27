# oah.project_tools_jev 테스트 — 키 조회 전에 목적지·키 이름·범위·기기 동의를 확인하는지, 동의 기록의 수명, 고정 연결 점검, Impact 약한 행 순서와 서버가 실제로 받는 내용을 본다.
from __future__ import annotations

import copy
import getpass
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

from oah import project_tools_jev as jev
from oah import rerank, secrets
from oah.impact.query import Row
from oah.rerank import typesafe
from oah.rerank.base import ProviderError
from tests.helpers import FakeResponse, git_commit, make_git_repo, make_tmp_dir
from tests.test_impact import fixture_repo
from tests.test_impact_map import SRC, run_impact
from tests.test_rerank import FakeServer
from tests.test_secrets_store import RecordingStore, fake_key

OFFICIAL = typesafe.OFFICIAL_URL
SHARED = typesafe.DEFAULT_KEY_NAME
OTHER_NAME = "ACME_JEV_NAME"
ELSEWHERE = "https://jev.example.test/v1/systemone"


def settings(url: str | None = None, key_name: str | None = None, model: str | None = None, **overrides) -> dict:
    """An enabled [rerank] table for TypeSafe, as load_config would hand it over."""
    table = copy.deepcopy(rerank.DEFAULTS)
    table.update(enabled=True, provider="typesafe", **overrides)
    for name, value in (("url", url), ("key_name", key_name), ("model", model)):
        if value is not None:
            table["typesafe"][name] = value
    return table


class Verdict:
    """An approval double that answers one word and records what it was asked."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked: list[tuple] = []

    def verdict(self, scopes, *, url, key_name, model) -> str:
        self.asked.append((tuple(scopes), url, key_name, model))
        return self.answer


class ScoreByName:
    """A provider registered as typesafe that scores each candidate from a table and records every call."""

    name = "typesafe"

    def __init__(self, scores=None) -> None:
        self.scores = scores or {}
        self.calls: list[dict] = []

    def key_name(self, config) -> str:
        return typesafe.TypeSafeProvider().key_name(config)

    def score(self, kind, query, items, *, key, config):
        self.calls.append({"kind": kind, "query": query, "items": [(item.name, item.description) for item in items],
                           "key": key, "timeout": config["timeout_seconds"], "limit": config["max_candidates"]})
        return [self.scores.get(item.name, 0.5) for item in items]


def admit(config: dict, **options) -> rerank.Admission:
    options.setdefault("providers", {"typesafe": ScoreByName()})
    return rerank.admit(config, **options)


class DestinationTest(unittest.TestCase):
    def test_kinds(self) -> None:
        cases = [
            (settings(), SHARED, typesafe.OFFICIAL),
            (settings(url=OFFICIAL), OTHER_NAME, typesafe.CUSTOM),
            (settings(url="http://127.0.0.1:9/v1/systemone"), SHARED, typesafe.LOOPBACK),
            (settings(url="http://localhost:9/x"), OTHER_NAME, typesafe.LOOPBACK),
            (settings(url=ELSEWHERE), OTHER_NAME, typesafe.CUSTOM),
            (settings(url=OFFICIAL + "/"), SHARED, typesafe.CUSTOM),
            (settings(url=OFFICIAL.replace("api.", "API.")), SHARED, typesafe.CUSTOM),
            (settings(url=OFFICIAL.replace(".ai/", ".ai:443/")), SHARED, typesafe.CUSTOM),
            (settings(url=OFFICIAL.replace(".ai/", ".ai.example.test/")), SHARED, typesafe.CUSTOM),
            (settings(url=OFFICIAL + "?next=1"), SHARED, typesafe.REFUSED),
            (settings(url=OFFICIAL + "#part"), SHARED, typesafe.REFUSED),
            (settings(url=OFFICIAL.replace("//", "//user:word@")), SHARED, typesafe.REFUSED),
            (settings(url="http://jev.example.test/v1/systemone"), SHARED, typesafe.REFUSED),
            (settings(url="ftp://jev.example.test/x"), SHARED, typesafe.REFUSED),
            (settings(url="https://"), SHARED, typesafe.REFUSED),
        ]
        for table, key_name, kind in cases:
            with self.subTest(url=table["typesafe"]["url"], key_name=key_name):
                self.assertEqual(typesafe.destination(table, key_name)[0], kind)

    def test_reads_what_the_provider_will_use(self) -> None:
        table = settings(url=ELSEWHERE, model="jev-2")
        self.assertEqual(typesafe.destination(table, OTHER_NAME), (typesafe.CUSTOM, ELSEWHERE, "jev-2"))
        self.assertEqual(typesafe.destination({"typesafe": "not a table"}, SHARED),
                         (typesafe.OFFICIAL, OFFICIAL, typesafe.DEFAULT_MODEL))


class AdmitTest(unittest.TestCase):
    """Destination, key name, scope and approval are settled first; a refusal never reaches a key store."""

    def setUp(self) -> None:
        self.key = fake_key()
        self.store = RecordingStore({SHARED: fake_key(), OTHER_NAME: fake_key()})

    def assert_refused(self, result: rerank.Admission, status: str, reason: str | None) -> None:
        self.assertEqual((result.provider, result.key, result.status, result.reason), (None, None, status, reason))
        self.assertEqual(self.store.calls, [], "a refusal never reads the key store")

    def test_environment_key_for_the_official_pair_keeps_its_behaviour(self) -> None:
        result = admit(settings(), env={SHARED: self.key}, keychain=self.store, scopes=("skills",))
        self.assertEqual((result.status, result.key.reveal(), result.consented), ("used", self.key, False))
        self.assertEqual(self.store.calls, [])

    def test_a_keychain_double_without_an_approval_is_read_as_before(self) -> None:
        result = admit(settings(), env={}, keychain=self.store, scopes=("skills",))
        self.assertEqual((result.status, result.key.reveal()), ("used", self.store.values[SHARED]))
        self.assertEqual(self.store.calls, [("probe", SHARED)])

    def test_the_device_key_needs_the_approval_before_any_lookup(self) -> None:
        for answer in (rerank.NO_CONSENT, rerank.STALE, rerank.REVOKED, rerank.DEVICE_STATE):
            with self.subTest(verdict=answer):
                approval = Verdict(answer)
                result = admit(settings(), env={}, keychain=self.store, scopes=("skills",), approval=approval)
                self.assert_refused(result, "unused(no key)", answer)
                self.assertEqual(approval.asked, [(("skills",), OFFICIAL, SHARED, typesafe.DEFAULT_MODEL)])
        approval = Verdict(rerank.GRANTED)
        result = admit(settings(), env={}, keychain=self.store, scopes=("skills",), approval=approval)
        self.assertEqual((result.status, result.key.reveal(), result.consented),
                         ("used", self.store.values[SHARED], True))
        self.assertEqual(self.store.calls, [("probe", SHARED)])

    def test_a_project_switched_off_stops_the_environment_key_too(self) -> None:
        for answer in (rerank.REVOKED, rerank.DEVICE_STATE):
            with self.subTest(verdict=answer):
                result = admit(settings(), env={SHARED: self.key}, keychain=self.store, scopes=("skills",),
                               approval=Verdict(answer))
                self.assert_refused(result, "unused(no key)", answer)
        for answer in (rerank.NO_CONSENT, rerank.STALE):
            with self.subTest(verdict=answer):
                result = admit(settings(), env={SHARED: self.key}, keychain=self.store, scopes=("skills",),
                               approval=Verdict(answer))
                self.assertEqual((result.status, result.key.reveal(), result.consented), ("used", self.key, False))

    def test_impact_needs_the_approval_whatever_the_key_source(self) -> None:
        self.assert_refused(admit(settings(), env={SHARED: self.key}, keychain=self.store, scopes=("impact",)),
                            "unused(no key)", rerank.NO_CONSENT)
        for answer in (rerank.NO_CONSENT, rerank.STALE, rerank.REVOKED, rerank.DEVICE_STATE):
            with self.subTest(verdict=answer):
                result = admit(settings(), env={SHARED: self.key}, keychain=self.store, scopes=("impact",),
                               approval=Verdict(answer))
                self.assert_refused(result, "unused(no key)", answer)
        result = admit(settings(), env={SHARED: self.key}, keychain=self.store, scopes=("impact",),
                       approval=Verdict(rerank.GRANTED))
        self.assertEqual((result.status, result.key.reveal(), result.consented), ("used", self.key, True))

    def test_the_shared_key_name_never_goes_to_another_address(self) -> None:
        approval = Verdict(rerank.GRANTED)
        for url in (ELSEWHERE, OFFICIAL + "/", OFFICIAL.replace(".ai/", ".ai.example.test/")):
            with self.subTest(url=url):
                result = admit(settings(url=url), env={SHARED: self.key}, keychain=self.store, scopes=("skills",),
                               approval=approval)
                self.assert_refused(result, "unused(error:insecure-url)", rerank.DESTINATION)
            with self.subTest(url=url, environment="empty"):
                result = admit(settings(url=url), env={}, keychain=self.store, scopes=("skills",), approval=approval)
                self.assert_refused(result, "unused(no key)", rerank.DESTINATION)
        self.assertEqual(approval.asked, [], "no approval can allow it")

    def test_another_address_reads_only_its_own_environment_key_after_approval(self) -> None:
        table = settings(url=ELSEWHERE, key_name=OTHER_NAME)
        self.assert_refused(admit(table, env={OTHER_NAME: self.key}, keychain=self.store, scopes=("skills",)),
                            "unused(no key)", rerank.NO_CONSENT)
        self.assert_refused(admit(table, env={OTHER_NAME: self.key}, keychain=self.store, scopes=("skills",),
                                  approval=Verdict(rerank.NO_CONSENT)), "unused(no key)", rerank.NO_CONSENT)
        approval = Verdict(rerank.GRANTED)
        result = admit(table, env={OTHER_NAME: self.key, SHARED: fake_key()}, keychain=self.store,
                       scopes=("skills",), approval=approval)
        self.assertEqual((result.status, result.key.reveal(), result.consented), ("used", self.key, True))
        self.assertEqual(approval.asked, [(("skills",), ELSEWHERE, OTHER_NAME, typesafe.DEFAULT_MODEL)])
        # No environment value: the key store holds one under both names, and neither is forwarded.
        self.assert_refused(admit(table, env={SHARED: fake_key()}, keychain=self.store, scopes=("skills",),
                                  approval=Verdict(rerank.GRANTED)), "unused(no key)", None)

    def test_the_official_address_with_another_key_name_is_another_destination(self) -> None:
        table = settings(key_name=OTHER_NAME)
        self.assert_refused(admit(table, env={OTHER_NAME: self.key}, keychain=self.store, scopes=("skills",)),
                            "unused(no key)", rerank.NO_CONSENT)
        self.assert_refused(admit(table, env={}, keychain=self.store, scopes=("skills",),
                                  approval=Verdict(rerank.GRANTED)), "unused(no key)", None)

    def test_a_loopback_address_takes_the_environment_key_and_never_the_key_store(self) -> None:
        table = settings(url="http://127.0.0.1:9/v1/systemone")
        result = admit(table, env={SHARED: self.key}, keychain=self.store, scopes=("skills",))
        self.assertEqual((result.status, result.key.reveal()), ("used", self.key))
        self.assert_refused(admit(table, env={}, keychain=self.store, scopes=("skills",)), "unused(no key)", None)
        self.assert_refused(admit(table, env={}, keychain=self.store, scopes=("skills",),
                                  approval=Verdict(rerank.GRANTED)), "unused(no key)", None)

    def test_a_refused_address_or_key_name_is_settled_from_text(self) -> None:
        approval = Verdict(rerank.GRANTED)
        for url in ("http://jev.example.test/v1/systemone", OFFICIAL + "?a=1", OFFICIAL.replace("//", "//u:p@")):
            with self.subTest(url=url):
                result = admit(settings(url=url), env={SHARED: self.key}, keychain=self.store, approval=approval)
                self.assert_refused(result, "unused(error:insecure-url)", "insecure-url")
                result = admit(settings(url=url), env={}, keychain=self.store, approval=approval)
                self.assert_refused(result, "unused(no key)", "insecure-url")
        for name in ("has space", "", "9START", "a" * 65):
            with self.subTest(key_name=name):
                table = settings()
                table["typesafe"]["key_name"] = name or " "
                result = admit(table, env={SHARED: self.key}, keychain=self.store, approval=approval)
                self.assert_refused(result, "unused(error:key-name)", None)
        self.assertEqual(approval.asked, [])

    def test_key_store_refusals_are_named(self) -> None:
        for state, reason in ((secrets.LOCKED, rerank.KEY_LOCKED), (secrets.DENIED, rerank.KEY_DENIED),
                              (secrets.MISSING, None), (secrets.UNSUPPORTED, None), (secrets.FAILED, None)):
            with self.subTest(state=state):
                result = admit(settings(), env={}, keychain=RecordingStore(state=state), scopes=("skills",),
                               approval=Verdict(rerank.GRANTED))
                self.assertEqual((result.key, result.status, result.reason), (None, "unused(no key)", reason))

    def test_a_caller_that_names_no_scope_needs_them_all(self) -> None:
        approval = Verdict(rerank.GRANTED)
        admit(settings(), env={}, keychain=self.store, approval=approval)
        self.assertEqual(approval.asked[0][0], rerank.SCOPES)
        result = admit(settings(), env={SHARED: self.key}, keychain=self.store)
        self.assertEqual((result.status, result.consented), ("used", False), "an environment key stays as before")
        for bad in ((), ("impact", "everything"), ("Impact",)):
            with self.subTest(scopes=bad), self.assertRaises(ValueError):
                admit(settings(), env={}, keychain=self.store, scopes=bad)

    def test_earlier_gates_still_come_first(self) -> None:
        approval = Verdict(rerank.GRANTED)
        cases = [(dict(settings(), enabled=False), {}, "unused(not-enabled)"),
                 (dict(settings(), provider=""), {}, "unused(not-configured)"),
                 (dict(settings(), provider="nope"), {}, "unused(unknown-provider)"),
                 (settings(), {"brief_disabled": True}, "unused(disabled-by-brief)")]
        for table, extra, status in cases:
            with self.subTest(status=status):
                result = admit(table, env={SHARED: self.key}, keychain=self.store, approval=approval, **extra)
                self.assert_refused(result, status, None)
        self.assertEqual(approval.asked, [])

    def test_another_provider_keeps_the_plain_lookup_but_never_sends_impact(self) -> None:
        class Plain(ScoreByName):
            name = "plain"

            def key_name(self, config) -> str:
                return "PLAIN_NAME"

        providers = {"plain": Plain()}
        table = dict(settings(), provider="plain")
        result = rerank.admit(table, env={"PLAIN_NAME": self.key}, keychain=self.store, providers=providers)
        self.assertEqual((result.status, result.key.reveal()), ("used", self.key))
        result = rerank.admit(table, env={"PLAIN_NAME": self.key}, keychain=self.store, providers=providers,
                              scopes=("impact",), approval=Verdict(rerank.GRANTED))
        self.assert_refused(result, "unused(unknown-provider)", None)

    def test_the_gate_wrapper_returns_the_three_legacy_values(self) -> None:
        provider, key, status = rerank._gate(settings(), brief_disabled=False, env={SHARED: self.key},
                                             keychain=self.store, providers={"typesafe": ScoreByName()})
        self.assertEqual((provider.name, key.reveal(), status), ("typesafe", self.key, "used"))

    def test_without_an_injected_keychain_the_device_approval_decides(self) -> None:
        approval = Verdict(rerank.NO_CONSENT)
        with mock.patch.object(jev, "device_approval", return_value=approval) as made, \
                mock.patch.object(secrets, "system_keychain", return_value=self.store):
            result = admit(settings(), env={}, scopes=("skills",), root=pathlib.Path("somewhere"))
            self.assert_refused(result, "unused(no key)", rerank.NO_CONSENT)
            made.assert_called_once_with(pathlib.Path("somewhere"), env={})
            approval.answer = rerank.GRANTED
            result = admit(settings(), env={}, scopes=("skills",), root=pathlib.Path("somewhere"))
            self.assertEqual((result.status, result.key.reveal(), result.consented),
                             ("used", self.store.values[SHARED], True))

    def test_the_device_key_store_itself_counts_as_no_injection(self) -> None:
        empty = make_tmp_dir(self)
        chain = secrets.MacKeychain(search_path=str(empty))
        approval = Verdict(rerank.NO_CONSENT)
        with mock.patch.object(jev, "device_approval", return_value=approval):
            result = admit(settings(), env={}, keychain=chain, scopes=("skills",), root=pathlib.Path("somewhere"))
        self.assertEqual((result.status, result.reason), ("unused(no key)", rerank.NO_CONSENT))
        self.assertEqual(len(approval.asked), 1)


def write_config(repo: pathlib.Path, *, url: str | None = None, key_name: str | None = None,
                 model: str | None = None, extra: str = "", impact: bool = True) -> None:
    lines = ["[impact]", f"rerank = {'true' if impact else 'false'}", "[rerank]", "enabled = true",
             'provider = "typesafe"', extra, "[rerank.typesafe]"]
    lines += [f'{name} = "{value}"' for name, value in (("url", url), ("key_name", key_name), ("model", model))
              if value is not None]
    (repo / ".oah").mkdir(exist_ok=True)
    (repo / ".oah" / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class DeviceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = make_git_repo(self)
        self.home = make_tmp_dir(self) / "device"
        self.env = {"OAH_HOME": str(self.home)}
        write_config(self.repo)

    def approve(self, *scopes: str, repo: pathlib.Path | None = None) -> dict:
        preview = jev.prepare(repo or self.repo, scopes=scopes or ("impact",), env=self.env)
        self.assertTrue(preview["ok"], preview)
        result = jev.enable(repo or self.repo, token=preview["token"], env=self.env)
        self.assertEqual(result["status"], "enabled", result)
        return preview

    def verdict(self, *scopes: str, repo: pathlib.Path | None = None, url: str = OFFICIAL, key_name: str = SHARED,
                model: str = typesafe.DEFAULT_MODEL, env: dict | None = None) -> str:
        approval = jev.device_approval(repo or self.repo, env=self.env if env is None else env)
        return approval.verdict(scopes or ("impact",), url=url, key_name=key_name, model=model)

    def record(self) -> pathlib.Path:
        return self.home / "state" / "jev" / "projects" / f"{jev.project(self.repo).id}.json"


class ConsentTest(DeviceCase):
    def test_preview_then_enable_then_disable(self) -> None:
        self.assertEqual(jev.status(self.repo, env=self.env)["consent"], "none")
        self.assertEqual(self.verdict(), rerank.NO_CONSENT)
        self.assertFalse(self.home.exists(), "a preview and a question write nothing")
        preview = self.approve("impact")
        self.assertEqual(preview["destination"], {"kind": "official", "url": OFFICIAL, "key_name": SHARED,
                                                  "model": typesafe.DEFAULT_MODEL})
        self.assertEqual(preview["credential"], ["environment", "device"])
        self.assertEqual(self.verdict(), rerank.GRANTED)
        self.assertEqual(self.verdict("skills"), rerank.NO_CONSENT, "a scope no approval covers")
        self.assertEqual(self.verdict("impact", "skills"), rerank.NO_CONSENT)
        status = jev.status(self.repo, env=self.env)
        self.assertEqual((status["consent"], status["scopes"]), ("enabled", {"impact": "granted"}))
        self.assertEqual(jev.disable(self.repo, env=self.env), {"ok": True, "status": "disabled"})
        self.assertEqual(self.verdict(), rerank.REVOKED)
        self.assertEqual(jev.status(self.repo, env=self.env)["consent"], "disabled")
        self.assertEqual(jev.disable(self.repo, env=self.env)["status"], "disabled", "switching off twice is fine")
        self.approve("impact")
        self.assertEqual(self.verdict(), rerank.GRANTED, "approving again switches the project back on")

    def test_the_impact_preview_lists_exactly_what_is_sent(self) -> None:
        scope = jev.prepare(self.repo, env=self.env)["scopes"][0]
        self.assertEqual(scope["scope"], "impact")
        self.assertEqual(scope["sends"], ["path", "line", "binding", "layer", "kind"])
        self.assertEqual(scope["sends"], list(jev.IMPACT_FIELDS))
        self.assertEqual(scope["labels"], [f"task kind: {jev.IMPACT_TASK}", f"query: {jev.IMPACT_QUERY}"])
        self.assertEqual(scope["never"], ["the row detail", "the queried target", "file contents", "absolute paths"])
        self.assertEqual(scope["bounds"], {"rows in one request": 50, "requests in one run": 1, "seconds waited": 10,
                                           "characters in one path": 200})
        self.assertEqual(scope["digest"], jev.policy_digest("impact"))

    def test_the_record_holds_no_key_and_is_private_to_the_user(self) -> None:
        self.approve("impact")
        record = json.loads(self.record().read_text(encoding="utf-8"))
        self.assertEqual(sorted(record), ["destination", "project", "schema", "scopes", "state", "updated"])
        self.assertEqual(record["scopes"], {"impact": jev.policy_digest("impact")})
        self.assertEqual(record["destination"], {"url": OFFICIAL, "key_name": SHARED, "model": typesafe.DEFAULT_MODEL})
        if os.name != "nt":
            self.assertEqual(self.record().stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.record().parent.stat().st_mode & 0o777, 0o700)

    def test_a_changed_destination_or_policy_makes_the_approval_stale(self) -> None:
        self.approve("impact")
        self.assertEqual(self.verdict(model="jev-other"), rerank.STALE)
        self.assertEqual(self.verdict(url=ELSEWHERE, key_name=OTHER_NAME), rerank.STALE)
        write_config(self.repo, model="jev-other")
        status = jev.status(self.repo, env=self.env)
        self.assertEqual((status["consent"], status["scopes"]), ("consent-stale", {"impact": "consent-stale"}))
        changed = jev.POLICIES["impact"]._replace(sends=jev.IMPACT_FIELDS + ("detail",))
        with mock.patch.dict(jev.POLICIES, {"impact": changed}):
            self.assertEqual(self.verdict(), rerank.STALE)
        with mock.patch.dict(jev.POLICIES, {"impact": jev.POLICIES["impact"]._replace(revision=2)}):
            self.assertEqual(self.verdict(), rerank.STALE)
        self.assertEqual(self.verdict(), rerank.GRANTED)

    def test_enable_records_only_what_the_preview_showed(self) -> None:
        preview = jev.prepare(self.repo, env=self.env)
        write_config(self.repo, model="jev-other")
        self.assertEqual(jev.enable(self.repo, token=preview["token"], env=self.env),
                         {"ok": False, "status": "preview-changed"})
        write_config(self.repo)
        for token in ("", "v1:impact:" + "0" * 64, preview["token"].replace("impact", "skills"),
                      preview["token"].replace("impact", "impact,impact"), "v1:everything:" + "0" * 64, None, 7):
            with self.subTest(token=token):
                self.assertEqual(jev.enable(self.repo, token=token, env=self.env)["status"], "preview-changed")
        self.assertFalse(self.record().exists())
        other = make_git_repo(self)
        write_config(other)
        self.assertEqual(jev.enable(other, token=preview["token"], env=self.env)["status"], "preview-changed",
                         "a token is for one project")

    def test_worktrees_share_the_approval_and_other_projects_do_not(self) -> None:
        git_commit(self.repo, {"a.txt": "a\n"})
        self.approve("impact")
        tree = make_tmp_dir(self) / "tree"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "--detach", str(tree)], check=True,
                       capture_output=True)
        self.assertEqual(jev.project(tree).id, jev.project(self.repo).id)
        self.assertEqual(self.verdict(repo=tree), rerank.GRANTED)
        self.assertEqual(self.verdict(repo=tree / ".oah"), rerank.GRANTED, "a folder inside the project")
        other = make_git_repo(self)
        self.assertEqual(self.verdict(repo=other), rerank.NO_CONSENT)
        folder = make_tmp_dir(self)
        self.assertEqual(jev.project(folder).kind, "folder")
        self.assertEqual(self.verdict(repo=folder), rerank.NO_CONSENT)
        write_config(folder)
        self.approve("impact", repo=folder)
        self.assertEqual(self.verdict(repo=folder), rerank.GRANTED)

    def test_a_record_that_cannot_be_trusted_stops_everything(self) -> None:
        self.approve("impact")
        good = self.record().read_text(encoding="utf-8")
        record = json.loads(good)
        broken = {
            "not JSON": "{",
            "a list": "[]",
            "another schema": json.dumps(dict(record, schema=2)),
            "another project": json.dumps(dict(record, project=dict(record["project"], path="elsewhere"))),
            "an unknown state": json.dumps(dict(record, state="maybe")),
            "an unknown scope": json.dumps(dict(record, scopes={"everything": "0" * 64})),
            "a scope no approval covers": json.dumps(dict(record, scopes=dict(record["scopes"], skills="0" * 64))),
            "no scopes": json.dumps(dict(record, scopes={})),
            "a short digest": json.dumps(dict(record, scopes={"impact": "abc"})),
            "an extra destination field": json.dumps(dict(record, destination=dict(record["destination"], x="y"))),
            "too large": json.dumps(dict(record, padding="x" * jev.RECORD_LIMIT)),
        }
        for label, text in broken.items():
            with self.subTest(record=label):
                self.record().write_text(text, encoding="utf-8")
                self.assertEqual(self.verdict(), rerank.DEVICE_STATE)
                self.assertEqual(jev.status(self.repo, env=self.env)["consent"], "device-state")
        self.record().write_text(good, encoding="utf-8")
        self.assertEqual(self.verdict(), rerank.GRANTED)
        self.assertEqual(jev.disable(self.repo, env=self.env)["status"], "disabled", "switching off repairs it")
        self.assertEqual(self.verdict(), rerank.REVOKED)

    @unittest.skipIf(os.name == "nt", "POSIX ownership and modes")
    def test_a_record_others_could_write_or_a_link_is_not_trusted(self) -> None:
        self.approve("impact")
        path = self.record()
        path.chmod(0o620)
        self.assertEqual(self.verdict(), rerank.DEVICE_STATE)
        path.chmod(0o602)
        self.assertEqual(self.verdict(), rerank.DEVICE_STATE)
        path.chmod(0o600)
        self.assertEqual(self.verdict(), rerank.GRANTED)
        real = path.with_name("kept.json")
        path.rename(real)
        path.symlink_to(real)
        self.assertEqual(self.verdict(), rerank.DEVICE_STATE)
        path.unlink()
        path.mkdir()
        self.assertEqual(self.verdict(), rerank.DEVICE_STATE)

    def test_the_approval_never_lives_where_the_project_could_ship_it(self) -> None:
        self.approve("impact")
        for label, env in (("a relative folder", {"OAH_HOME": "device"}),
                           ("inside the work tree", {"OAH_HOME": str(self.repo / "device")}),
                           ("inside the git folder", {"OAH_HOME": str(self.repo / ".git" / "device")}),
                           ("the home folder is the project", {"HOME": str(self.repo), "USERPROFILE": str(self.repo)})):
            with self.subTest(place=label):
                self.assertEqual(self.verdict(env=env), rerank.DEVICE_STATE)
                self.assertEqual(jev.prepare(self.repo, env=env), {"ok": False, "status": "device-state"})
                self.assertEqual(jev.enable(self.repo, token="v1:impact:" + "0" * 64, env=env)["status"], "device-state")
                self.assertEqual(jev.disable(self.repo, env=env), {"ok": False, "status": "device-state"})
                self.assertEqual(jev.status(self.repo, env=env)["consent"], "device-state")
        self.assertFalse((self.repo / "device").exists())
        self.assertEqual(self.verdict(env={}), rerank.NO_CONSENT, "no home folder at all: nothing was ever recorded")
        self.assertEqual(jev.prepare(self.repo, env={})["status"], "device-state")
        home = make_tmp_dir(self)
        env = {"HOME": str(home), "USERPROFILE": str(home)}
        preview = jev.prepare(self.repo, env=env)
        self.assertEqual(jev.enable(self.repo, token=preview["token"], env=env)["status"], "enabled")
        self.assertTrue((home / ".oah" / "state" / "jev" / "projects").is_dir(), "the default is <home>/.oah")
        self.assertEqual(self.verdict(env=env), rerank.GRANTED)

    def test_refusals_of_a_preview(self) -> None:
        for scopes in ((), ("everything",), ("impact", "Impact"), ("skills",), ("impact", "skills"), rerank.SCOPES):
            with self.subTest(scopes=scopes):
                self.assertEqual(jev.prepare(self.repo, scopes=scopes, env=self.env), {"ok": False, "status": "scope"})
        for label, options in (("the shared key name elsewhere", {"url": ELSEWHERE}),
                               ("an address that is not HTTPS", {"url": "http://jev.example.test/x"}),
                               ("a URL too long to show", {"url": ELSEWHERE + "/" + "x" * 512, "key_name": OTHER_NAME}),
                               ("a model too long to show", {"model": "x" * 129}),
                               ("a key name that is not a name", {"key_name": "has space"})):
            with self.subTest(case=label):
                write_config(self.repo, **options)
                self.assertEqual(jev.prepare(self.repo, env=self.env), {"ok": False, "status": "destination"})
                self.assertEqual(jev.status(self.repo, env=self.env)["destination_status"], "destination")
        write_config(self.repo, extra="surprise = 1")
        self.assertEqual(jev.prepare(self.repo, env=self.env), {"ok": False, "status": "config-invalid"})
        write_config(self.repo, url=ELSEWHERE, key_name=OTHER_NAME)
        preview = jev.prepare(self.repo, env=self.env)
        self.assertEqual((preview["destination"]["kind"], preview["credential"]), ("custom", ["environment"]))

    def test_status_preview_enable_and_disable_look_no_key_up(self) -> None:
        asked: list[str] = []

        class Watched(dict):
            def get(self, name, default=None):
                asked.append(name)
                return super().get(name, default)

            def __getitem__(self, name):
                asked.append(name)
                return super().__getitem__(name)

        env = Watched(self.env, **{SHARED: fake_key(), OTHER_NAME: fake_key()})
        refuse = mock.Mock(side_effect=AssertionError("no key lookup is expected"))
        with mock.patch.object(secrets, "find", refuse), mock.patch.object(secrets, "get", refuse), \
                mock.patch.object(secrets, "system_keychain", refuse):
            jev.status(self.repo, env=env)
            preview = jev.prepare(self.repo, env=env)
            self.assertEqual(jev.enable(self.repo, token=preview["token"], env=env)["status"], "enabled")
            jev.device_approval(self.repo, env=env).verdict(("impact",), url=OFFICIAL, key_name=SHARED,
                                                           model=typesafe.DEFAULT_MODEL)
            self.assertEqual(jev.disable(self.repo, env=env)["status"], "disabled")
            self.assertEqual(jev.prepare_test()["status"], "ready")
        refuse.assert_not_called()
        self.assertEqual(sorted(set(asked)), ["OAH_HOME"], "the environment is read for the device folder alone")


class Terminal(io.BytesIO):
    """A stream that says it is a terminal and must never be read."""

    def isatty(self) -> bool:
        return True

    def read(self, *args):
        raise AssertionError("a terminal is never read")


class KeyCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RecordingStore()
        self.value = fake_key()

    def assert_silent(self, result: dict) -> None:
        self.assertNotIn(self.value, json.dumps(result))

    def test_store_from_a_secret_and_from_a_pipe(self) -> None:
        result = jev.store_key(secrets.Secret(self.value), store=self.store)
        self.assertEqual(result, {"ok": True, "status": "added"})
        self.assertEqual(self.store.values, {SHARED: self.value})
        newer = fake_key()
        result = jev.store_key(io.BytesIO((newer + "\n").encode()), store=self.store)
        self.assertEqual(result, {"ok": True, "status": "updated"})
        self.assertEqual(self.store.values, {SHARED: newer})

    def test_a_value_that_is_not_one_visible_ascii_line_is_invalid(self) -> None:
        for label, source in (("a space", secrets.Secret(self.value + " x")),
                              ("not ASCII", secrets.Secret(self.value + "\u00e9")),
                              ("two lines", io.BytesIO((self.value + "\n" + self.value).encode())),
                              ("empty", io.BytesIO(b"")), ("too long", io.BytesIO(b"a" * 5000)),
                              ("a terminal", Terminal()), ("plain text", self.value), ("nothing", None)):
            with self.subTest(source=label):
                result = jev.store_key(source, store=self.store)
                self.assertEqual(result, {"ok": False, "status": "invalid"})
                self.assert_silent(result)
        self.assertEqual(self.store.calls, [])

    def test_key_store_refusals_are_words(self) -> None:
        for state in (secrets.LOCKED, secrets.DENIED, secrets.UNSUPPORTED, secrets.FAILED):
            with self.subTest(state=state):
                store = RecordingStore(state=state)
                result = jev.store_key(secrets.Secret(self.value), store=store)
                self.assertEqual(result, {"ok": False, "status": state})
                self.assert_silent(result)
                self.assertEqual(jev.delete_key(store=store), {"ok": False, "status": state})
                self.assertEqual(jev.key_state(env={}, store=store),
                                 {"ok": True, "status": "ok", "name": SHARED, "environment": False, "device": state})

    def test_delete_and_state(self) -> None:
        self.assertEqual(jev.key_state(env={}, store=self.store)["device"], "missing")
        jev.store_key(secrets.Secret(self.value), store=self.store)
        state = jev.key_state(env={SHARED: fake_key()}, store=self.store)
        self.assertEqual(state, {"ok": True, "status": "ok", "name": SHARED, "environment": True,
                                 "device": "present"})
        self.assert_silent(state)
        self.assertEqual(jev.delete_key(store=self.store), {"ok": True, "status": "removed"})
        self.assertEqual(jev.delete_key(store=self.store), {"ok": True, "status": "missing"})
        self.assertEqual({name for _call, name in self.store.calls}, {SHARED}, "one fixed name, the oah.secrets label")


class Unread:
    """The stream handed to a command that takes no value: any use of it fails the test."""

    def read(self, *args):
        raise AssertionError("this command reads no value")

    def isatty(self) -> bool:
        raise AssertionError("this command reads no value")


UNREAD = Unread()


def pipe(text: str) -> io.BytesIO:
    return io.BytesIO(text.encode("utf-8"))


class DispatchTest(DeviceCase):
    """The packaged entry: a fixed command word, the folder as a flag, a token or the key on a byte stream."""

    USAGE = (2, {"ok": False, "status": "usage"})

    def setUp(self) -> None:
        super().setUp()
        self.store = RecordingStore()
        self.root = str(self.repo)

    def call(self, *argv: str, stdin: object = UNREAD, **options) -> tuple[int, dict]:
        options.setdefault("env", self.env)
        options.setdefault("store", self.store)
        code, result = jev.dispatch(argv, stdin=stdin, **options)
        self.assertEqual(code == 0, result["ok"] is True)
        self.assertIsInstance(result["status"], str)
        json.dumps(result)  # every result is ready for JSON
        return code, result

    def test_the_approval_round_trip(self) -> None:
        code, preview = self.call("prepare", "--root", self.root)
        self.assertEqual((code, preview), (0, jev.prepare(self.repo, env=self.env)))
        self.assertEqual(self.call("status", "--root", self.root)[1]["consent"], "none")
        self.assertEqual(self.call("enable", "--root", self.root, stdin=pipe(preview["token"] + "\n")),
                         (0, {"ok": True, "status": "enabled", "scopes": ["impact"]}))
        code, shown = self.call("status", "--root", self.root)
        self.assertEqual((code, shown), (0, jev.status(self.repo, env=self.env)))
        self.assertEqual((shown["status"], shown["consent"], shown["scopes"]), ("ok", "enabled", {"impact": "granted"}))
        self.assertEqual(self.call("disable", "--root", self.root), (0, {"ok": True, "status": "disabled"}))
        self.assertEqual(self.call("status", "--root", self.root)[1]["consent"], "disabled")
        code, preview = self.call("prepare", "--root", self.root, "--scope", "impact")
        self.assertEqual((code, [item["scope"] for item in preview["scopes"]]), (0, ["impact"]))
        for scope in ("skills", "everything"):
            self.assertEqual(self.call("prepare", "--root", self.root, "--scope", scope, "--scope", "impact"),
                             (1, {"ok": False, "status": "scope"}))

    def test_commands_that_take_no_value_never_touch_the_stream(self) -> None:
        for argv in (("status", "--root", self.root), ("prepare", "--root", self.root),
                     ("disable", "--root", self.root), ("key-state",), ("key-delete",), ("test-prepare",)):
            with self.subTest(command=argv[0]):
                self.assertEqual(self.call(*argv)[0], 0)
        self.assertEqual(self.call("test-prepare"), (0, jev.prepare_test()))

    def test_a_token_or_key_among_the_arguments_is_a_usage_error(self) -> None:
        token = jev.prepare(self.repo, env=self.env)["token"]
        ack = jev.prepare_test()["token"]
        for argv in (("enable", "--root", self.root, token), ("enable", "--root", self.root, "--token", token),
                     ("enable", token, "--root", self.root), ("enable", "--token", token),
                     ("test-run", ack), ("test-run", "--ack", ack), ("test-run", "--token", ack),
                     ("key-store", fake_key()), ("key-store", "--key", fake_key()), ("key-store", "--value", "x")):
            with self.subTest(argv=argv[:2]):
                self.assertEqual(self.call(*argv), self.USAGE)
        self.assertEqual((self.store.calls, self.record().exists()), ([], False))

    def test_a_token_that_does_not_arrive_whole_approves_nothing(self) -> None:
        token = jev.prepare(self.repo, env=self.env)["token"]
        streams = (("no stream", None), ("empty", pipe("")), ("another token", pipe("v1:impact:" + "0" * 64)),
                   ("too long", pipe(token + "\n" + "x" * jev.TOKEN_LIMIT)), ("two lines", pipe(token + "\n\n")),
                   ("a terminal", Terminal()), ("text, not bytes", io.StringIO(token)))
        for label, stream in streams:
            with self.subTest(stream=label):
                self.assertEqual(self.call("enable", "--root", self.root, stdin=stream),
                                 (1, {"ok": False, "status": "preview-changed"}))
        self.assertFalse(self.record().exists())
        for ending in ("", "\n", "\r\n"):
            with self.subTest(ending=ending):
                self.assertEqual(self.call("enable", "--root", self.root, stdin=pipe(token + ending))[0], 0)

    def test_usage_and_folder_refusals(self) -> None:
        other = make_tmp_dir(self)
        (other / "file.txt").write_text("x", encoding="utf-8")
        calls = [(), ("",), ("STATUS", "--root", self.root), ("nope",), ("order", "--root", self.root), ("status",),
                 ("status", "--root"), ("status", self.root), ("status", "--root", self.root, "--root", self.root),
                 ("status", "--root", self.root, "--scope", "impact"), ("status", "--root=" + self.root),
                 ("status", "--roo", self.root), ("status", "-r", self.root), ("prepare", "--scope", "impact"),
                 ("disable", "--root", self.root, "extra"), ("key-state", "--root", self.root),
                 ("key-delete", "--scope", "impact"), ("test-prepare", "--root", self.root)]
        for argv in calls:
            with self.subTest(argv=argv):
                self.assertEqual(self.call(*argv), self.USAGE)
        for argv in ("status", None, 7, ["status", "--root", 7], [b"status"], {"status": "--root"}):
            with self.subTest(argv=argv):
                self.assertEqual(jev.dispatch(argv, env=self.env, store=self.store), self.USAGE)
        for root in ("relative/folder", ".", "", str(other / "missing"), str(other / "file.txt"), "\0",
                     str(other) + "\0"):
            with self.subTest(root=root):
                for command in jev.PROJECT_COMMANDS:
                    self.assertEqual(self.call(command, "--root", root), (2, {"ok": False, "status": "root"}))
        self.assertEqual((self.home.exists(), self.store.calls), (False, []))
        self.assertEqual(jev.COMMANDS, ("status", "prepare", "enable", "disable", "key-state", "key-store",
                                        "key-delete", "test-prepare", "test-run"))

    def test_the_key_arrives_on_the_stream_and_no_result_holds_it(self) -> None:
        value = fake_key()
        self.assertEqual(self.call("key-store", stdin=pipe(value + "\n")), (0, {"ok": True, "status": "added"}))
        self.assertEqual(self.store.values, {SHARED: value})
        self.assertEqual(self.call("key-state"), (0, {"ok": True, "status": "ok", "name": SHARED,
                                                      "environment": False, "device": "present"}))
        streams = (("no stream", None), ("empty", pipe("")), ("too long", pipe("a" * (secrets.HANDOFF_LIMIT + 1))),
                   ("two lines", pipe(fake_key() + "\n" + fake_key())), ("a space", pipe(fake_key() + " x")),
                   ("a terminal", Terminal()), ("text, not bytes", io.StringIO(fake_key())))
        for label, stream in streams:
            with self.subTest(stream=label):
                self.assertEqual(self.call("key-store", stdin=stream), (1, {"ok": False, "status": "invalid"}))
        self.assertEqual(self.store.values, {SHARED: value}, "a refused value changes nothing")
        for state in (secrets.LOCKED, secrets.DENIED, secrets.UNSUPPORTED, secrets.FAILED):
            with self.subTest(state=state):
                code, result = self.call("key-store", stdin=pipe(value), store=RecordingStore(state=state))
                self.assertEqual((code, result), (1, {"ok": False, "status": state}))
        self.assertEqual(self.call("key-delete"), (0, {"ok": True, "status": "removed"}))
        self.assertEqual(self.call("key-delete"), (0, {"ok": True, "status": "missing"}))

    def test_the_connection_check_takes_its_token_from_the_stream(self) -> None:
        ack = jev.prepare_test()["token"]
        sent = []

        def transport(payload, *, key, timeout):
            sent.append(payload)
            return json.loads(reply())

        env = dict(self.env, **{SHARED: fake_key()})
        for label, stream in (("no stream", None), ("empty", pipe("")), ("another", pipe("v1:probe:" + "0" * 64)),
                              ("a terminal", Terminal())):
            with self.subTest(stream=label):
                self.assertEqual(self.call("test-run", stdin=stream, env=env, transport=transport),
                                 (1, {"ok": False, "status": "ack-required"}))
        self.assertEqual((sent, self.store.calls), ([], []))
        self.assertEqual(self.call("test-run", stdin=pipe(ack + "\r\n"), env=env, transport=transport),
                         (0, {"ok": True, "status": "ok", "source": "environment"}))
        self.assertEqual(sent, [typesafe.build_probe_request()])
        self.assertEqual(self.call("test-run", stdin=pipe(ack), transport=transport),
                         (1, {"ok": False, "status": "no-key"}))

        def failing(payload, *, key, timeout):
            raise ProviderError("unauthorized")

        self.assertEqual(self.call("test-run", stdin=pipe(ack), env=env, transport=failing),
                         (1, {"ok": False, "status": "unauthorized", "source": "environment"}))

    def test_an_unexpected_failure_is_one_word(self) -> None:
        with mock.patch.object(jev, "status", side_effect=RuntimeError("CANARY " + self.root)):
            self.assertEqual(self.call("status", "--root", self.root), (1, {"ok": False, "status": "internal"}))
        with mock.patch.object(jev, "status", side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            self.call("status", "--root", self.root)

    def test_the_work_tree_configuration_decides_whatever_folder_inside_it_is_named(self) -> None:
        write_config(self.repo, url=ELSEWHERE, key_name=OTHER_NAME)
        inner = self.repo / "docs" / "deep"
        inner.mkdir(parents=True)
        code, preview = self.call("prepare", "--root", str(inner))
        self.assertEqual((code, preview), (0, jev.prepare(self.repo, env=self.env)))
        self.assertEqual(preview["destination"], {"kind": "custom", "url": ELSEWHERE, "key_name": OTHER_NAME,
                                                  "model": typesafe.DEFAULT_MODEL})
        self.assertEqual(self.call("enable", "--root", str(inner), stdin=pipe(preview["token"]))[0], 0)
        self.assertEqual(self.verdict(url=ELSEWHERE, key_name=OTHER_NAME), rerank.GRANTED)
        self.assertEqual(self.call("status", "--root", str(inner))[1]["consent"], "enabled")

    def test_a_real_process_takes_the_token_on_its_standard_input(self) -> None:
        program = ("import json, sys\n"
                   "from oah import project_tools_jev\n"
                   "code, result = project_tools_jev.dispatch(sys.argv[1:], stdin=sys.stdin.buffer)\n"
                   "print(json.dumps(result))\n"
                   "sys.exit(code)\n")
        env = dict(os.environ, PYTHONPATH=SRC, **self.env)
        elsewhere = make_tmp_dir(self)  # the current folder decides nothing

        def run(*argv: str, data: bytes = b"") -> tuple[int, dict, bytes]:
            done = subprocess.run([sys.executable, "-c", program, *argv], input=data, capture_output=True, env=env,
                                  cwd=elsewhere, timeout=120)
            return done.returncode, json.loads(done.stdout), done.stderr

        code, preview, errors = run("prepare", "--root", self.root)
        self.assertEqual((code, preview, errors), (0, jev.prepare(self.repo, env=self.env), b""))
        self.assertEqual(run("enable", "--root", self.root, preview["token"]), (*self.USAGE, b""))
        self.assertEqual(run("enable", "--root", self.root), (1, {"ok": False, "status": "preview-changed"}, b""))
        self.assertEqual(run("enable", "--root", self.root, data=preview["token"].encode() + b"\n"),
                         (0, {"ok": True, "status": "enabled", "scopes": ["impact"]}, b""))
        self.assertEqual(self.verdict(), rerank.GRANTED)
        self.assertEqual(run("status", "--root", self.root)[1]["consent"], "enabled")


class Recorder:
    """Stands in for OpenerDirector.open: records the request and the opener, then answers."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.seen: list[dict] = []

    def __call__(self, opener, request, data=None, timeout=None):
        self.seen.append({"url": request.full_url, "method": request.get_method(),
                          "headers": {name.lower(): value for name, value in request.header_items()},
                          "body": request.data, "timeout": timeout,
                          "handlers": [type(handler).__name__ for handler in opener.handlers],
                          "proxies": [handler.proxies for handler in opener.handlers
                                      if isinstance(handler, urllib.request.ProxyHandler)]})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def patch(self):
        """The patch that puts this recorder in place of every opener's `open`."""
        def open_recorded(opener, request, data=None, timeout=None):
            return self(opener, request, data, timeout)

        return mock.patch.object(urllib.request.OpenerDirector, "open", open_recorded)


def reply(noul: float = 0.9, model: object = "jev-1.14.0", **extra) -> bytes:
    return json.dumps({"model": model, "answers": {"check": {"type": "noul", "noul": noul}},
                       "usage": {"input_tokens": 9, "output_tokens": 1}, **extra}).encode()


class ConnectionCheckTest(unittest.TestCase):
    GOLDEN = {"model": "jev-latest",
              "state": "oah connection check. This text is fixed and carries no project data.",
              "questions": {"check": {"type": "noul", "instructions": "Is `state` a connection check?"}}}

    def setUp(self) -> None:
        self.key = fake_key()
        self.store = RecordingStore()
        self.ack = jev.prepare_test()["token"]

    def check(self, answer, **options) -> tuple[dict, Recorder]:
        recorder = Recorder(answer)
        with recorder.patch():
            result = jev.run_test(ack=self.ack, env=options.pop("env", {SHARED: self.key}), store=self.store, **options)
        return result, recorder

    def test_the_request_is_fixed_and_says_nothing_about_this_machine(self) -> None:
        payload = typesafe.build_probe_request()
        self.assertEqual(payload, self.GOLDEN)
        preview = jev.prepare_test()
        self.assertEqual((preview["url"], preview["key_name"], preview["request"], preview["requests"],
                          preview["timeout_seconds"]), (OFFICIAL, SHARED, self.GOLDEN, 1, 10.0))
        text = json.dumps(payload)
        for private in (os.getcwd(), str(pathlib.Path.home()), socket.gethostname(), getpass.getuser()):
            if len(private) > 2:
                self.assertNotIn(private, text)

    def test_nothing_is_looked_up_or_sent_without_the_token(self) -> None:
        asked: list[str] = []

        class Watched(dict):
            def get(self, name, default=None):
                asked.append(name)
                return super().get(name, default)

        recorder = Recorder(AssertionError("nothing is sent"))
        with recorder.patch():
            for ack in ("", "yes", None, True, self.ack[:-1], self.ack.upper(), "v1:probe:" + "0" * 64):
                with self.subTest(ack=ack):
                    result = jev.run_test(ack=ack, env=Watched({SHARED: self.key}), store=self.store)
                    self.assertEqual(result, {"ok": False, "status": "ack-required"})
        self.assertEqual((asked, self.store.calls, recorder.seen), ([], [], []))

    def test_one_request_to_the_official_address_without_proxy_or_redirect(self) -> None:
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                                          "ALL_PROXY": "http://127.0.0.1:9"}):
            result, recorder = self.check(FakeResponse(reply()))
        self.assertEqual(result, {"ok": True, "status": "ok", "source": "environment"})
        self.assertEqual(len(recorder.seen), 1)
        seen = recorder.seen[0]
        self.assertEqual((seen["url"], seen["method"], seen["timeout"]), (OFFICIAL, "POST", 10.0))
        self.assertEqual(json.loads(seen["body"]), self.GOLDEN)
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {self.key}")
        self.assertNotIn(self.key, seen["body"].decode())
        self.assertEqual([proxies for proxies in seen["proxies"] if proxies], [], "no proxy handler holds a proxy")
        self.assertIn("_NoRedirect", seen["handlers"])
        self.assertNotIn("HTTPRedirectHandler", seen["handlers"])
        self.assertEqual(self.store.calls, [], "the environment key comes first, as oah.secrets documents")

    def test_the_stored_key_is_used_when_the_environment_has_none(self) -> None:
        self.store.values[SHARED] = self.key
        result, recorder = self.check(FakeResponse(reply(model="")), env={SHARED: ""})
        self.assertEqual(result, {"ok": True, "status": "ok", "source": "device"})
        self.assertEqual(recorder.seen[0]["headers"]["authorization"], f"Bearer {self.key}")
        self.assertEqual(self.store.calls, [("probe", SHARED)])

    def test_no_key_and_key_store_refusals(self) -> None:
        for state, status in ((None, "no-key"), (secrets.LOCKED, "key-locked"), (secrets.DENIED, "key-denied"),
                              (secrets.UNSUPPORTED, "no-key"), (secrets.FAILED, "no-key")):
            with self.subTest(state=state):
                self.store = RecordingStore(state=state)
                result, recorder = self.check(AssertionError("nothing is sent"), env={})
                self.assertEqual((result, recorder.seen), ({"ok": False, "status": status}, []))

    def test_failures_are_one_word_and_never_response_text(self) -> None:
        echo = f"private {self.key} detail"

        def http(code: int) -> urllib.error.HTTPError:
            return urllib.error.HTTPError(OFFICIAL, code, echo, {}, io.BytesIO(echo.encode()))

        cases = [
            (http(401), "unauthorized"), (http(429), "rate-limited"), (http(529), "overloaded"),
            (http(400), "http-400"), (http(413), "http-413"), (http(415), "http-415"), (http(422), "http-422"),
            (http(500), "http-500"), (http(302), "http-302"),
            (urllib.error.URLError(TimeoutError(echo)), "timeout"), (TimeoutError(echo), "timeout"),
            (urllib.error.URLError(echo), "network"), (ConnectionResetError(echo), "network"),
            (FakeResponse(echo.encode()), "bad-response"), (FakeResponse(b"{}"), "bad-response"),
            (FakeResponse(reply(noul=1.5)), "bad-response"), (FakeResponse(reply(noul="NaN")), "bad-response"),
            (FakeResponse(reply() .replace(b'"check"', b'"other"')), "bad-response"),
            (FakeResponse(b'{"answers": {"check": {"type": "noul", "noul": 0.5}, "check": {"type": "noul", "noul": 0.6}}}'),
             "bad-response"),
            (FakeResponse(b" " * (typesafe.RESPONSE_LIMIT + 1)), "bad-response"),
        ]
        for answer, status in cases:
            with self.subTest(status=status, answer=type(answer).__name__):
                result, recorder = self.check(answer)
                self.assertEqual(result, {"ok": False, "status": status, "source": "environment"})
                self.assertEqual(len(recorder.seen), 1, "one request, never a retry")
                self.assertNotIn(self.key, json.dumps(result))

    def test_the_result_ignores_provider_metadata(self) -> None:
        result, _recorder = self.check(FakeResponse(reply(model="jev-preview", extra_field="test metadata")))
        self.assertEqual(result, {"ok": True, "status": "ok", "source": "environment"})

    def test_an_injected_transport_gets_the_fixed_payload_and_an_opaque_key(self) -> None:
        calls = []

        def transport(payload, *, key, timeout):
            calls.append((payload, key, timeout))
            return json.loads(reply())

        result = jev.run_test(ack=self.ack, env={SHARED: self.key}, store=self.store, transport=transport)
        self.assertEqual(result["status"], "ok")
        (payload, key, timeout), = calls
        self.assertEqual((payload, key.reveal(), timeout), (self.GOLDEN, self.key, 10.0))
        self.assertNotIn(self.key, repr(key))

        def failing(payload, *, key, timeout):
            raise ProviderError("rate-limited")

        self.assertEqual(jev.run_test(ack=self.ack, env={SHARED: self.key}, transport=failing),
                         {"ok": False, "status": "rate-limited", "source": "environment"})

    def test_the_official_post_ignores_any_configuration(self) -> None:
        recorder = Recorder(FakeResponse(reply()))
        with recorder.patch():
            typesafe.post_official(self.GOLDEN, key=secrets.Secret(self.key), timeout=999)
        self.assertEqual((recorder.seen[0]["url"], recorder.seen[0]["timeout"]), (OFFICIAL, 10.0))


def row(binding: str, layer: str, source: str, line: int | None = None, kind: str = "code",
        target: str = "CANARY-TARGET/app.py", detail: str = "CANARY-DETAIL uses a_private_table", score: int = 1) -> Row:
    return Row(binding, layer, kind, source, line, target, detail, score)


class ImpactOrderTest(DeviceCase):
    def setUp(self) -> None:
        super().setUp()
        self.key = fake_key()
        self.env[SHARED] = self.key
        self.store = RecordingStore({SHARED: fake_key()})
        self.rows = [
            row("pin", "gates", "ci gate", kind="gate"),
            row("exec", "lexical", "Makefile", 3, "workflow"),
            row("link", "lexical", "docs/a.md", 4, "doc"),
            row("link", "db", "db/001.sql", 9, "migration"),
            row("link", "lexical", "src/b.py", 7),
            row("link", "wiki", "docs/wiki/c.md", None, "doc"),
            row("mention", "graph", "src/d.py"),
            row("mention", "db", "db/002.sql", 2, "migration"),
            row("mention", "cochange", "tests/test_e.py", None, "test"),
            row("mention", "lexical", "src/f.py", 1, "house-kind"),
        ]
        self.files = {item.source: item.kind for item in self.rows if item.layer != "gates"}
        self.provider = ScoreByName({"docs/a.md:4": 0.1, "src/b.py:7": 0.9, "docs/wiki/c.md": 0.5,
                                     "src/d.py": 0.2, "tests/test_e.py": 0.3, "src/f.py:1": 0.8})

    def order(self, rows=None, **options):
        options.setdefault("env", self.env)
        options.setdefault("keychain", self.store)
        options.setdefault("providers", {"typesafe": self.provider})
        options.setdefault("files", self.files)
        return jev.order_impact(self.rows if rows is None else rows, root=self.repo, **options)

    def assert_untouched(self, result, status: str, *, requests: int = 0) -> None:
        self.assertEqual(result, (self.rows, status))
        self.assertEqual(len(self.provider.calls), requests)

    def test_only_the_weak_rows_that_were_sent_change_places(self) -> None:
        self.approve("impact")
        ordered, status = self.order()
        self.assertEqual(status, "used")
        self.assertEqual([item.where for item in ordered],
                         ["ci gate", "Makefile:3", "src/b.py:7", "db/001.sql:9", "docs/wiki/c.md", "docs/a.md:4",
                          "src/f.py:1", "db/002.sql:2", "tests/test_e.py", "src/d.py"])
        self.assertCountEqual(ordered, self.rows)
        for index, item in enumerate(self.rows):
            if item.binding in ("pin", "exec") or item.layer in ("gates", "db"):
                self.assertIs(ordered[index], item, "strong, gate and DB rows keep their place")
        self.assertEqual([item.binding for item in ordered], [item.binding for item in self.rows])
        self.assertIsNot(ordered, self.rows)
        self.assertEqual(self.store.calls, [], "the environment key came first")

    def test_the_request_holds_the_fixed_fields_and_nothing_else(self) -> None:
        self.approve("impact")
        self.order()
        (call,) = self.provider.calls
        self.assertEqual((call["kind"], call["query"]), ("impact", jev.IMPACT_QUERY))
        self.assertEqual(call["items"], [("docs/a.md:4", "link lexical doc"), ("src/b.py:7", "link lexical code"),
                                         ("docs/wiki/c.md", "link wiki doc"), ("src/d.py", "mention graph code"),
                                         ("tests/test_e.py", "mention cochange test"),
                                         ("src/f.py:1", "mention lexical other")])
        self.assertEqual((call["timeout"], call["limit"], call["key"].reveal()), (10.0, 50, self.key))

    def test_no_approval_no_request_and_no_key_lookup(self) -> None:
        del self.env[SHARED]
        self.assert_untouched(self.order(), "unused(no-consent)")
        self.approve("impact")
        jev.disable(self.repo, env=self.env)
        self.assert_untouched(self.order(), "unused(consent-revoked)")
        self.approve("impact")
        self.assert_untouched(self.order(config=settings(model="jev-other")), "unused(consent-stale)")
        with mock.patch.dict(jev.POLICIES, {"impact": jev.POLICIES["impact"]._replace(revision=2)}):
            self.assert_untouched(self.order(), "unused(consent-stale)")
        self.record().write_text("{", encoding="utf-8")
        self.assert_untouched(self.order(), "unused(device-state)")
        self.assertEqual(self.store.calls, [])

    def test_the_stored_key_is_used_only_with_the_approval(self) -> None:
        del self.env[SHARED]
        self.approve("impact")
        ordered, status = self.order()
        self.assertEqual(status, "used")
        self.assertEqual(self.provider.calls[0]["key"].reveal(), self.store.values[SHARED])
        self.assertEqual(self.store.calls, [("probe", SHARED)])
        for state, reason in ((secrets.LOCKED, "key-locked"), (secrets.DENIED, "key-denied"), (secrets.MISSING, "no key")):
            with self.subTest(state=state):
                result = self.order(keychain=RecordingStore(state=state))
                self.assertEqual(result, (self.rows, f"unused({reason})"))

    def test_states_that_send_nothing(self) -> None:
        self.approve("impact")
        refuse = Verdict(rerank.GRANTED)
        self.assert_untouched(self.order(offline=True, approval=refuse), "unused(offline)")
        self.assert_untouched(self.order(config=dict(settings(), enabled=False), approval=refuse), "unused(not-configured)")
        self.assert_untouched(self.order(config=dict(settings(), provider=" "), approval=refuse), "unused(not-configured)")
        strong = [item for item in self.rows if item.binding in ("pin", "exec")]
        self.assertEqual(self.order(strong, approval=refuse), (strong, "unused(no-candidates)"))
        self.assertEqual(self.order([], approval=refuse), ([], "unused(no-candidates)"))
        self.assert_untouched(self.order(files={}, approval=refuse), "unused(no-candidates)")
        self.assertEqual((refuse.asked, self.store.calls), ([], []), "none of these asks for approval or a key")
        self.assert_untouched(self.order(config=settings(url=ELSEWHERE)), "unused(destination)")
        self.assert_untouched(self.order(config=settings(url="http://jev.example.test/x")), "unused(error:insecure-url)")
        self.assert_untouched(self.order(config=settings(url="http://jev.example.test/x"), env={"OAH_HOME": str(self.home)}),
                              "unused(error:insecure-url)")
        self.assert_untouched(self.order(config=settings(url=ELSEWHERE), env={"OAH_HOME": str(self.home)}),
                              "unused(destination)")
        self.assert_untouched(self.order(config=dict(settings(), provider="nope")), "unused(unknown-provider)")
        write_config(self.repo, extra="surprise = 1")
        self.assert_untouched(self.order(), "unused(not-configured)")
        self.assert_untouched(self.order(config=dict(settings(), circuit_failures="many")), "unused(not-configured)")

    def test_the_configuration_can_lower_the_bounds_and_cannot_raise_them(self) -> None:
        self.approve("impact")
        many = [row("mention", "cochange", f"src/m{index:03}.py") for index in range(80)]
        files = {item.source for item in many}
        for limit, timeout, sent, waited in ((500, 999.0, 50, 10.0), (0, 0.0, 50, 10.0), (-3, -1.0, 50, 10.0),
                                             (3, 2.5, 3, 2.5), (50, 10.0, 50, 10.0)):
            with self.subTest(limit=limit, timeout=timeout):
                self.provider.calls.clear()
                table = settings(max_candidates=limit, timeout_seconds=timeout)
                ordered, status = self.order(many, files=files, config=table)
                self.assertEqual(status, "used")
                (call,) = self.provider.calls
                self.assertEqual((len(call["items"]), call["timeout"], call["limit"]), (sent, waited, sent))
                self.assertCountEqual(ordered, many)
                self.assertEqual(ordered[sent:], many[sent:], "rows past the bound keep their place")

    def test_rows_that_are_never_sent(self) -> None:
        self.approve("impact")
        shaped = "docs/" + "sk" + "-" + "a" * 24 + ".md"  # the shape of a key, which the scanner's rules flag
        unsafe = ["/abs/x.py", "~/x.py", "../x.py", "a/../x.py", "a//x.py", "./x.py", "a\\x.py", "C:/x.py", "a\nb.py",
                  "a\x7fb.py", "x" * 201, "", shaped, "unknown/x.py"]
        rows = [row("mention", "cochange", source) for source in unsafe]
        rows += [row("mention", "cochange", "ok.py", 0), row("mention", "cochange", "ok.py", True),
                 row("mention", "cochange", "ok.py", jev.LINE_LIMIT + 1), row("mention", "gates", "ok.py"),
                 row("link", "db", "ok.py"), row("pin", "lexical", "ok.py"), row("exec", "lexical", "ok.py"),
                 row("mention", "cochange", "ok.py"), row("mention", "graph", "ok.py"),
                 row("link", "lexical", "ok.py", 12), row("mention", "lexical", "ok.py", 12)]
        files = set(unsafe[:-1]) | {"ok.py"}
        ordered, status = self.order(rows, files=files)
        self.assertEqual(status, "used")
        self.assertEqual(self.provider.calls[0]["items"], [("ok.py", "mention cochange code"),
                                                          ("ok.py:12", "link lexical code")],
                         "a repeated name is sent once, under the first row that has it")
        self.assertEqual(ordered, rows)

    def test_a_failing_provider_leaves_the_rows(self) -> None:
        self.approve("impact")

        class Failing(ScoreByName):
            def __init__(self, error) -> None:
                super().__init__()
                self.error = error

            def score(self, kind, query, items, *, key, config):
                self.calls.append({})
                if isinstance(self.error, BaseException):
                    raise self.error
                return self.error

        for error, status in ((ProviderError("unauthorized"), "unused(error:unauthorized)"),
                              (ProviderError("rate-limited"), "unused(error:rate-limited)"),
                              (ProviderError("timeout"), "unused(error:timeout)"),
                              (RuntimeError("boom"), "unused(error:internal)"),
                              ([0.5], "unused(error:bad-response)"), ([float("nan")] * 6, "unused(error:bad-response)"),
                              ("high", "unused(error:bad-response)")):
            with self.subTest(status=status):
                failing = Failing(error)
                result = self.order(providers={"typesafe": failing})
                self.assertEqual(result, (self.rows, status))
                self.assertEqual(len(failing.calls), 1, "one request, never a retry")
        with self.assertRaises(KeyboardInterrupt):
            self.order(providers={"typesafe": Failing(KeyboardInterrupt())})


class ImpactWireTest(DeviceCase):
    """The real provider against a loopback server: what a server sees, and which HTTP answers fall back."""

    CANARIES = ("CANARY-DETAIL", "CANARY-TARGET", "a_private_table")

    def setUp(self) -> None:
        super().setUp()
        self.key = fake_key()
        self.env[SHARED] = self.key
        self.rows = [row("exec", "lexical", "Makefile", 3, "workflow"), row("link", "lexical", "docs/a.md", 4, "doc"),
                     row("link", "lexical", "src/b.py", 7), row("mention", "cochange", "tests/test_e.py", None, "test"),
                     row("mention", "graph", "src/d.py", None, "house-kind"), row("link", "db", "db/001.sql", 9, "migration")]
        self.files = {item.source: item.kind for item in self.rows}
        self.scores = {"docs/a.md:4": 0.2, "src/b.py:7": 0.9, "tests/test_e.py": 0.1, "src/d.py": 0.7}

    def serve(self, respond) -> FakeServer:
        server = FakeServer(self, respond)
        write_config(self.repo, url=server.url())
        self.approve("impact")
        return server

    def answers(self, path, body):
        names = [item["name"] for item in body["state"]["candidates"]]
        return 200, {"model": "jev-test", "answers": {f"c{index}": {"type": "noul", "noul": self.scores[name]}
                                                      for index, name in enumerate(names)}}, 0, {}

    def order(self, **options):
        return jev.order_impact(self.rows, root=self.repo, files=self.files, env=self.env,
                                keychain=RecordingStore(), **options)

    def test_the_server_sees_paths_lines_and_fixed_words_only(self) -> None:
        server = self.serve(self.answers)
        ordered, status = self.order()
        self.assertEqual(status, "used")
        self.assertEqual([item.where for item in ordered],
                         ["Makefile:3", "src/b.py:7", "docs/a.md:4", "src/d.py", "tests/test_e.py", "db/001.sql:9"])
        (request,) = server.requests
        body = request["body"]
        self.assertEqual(sorted(body), ["model", "questions", "state"])
        self.assertEqual(body["model"], typesafe.DEFAULT_MODEL)
        self.assertEqual(body["state"], {"task_kind": "impact", "query": jev.IMPACT_QUERY, "candidates": [
            {"name": "docs/a.md:4", "description": "link lexical doc"},
            {"name": "src/b.py:7", "description": "link lexical code"},
            {"name": "tests/test_e.py", "description": "mention cochange test"},
            {"name": "src/d.py", "description": "mention graph other"}]})
        self.assertEqual(sorted(body["questions"]), ["c0", "c1", "c2", "c3"])
        allowed = set("link mention lexical wiki graph cochange other".split()) | set(jev.IMPACT_KINDS)
        for candidate in body["state"]["candidates"]:
            self.assertIn(candidate["name"].split(":")[0], self.files)
            self.assertLessEqual(set(candidate["description"].split()), allowed)
        text = json.dumps(body)
        private = [*self.CANARIES, str(self.repo), self.repo.name, str(self.home), str(pathlib.Path.home()),
                   os.getcwd(), getpass.getuser(), socket.gethostname(), self.key, jev.project(self.repo).id]
        for value in private:
            if len(value) > 2:
                self.assertNotIn(value, text)
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {self.key}")
        self.assertNotIn(self.key, json.dumps({name: value for name, value in request["headers"].items()
                                              if name != "Authorization"}))

    def test_http_answers_that_fall_back(self) -> None:
        echo = "private body " + self.key
        cases = [(401, "unused(error:unauthorized)"), (429, "unused(error:rate-limited)"),
                 (529, "unused(error:overloaded)"), (500, "unused(error:http-500)"), (400, "unused(error:http-400)"),
                 (302, "unused(error:http-302)")]
        for code, status in cases:
            with self.subTest(code=code):
                server = self.serve(lambda path, body, code=code: (
                    code, {"error": echo}, 0, {"Location": "http://127.0.0.1:9/elsewhere"} if code == 302 else {}))
                result = self.order()
                self.assertEqual(result, (self.rows, status))
                self.assertEqual(len(server.requests), 1, "one request, never a retry or a redirect")
        for label, payload in (("not JSON", b"<html>"), ("no answers", {}), ("a missing answer", {"answers": {}}),
                               ("out of range", {"answers": {f"c{i}": {"type": "noul", "noul": 2} for i in range(4)}})):
            with self.subTest(answer=label):
                self.serve(lambda path, body, payload=payload: (200, payload, 0, {}))
                self.assertEqual(self.order(), (self.rows, "unused(error:bad-response)"))

    def test_a_slow_server_times_out_within_the_lowered_bound(self) -> None:
        server = FakeServer(self, lambda path, body: (200, {"answers": {}}, 1.0, {}))
        write_config(self.repo, url=server.url(), extra="timeout_seconds = 0.2")
        self.approve("impact")
        self.assertEqual(self.order(), (self.rows, "unused(error:timeout)"))


class ImpactCliTest(unittest.TestCase):
    """`oah impact` in a real process: opt-in, approval on the device, and the flags that keep the old order."""

    def setUp(self) -> None:
        self.repo = fixture_repo(self)
        self.home = make_tmp_dir(self) / "device"
        self.key = fake_key()
        self.env = {"OAH_HOME": str(self.home), SHARED: self.key}
        self.server = FakeServer(self, self.respond)
        write_config(self.repo, url=self.server.url())
        git_commit(self.repo, {})

    def respond(self, path, body):
        names = [item["name"] for item in body["state"]["candidates"]]
        answers = {f"c{index}": {"type": "noul", "noul": (index + 1) / (len(names) + 1)} for index in range(len(names))}
        return 200, {"model": "jev-test", "answers": answers}, 0, {}

    def impact(self, *args: str) -> list[str]:
        result = run_impact(self.repo, "src/app.py", "tools/check.py", "docs", *args, env_extra=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def rows(self, lines: list[str]) -> list[str]:
        return [line for line in lines if line.split(" ")[0] in ("pin", "exec", "link", "mention")]

    def line(self, lines: list[str], start: str) -> str:
        return next(line for line in lines if line.startswith(start))

    def test_opt_in_and_approved_reorders_only_weak_rows(self) -> None:
        plain = self.impact("--no-rerank")
        self.assertEqual(self.line(plain, "rerank="), "rerank=unused(disabled-by-brief)")
        waiting = self.impact()
        self.assertEqual(self.line(waiting, "rerank="), "rerank=unused(no-consent)")
        self.assertEqual(self.rows(waiting), self.rows(plain))
        self.assertEqual(self.server.requests, [], "nothing is sent before the approval on this device")
        preview = jev.prepare(self.repo, env=self.env)
        self.assertEqual(jev.enable(self.repo, token=preview["token"], env=self.env)["status"], "enabled")
        used = self.impact()
        self.assertEqual(self.line(used, "rerank="), "rerank=used")
        self.assertEqual(len(self.server.requests), 1)
        self.assertCountEqual(self.rows(used), self.rows(plain))
        self.assertNotEqual(self.rows(used), self.rows(plain), "the weak rows were re-ordered")
        strong = [line for line in self.rows(plain) if line.split(" ")[0] in ("pin", "exec")]
        self.assertEqual(self.rows(used)[:len(strong)], strong)
        self.assertEqual([line.split(" ")[0] for line in self.rows(used)], [line.split(" ")[0] for line in self.rows(plain)])
        for start in ("db-impact:", "total=", "maps:", "gates:"):
            self.assertEqual(self.line(used, start), self.line(plain, start))
        sent = self.server.requests[0]["body"]["state"]
        self.assertEqual((sent["task_kind"], sent["query"]), ("impact", jev.IMPACT_QUERY))
        text = json.dumps(self.server.requests[0]["body"])
        for private in ("changed together", str(self.repo), self.repo.name, str(self.home)):
            self.assertNotIn(private, text)
        tracked = subprocess.run(["git", "-C", str(self.repo), "ls-files"], check=True, capture_output=True,
                                 text=True).stdout.split()
        self.assertLessEqual({item["name"].split(":")[0] for item in sent["candidates"]}, set(tracked))
        for item in sent["candidates"]:
            self.assertRegex(item["description"], r"\A(link|mention) (lexical|wiki|graph|cochange) [a-z]+\Z")

    def test_flags_and_json_keep_their_contracts(self) -> None:
        preview = jev.prepare(self.repo, env=self.env)
        jev.enable(self.repo, token=preview["token"], env=self.env)
        self.assertEqual(self.line(self.impact("--offline"), "rerank="), "rerank=unused(offline)")
        self.assertEqual(self.line(self.impact("--strong-only"), "rerank="), "rerank=unused(no-candidates)")
        self.assertEqual(self.line(self.impact("--no-rerank"), "rerank="), "rerank=unused(disabled-by-brief)")
        self.assertEqual(self.server.requests, [])
        plain = json.loads("\n".join(self.impact("--json", "--no-rerank")))
        used = json.loads("\n".join(self.impact("--json")))
        self.assertEqual(used["rerank"], "used")
        self.assertEqual(sorted(used), sorted(plain))
        for name in ("db_impact", "total", "shown", "overflow", "targets", "gates", "scope", "fallback"):
            self.assertEqual(used[name], plain[name], name)
        canonical = lambda rows: sorted(json.dumps(item, sort_keys=True) for item in rows)
        self.assertEqual(canonical(used["rows"]), canonical(plain["rows"]))
        self.assertEqual([item for item in used["rows"] if item["layer"] == "db"],
                         [item for item in plain["rows"] if item["layer"] == "db"])
        jev.disable(self.repo, env=self.env)
        self.assertEqual(self.line(self.impact(), "rerank="), "rerank=unused(consent-revoked)")
        self.assertEqual(len(self.server.requests), 1)

    def test_the_impact_switch_alone_sends_nothing(self) -> None:
        (self.repo / ".oah" / "config.toml").write_text("[impact]\nrerank = true\n", encoding="utf-8")
        self.assertEqual(self.line(self.impact(), "rerank="), "rerank=unused(not-configured)")
        write_config(self.repo, url=self.server.url(), impact=False)
        self.assertEqual(self.line(self.impact(), "rerank="), "rerank=unused(not-enabled)")
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
