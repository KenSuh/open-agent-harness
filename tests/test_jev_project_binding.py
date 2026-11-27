# Jev 프로젝트 결속 테스트 — 기기 동의와 차단이 호출자가 이름 붙인 프로젝트에만 적용되고 현재 폴더가 대신하지 않는지, 동의한 요청이 모든 진입점에서 동의의 한도를 지키는지, 제품 코드의 모든 호출이 프로젝트를 넘기는지 본다.
from __future__ import annotations

import ast
import contextlib
import inspect
import io
import pathlib
import unittest
from unittest import mock

from oah import call, project_tools_jev as jev, rerank, routing, secrets, skillsel
from oah.design.sources import assessment
from oah.discovery import gateway
from oah.rerank import judge, typesafe
from tests.helpers import make_git_repo, make_tmp_dir
from tests.test_design_judgement import POLICY, FakeJudge, JudgementCase
from tests.test_project_tools_jev import ELSEWHERE, OTHER_NAME, SHARED, Verdict, row, settings, write_config
from tests.test_rerank import JUDGE_PAIRS, JUDGE_REQUEST, candidates
from tests.test_secrets_store import RecordingStore, fake_key

AXES = {"kind": "implementation", "intensity": "medium", "reasoning": "high", "risk": "normal", "tier": "high"}
WIDE = {"timeout_seconds": 999.0, "max_candidates": 99}  # more than an approval allows
JUDGING = {"enabled": True, "send_design_text": False, "send_task_text": False}
REFUSED = "unused(no key)"


class Recording(FakeJudge):
    """Registered as typesafe: it answers every kind of request and records the bounds and the key of each."""

    name = "typesafe"

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, object, object]] = []
        self.keys: list[str] = []
        self.sizes: list[int] = []

    def key_name(self, config) -> str:
        return typesafe.TypeSafeProvider().key_name(config)

    def note(self, what: str, key, config, size: int) -> None:
        self.seen.append((what, config.get("timeout_seconds"), config.get("max_candidates")))
        self.keys.append(key.reveal())
        self.sizes.append(size)

    def score(self, kind, query, items, *, key, config):
        self.note("score", key, config, len(items))
        return [0.5 for _item in items]

    def advise(self, axes, work, *, key, config):
        self.note("advise", key, config, 1)
        return "medium", 0.9

    def choose_model(self, request, pairs, *, key, config):
        self.note("choose_model", key, config, len(pairs))
        return (*pairs[0], 0.9)

    def judge(self, state, questions, *, key, config):
        self.note("judge", key, config, len(questions))
        return super().judge(state, questions, key=key, config=config)


class DeviceStore(secrets.MacKeychain):
    """The device key store as the gate sees it (an instance of the real class), answering from memory."""

    def __init__(self, value: str | None = None) -> None:  # the real set-up is skipped, so no program is ever run
        self.value = value
        self.calls: list[str] = []

    def probe(self, name: str) -> tuple[str, str | None]:
        self.calls.append(name)
        return (secrets.PRESENT, self.value) if self.value else (secrets.MISSING, None)

    def lookup(self, name: str) -> str | None:
        self.calls.append(name)
        return self.value


def ask_order(provider: Recording, **gate) -> str:
    return rerank.rerank("skills", candidates("a", "b"), providers={"typesafe": provider}, **gate).status


def ask_difficulty(provider: Recording, **gate) -> str:
    return rerank.advise_difficulty(AXES, "add a flag", providers={"typesafe": provider}, **gate).status


def ask_model(provider: Recording, **gate) -> str:
    return rerank.judge_model(JUDGE_REQUEST, JUDGE_PAIRS, providers={"typesafe": provider}, **gate).status


# Each entry point that asks the provider, by the provider call it ends in. `judge` has its own case below.
ENTRIES = {"score": ask_order, "advise": ask_difficulty, "choose_model": ask_model}


def two_projects(test: unittest.TestCase) -> None:
    """Two repositories on one device. The process stands in `here`; `there` is another project a caller names."""
    test.home = make_tmp_dir(test) / "device"
    test.key = fake_key()
    folder = str(make_tmp_dir(test))
    test.env = {"OAH_HOME": str(test.home), "HOME": folder, "USERPROFILE": folder, SHARED: test.key}
    test.unkeyed = {name: value for name, value in test.env.items() if name != SHARED}
    test.here, test.there = make_git_repo(test), make_git_repo(test)
    for repo in (test.here, test.there):
        write_config(repo)
    refuse = AssertionError("the real device key store is never read in these tests")
    test.enterContext(mock.patch.object(secrets, "system_keychain", side_effect=refuse))
    test.enterContext(contextlib.chdir(test.here))


def approve(test: unittest.TestCase, repo: pathlib.Path) -> None:
    preview = jev.prepare(repo, env=test.env)
    test.assertEqual(jev.enable(repo, token=preview["token"], env=test.env)["status"], "enabled")


class BindingTest(unittest.TestCase):
    """An approval and a switch-off belong to the project a caller names, wherever the process stands."""

    def setUp(self) -> None:
        two_projects(self)

    def test_a_project_switched_off_stays_off_wherever_the_process_stands(self) -> None:
        self.assertEqual(jev.disable(self.there, env=self.env)["status"], "disabled")
        for standing in (self.here, self.there):
            with contextlib.chdir(standing):
                for name, ask in ENTRIES.items():
                    with self.subTest(entry=name, standing=standing.parent.name):
                        provider = Recording()
                        self.assertEqual(ask(provider, config=settings(), env=self.env, root=self.there), REFUSED)
                        self.assertEqual(provider.seen, [], "nothing is sent for the project that was switched off")
                        self.assertEqual(ask(provider, config=settings(), env=self.env, root=self.here), "used")
                        self.assertEqual([what for what, *_bounds in provider.seen], [name])

    def test_task_advice_is_reachable_only_for_a_project_that_is_not_switched_off(self) -> None:
        table = settings(judgement=dict(JUDGING, send_task_text=True))

        def reachable(root: pathlib.Path | None) -> bool:
            return gateway.reachable("dispatch", task_advice=True, config=table, env=self.env, root=root)

        self.assertEqual((reachable(self.here), reachable(self.there)), (True, True))
        jev.disable(self.there, env=self.env)
        self.assertEqual((reachable(self.here), reachable(self.there)), (True, False))
        with contextlib.chdir(self.there):
            self.assertEqual((reachable(self.here), reachable(self.there)), (True, False))

    def test_an_impact_approval_belongs_to_the_project_it_was_given_for(self) -> None:
        approve(self, self.here)
        rows = [row("link", "lexical", "docs/a.md", 4, "doc"), row("link", "lexical", "src/b.py", 7)]
        files = {item.source: item.kind for item in rows}
        for standing in (self.here, self.there):
            with contextlib.chdir(standing), self.subTest(standing=standing.parent.name):
                provider, store = Recording(), DeviceStore(fake_key())
                gate = {"files": files, "providers": {"typesafe": provider}}
                self.assertEqual(jev.order_impact(rows, root=self.there, env=self.env, **gate),
                                 (rows, "unused(no-consent)"))
                self.assertEqual(jev.order_impact(rows, root=self.there, env=self.unkeyed, keychain=store, **gate),
                                 (rows, "unused(no-consent)"))
                self.assertEqual((provider.seen, store.calls), ([], []), "no request and no key lookup")
                self.assertEqual(jev.order_impact(rows, root=self.here, env=self.env, **gate)[1], "used")
                self.assertEqual(jev.order_impact(rows, root=self.here, env=self.unkeyed, keychain=store, **gate)[1],
                                 "used")
                self.assertEqual(provider.keys, [self.key, store.value])
                self.assertEqual(store.calls, [SHARED], "the stored key is read for the approved project alone")

    def test_the_gate_reads_the_approval_of_the_named_project(self) -> None:
        approve(self, self.here)
        for standing in (self.here, self.there):
            with contextlib.chdir(standing), self.subTest(standing=standing.parent.name):
                given = rerank.admit(settings(), env=self.env, scopes=("impact",), root=self.here,
                                     providers={"typesafe": Recording()})
                self.assertEqual((given.status, given.consented), ("used", True))
                other = rerank.admit(settings(), env=self.env, scopes=("impact",), root=self.there,
                                     providers={"typesafe": Recording()})
                self.assertEqual((other.provider, other.key, other.status, other.reason),
                                 (None, None, REFUSED, rerank.NO_CONSENT))

    def test_without_a_named_project_nothing_on_this_device_is_read(self) -> None:
        approve(self, self.here)  # the folder the process stands in is approved, and that is not borrowed
        asked: list[str] = []

        class Watched(dict):
            def get(self, name, default=None):
                asked.append(name)
                return super().get(name, default)

        refuse = mock.Mock(side_effect=AssertionError("no project was named, so no device state is read"))
        store = DeviceStore(fake_key())
        elsewhere = settings(url=ELSEWHERE, key_name=OTHER_NAME)
        cases = {
            "Impact ordering": (settings(), {"env": Watched(self.env), "scopes": ("impact",)}),
            "the key stored on this device": (settings(), {"env": Watched(self.unkeyed), "keychain": store,
                                                           "scopes": ("skills",)}),
            "another destination": (elsewhere, {"env": Watched(self.env, **{OTHER_NAME: fake_key()}),
                                                "scopes": ("skills",)}),
        }
        with mock.patch.object(jev, "device_approval", refuse), mock.patch.object(jev, "project", refuse):
            for label, (table, gate) in cases.items():
                with self.subTest(use=label):
                    result = rerank.admit(table, providers={"typesafe": Recording()}, **gate)
                    self.assertEqual((result.provider, result.key, result.status, result.reason),
                                     (None, None, REFUSED, rerank.NO_CONSENT))
            for name, ask in ENTRIES.items():
                with self.subTest(entry=name):
                    provider = Recording()
                    self.assertEqual(ask(provider, config=settings(), env=Watched(self.unkeyed), keychain=store),
                                     REFUSED)
                    self.assertEqual(provider.seen, [])
            kept = rerank.admit(settings(), env=Watched(self.env), scopes=("skills",),
                                providers={"typesafe": Recording()})
            self.assertEqual((kept.status, kept.key.reveal(), kept.consented), ("used", self.key, False),
                             "an environment key keeps its behaviour")
        refuse.assert_not_called()
        self.assertEqual(store.calls, [], "the key stored on this device is not looked up")
        self.assertEqual(set(asked), {SHARED, OTHER_NAME}, "the environment is read for the key name alone")
        self.assertEqual(jev.DeviceApproval(None, self.env).verdict(("impact",), url=typesafe.OFFICIAL_URL,
                                                                    key_name=SHARED, model=typesafe.DEFAULT_MODEL),
                         rerank.NO_CONSENT)
        with self.assertRaises(ValueError):
            jev.project(None)

    def test_the_key_stored_on_this_device_is_never_used_outside_impact(self) -> None:
        approve(self, self.here)
        for name, ask in ENTRIES.items():
            with self.subTest(entry=name):
                store, provider = DeviceStore(fake_key()), Recording()
                self.assertEqual(ask(provider, config=settings(), env=self.unkeyed, keychain=store, root=self.here),
                                 REFUSED)
                self.assertEqual((store.calls, provider.seen), ([], []))


class ProductPathTest(unittest.TestCase):
    """The same through the paths `oah call` takes: each one asks for the project it was given."""

    def setUp(self) -> None:
        two_projects(self)
        self.provider = Recording()
        self.enterContext(mock.patch.dict(rerank.PROVIDERS, {"typesafe": self.provider}))
        self.assertEqual(jev.disable(self.there, env=self.env)["status"], "disabled")
        self.axes = routing.Axes("implementation", "low", "medium", "normal")

    def difficulty(self, root: pathlib.Path) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            return call.judge_difficulty(self.axes, section=None, request_text="Add a flag to the report.",
                                         scope=("src/a.py",), rule_tier="medium", floor_tier="low", root=root,
                                         state_dir=make_tmp_dir(self), env=self.env).status

    def advice(self, root: pathlib.Path) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            return call.advise(self.axes, ("src/a.py",), root=root, state_dir=make_tmp_dir(self), env=self.env)

    def skills(self, root: pathlib.Path) -> str:
        return skillsel.choose(root, kind="docs", stage="initial", risk="normal", scope=("README.md",),
                               state_dir=None, env=self.env)[1]

    def test_difficulty_and_skill_order_follow_the_given_project(self) -> None:
        for standing in (self.here, self.there):
            with contextlib.chdir(standing), self.subTest(standing=standing.parent.name):
                self.provider.seen.clear()
                self.assertEqual((self.difficulty(self.there), self.advice(self.there), self.skills(self.there)),
                                 (REFUSED, REFUSED, REFUSED))
                self.assertEqual(self.provider.seen, [], "nothing is sent for the project that was switched off")
                self.assertEqual((self.difficulty(self.here), self.advice(self.here)), ("used", "used tier=medium"))
                self.assertIn(self.skills(self.here), ("used", "unused(no-candidates)"))
                self.assertEqual([what for what, *_bounds in self.provider.seen][:2], ["advise", "advise"])


class JudgementBindingTest(JudgementCase):
    """`judge` follows the named project too, and an approved judgement keeps the approval's bounds."""

    def setUp(self) -> None:
        super().setUp()
        two_projects(self)

    def ask(self, provider: Recording, **gate) -> str:
        request = judge.Request("source-change-impact", "source-change-impact/v2",
                                (("release_note_excerpt", self.notes()),))
        return judge.judge(request, policy=POLICY, sources=self.store, providers={"typesafe": provider}, **gate).status

    def test_a_judgement_follows_the_named_project(self) -> None:
        jev.disable(self.there, env=self.env)
        table = settings(judgement=JUDGING)
        for standing in (self.here, self.there):
            with contextlib.chdir(standing), self.subTest(standing=standing.parent.name):
                provider = Recording()
                self.assertEqual(self.ask(provider, config=table, env=self.env, root=self.there), REFUSED)
                self.assertEqual(provider.seen, [])
                self.assertEqual(self.ask(provider, config=table, env=self.env, root=self.here), "used")
                self.assertEqual([what for what, *_bounds in provider.seen], ["judge"])
        store, provider = DeviceStore(fake_key()), Recording()
        self.assertEqual(self.ask(provider, config=table, env=self.unkeyed, keychain=store), REFUSED)
        self.assertEqual((store.calls, provider.seen), ([], []), "no project named: the stored key is not used")

    def test_an_approved_judgement_keeps_the_bounds_of_the_approval(self) -> None:
        provider = Recording()
        consented = {"env": {}, "keychain": RecordingStore({SHARED: fake_key()}), "approval": Verdict(rerank.GRANTED)}
        self.assertEqual(self.ask(provider, config=settings(judgement=JUDGING, **WIDE), **consented), "used")
        self.assertEqual(provider.seen, [("judge", rerank.TIMEOUT_LIMIT, rerank.CANDIDATE_LIMIT)])
        provider = Recording()
        self.ask(provider, config=settings(judgement=JUDGING, **WIDE), env={SHARED: fake_key()},
                 keychain=RecordingStore())
        self.assertEqual(provider.seen, [("judge", 999.0, 99)], "an environment key keeps the configuration's values")


class BoundsTest(unittest.TestCase):
    """A use the approval admitted keeps the approval's bounds at every entry point that asks the provider."""

    def consented(self) -> dict:
        return {"env": {}, "keychain": RecordingStore({SHARED: fake_key()}), "approval": Verdict(rerank.GRANTED)}

    def test_an_approved_use_keeps_the_bounds_of_the_approval(self) -> None:
        for name, ask in ENTRIES.items():
            with self.subTest(entry=name):
                provider = Recording()
                self.assertEqual(ask(provider, config=settings(**WIDE), **self.consented()), "used")
                self.assertEqual(provider.seen, [(name, rerank.TIMEOUT_LIMIT, rerank.CANDIDATE_LIMIT)])
                provider = Recording()
                self.assertEqual(ask(provider, config=settings(timeout_seconds=2.5, max_candidates=7),
                                     **self.consented()), "used")
                self.assertEqual(provider.seen, [(name, 2.5, 7)], "the configuration can still lower them")

    def test_an_environment_key_without_an_approval_keeps_the_configuration(self) -> None:
        for name, ask in ENTRIES.items():
            with self.subTest(entry=name):
                provider = Recording()
                self.assertEqual(ask(provider, config=settings(**WIDE), env={SHARED: fake_key()},
                                     keychain=RecordingStore()), "used")
                self.assertEqual(provider.seen, [(name, 999.0, 99)])

    def test_the_candidate_bound_decides_what_an_approved_request_carries(self) -> None:
        over = rerank.CANDIDATE_LIMIT + 1
        provider = Recording()
        judged = rerank.judge_model(JUDGE_REQUEST, [(f"m{index}", "high") for index in range(over)],
                                    config=settings(**WIDE), providers={"typesafe": provider}, **self.consented())
        self.assertEqual((judged.status, provider.seen), ("unused(too-many-candidates)", []))
        result = rerank.rerank("skills", candidates(*(f"s{index}" for index in range(over))), config=settings(**WIDE),
                               providers={"typesafe": provider}, **self.consented())
        self.assertEqual((result.status, provider.sizes, len(result.order)), ("used", [rerank.CANDIDATE_LIMIT], over))


SOURCE = pathlib.Path(rerank.__file__).resolve().parents[1]  # src/oah
CONSENT_FILES = ("project_tools_jev.py", "rerank/__init__.py", "rerank/judge.py", "discovery/gateway.py")
# A call that reaches the TypeSafe gate, by the name it is made with. None is a bare name; "*" is any receiver.
# A bare `admit` or `_gate` is the gate only inside the rerank package (other modules have their own `admit`).
GATE_CALLS = {"judge_model": ("*",), "advise_difficulty": ("*",), "rerank_skills": ("*",), "_gate": ("*",),
              "rerank": ("rerank",), "judge": ("judge",), "reachable": ("gateway", None),
              "assess": ("assessment",), "admit": ("rerank",)}
KNOWN_SITES = {("call.py", "judge_model"), ("call.py", "advise_difficulty"), ("call.py", "judge"),
               ("call.py", "reachable"), ("call.py", "rerank_skills"), ("skillsel.py", "rerank"),
               ("skillsel.py", "rerank_skills"), ("design_cli.py", "judge"), ("skills_cli.py", "judge"),
               ("sources_cli.py", "assess"), ("design/sources/assessment.py", "judge"),
               ("project_tools_jev.py", "rerank"), ("rerank/__init__.py", "admit"), ("rerank/judge.py", "admit"),
               ("discovery/gateway.py", "_gate")}


def gate_calls(path: pathlib.Path) -> list[tuple[str, ast.Call]]:
    """(name, call) for every call in the file that reaches the gate."""
    inside = path.parent.name == "rerank"
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute):
            name, owner = target.attr, target.value.id if isinstance(target.value, ast.Name) else "?"
        elif isinstance(target, ast.Name):
            name, owner = target.id, None
        else:
            continue
        owners = GATE_CALLS.get(name, ())
        if "*" in owners or owner in owners or (inside and owner is None and name in ("admit", "_gate")):
            found.append((name, node))
    return found


class ProductCallsTest(unittest.TestCase):
    """The class of mistake behind this file: a caller that reads one project's settings and names no project."""

    def test_every_call_that_reaches_the_gate_names_the_project(self) -> None:
        sites, unnamed = set(), []
        for path in sorted(SOURCE.rglob("*.py")):
            relative = path.relative_to(SOURCE).as_posix()
            for name, node in gate_calls(path):
                words = {word.arg: word.value for word in node.keywords}
                if name == "reachable" and "config" not in words:
                    continue  # no settings, so no gate: the request boundary asks whether anything is managed
                sites.add((relative, name))
                given = words.get("root")
                if given is None or (isinstance(given, ast.Constant) and given.value is None):
                    unnamed.append(f"{relative}:{node.lineno} {name}")
        self.assertEqual(unnamed, [], "pass root= (the folder the settings were read from) to the gate")
        self.assertEqual(KNOWN_SITES - sites, set(), "the sweep no longer finds a call it must see")

    def test_every_entry_point_takes_the_project(self) -> None:
        for entry in (rerank.admit, rerank.rerank, rerank.advise_difficulty, rerank.judge_model, judge.judge,
                      gateway.reachable, skillsel.rerank_skills, assessment.assess, jev.order_impact):
            with self.subTest(entry=entry.__qualname__):
                self.assertIn("root", inspect.signature(entry).parameters)
        for entry in (jev.project, jev.device_approval, jev.DeviceApproval, jev.order_impact):
            with self.subTest(entry=entry.__qualname__, rule="no default project"):
                self.assertIs(inspect.signature(entry).parameters["root"].default, inspect.Parameter.empty)

    def test_the_consent_code_never_asks_for_the_current_folder(self) -> None:
        for relative in CONSENT_FILES:
            tree = ast.parse((SOURCE / relative).read_text(encoding="utf-8"))
            names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            with self.subTest(file=relative):
                self.assertEqual(names & {"cwd", "getcwd", "getcwdb", "curdir"}, set())


if __name__ == "__main__":
    unittest.main()
