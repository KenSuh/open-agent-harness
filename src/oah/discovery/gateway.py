# E 요청 경계 — 같은 준비 신원을 전송 직전에 확인하고 요청 및 수신 사용량의 닫힌 근거를 남긴다.
from __future__ import annotations

import dataclasses
import pathlib
from typing import Callable, Mapping, ParamSpec, Protocol, Sequence, TYPE_CHECKING, TypeVar

from oah.discovery import attempts, preparation
from oah.discovery.store import digest
from oah.rerank.base import Candidate
from oah.secrets import Keychain, Secret

if TYPE_CHECKING:
    from oah.design.sources.adapters import Transport
    from oah.runs import Run
    from oah.skillpack import Admitted

T = TypeVar("T")
P = ParamSpec("P")


def reachable(operation: str, *, managed: bool = False, discovery: bool = False, task_advice: bool = False,
              config: Mapping[str, preparation.Json] | None = None, env: Mapping[str, str] | None = None,
              keychain: Keychain | None = None) -> bool:
    from oah import rerank, skillpack

    if operation in skillpack.TASKLESS or managed or discovery:
        return True
    if not task_advice or config is None:
        return False
    settings = config.get("judgement")
    if not isinstance(settings, Mapping) or settings.get("enabled") is not True \
            or settings.get("send_task_text") is not True:
        return False
    provider, _key, _status = rerank._gate(config, brief_disabled=False, env=env, keychain=keychain, providers=None)
    return provider is not None and callable(getattr(provider, "judge", None))


def send(admitted: Admitted, kind: attempts.Kind, input_digest: str, transport: Callable[[], T], *,
         state: pathlib.Path | None = None, query: bool = False, known: bool = True) -> T:
    from oah import skillpack

    live = skillpack.require(admitted)
    assert live.prepared is not None
    record = live.prepared.record()
    if live.operation.kind == "dispatch":
        try:
            checked = preparation.parse(record)
            facts = checked["facts"]
            if not isinstance(facts, dict) or facts["route"] not in ("process", "orca", "native") \
                    or not all(isinstance(facts[key], dict) for key in ("inventory", "target")) \
                    or not checked["profile"]:
                raise ValueError("dispatch requests need the complete target, inventory and profile")
        except ValueError as exc:
            raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "admission", str(exc)) from None
    if not reachable(live.operation.kind, managed=live.operation.kind == "dispatch"):
        raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "admission", "no E request is reachable")
    if state is None and attempts.CURRENT.get() is None:
        raise skillpack.PackRefused(skillpack.NOT_DELIVERABLE_CODE, "admission",
                                   "a retained request-summary location is required; nothing was sent")
    if query:
        live.reserve_query()
    with attempts.request(kind, input_digest, state=state, known=known,
                          preparation=str(record["digest"])) as fact:
        try:
            live.require()
        except skillpack.PackRefused:
            if fact is not None:
                fact["requests"] = 0
            raise
        if fact is not None:
            fact["state"] = "started"
            fact["requests"] = 1 if known else None
        result = transport()
        if fact is not None:
            fact["outcome_digest"] = digest({"status": "returned"})
        return result


def collector_transport(original: Transport, admitted: Admitted, *, state: pathlib.Path) -> Transport:
    """Reserve one bounded source check; keep an admitted attempt around each of its I/O operations."""
    from oah import skillpack
    from oah.design.sources.adapters import IO_FIELDS

    skillpack.require(admitted).reserve_query()

    def wrap(name: str, operation: Callable[P, T]) -> Callable[P, T]:
        def request(*args: P.args, **options: P.kwargs) -> T:
            return send(admitted, "skill-review", digest({"operation": name,
                        "target": str(args[0]) if args else ""}),
                        lambda: operation(*args, **options), state=state)

        return request

    return dataclasses.replace(original, **{name: wrap(name, getattr(original, name)) for name in IO_FIELDS})


class ScoreProvider(Protocol):
    name: str

    def key_name(self, config: Mapping[str, preparation.Json]) -> str: ...

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: Secret,
              config: Mapping[str, preparation.Json]) -> Sequence[float]: ...


@dataclasses.dataclass(frozen=True, slots=True)
class SkillOrderProvider:
    provider: ScoreProvider
    admitted: Admitted
    state: pathlib.Path | None

    @property
    def name(self) -> str:
        return self.provider.name

    def key_name(self, config: Mapping[str, preparation.Json]) -> str:
        return self.provider.key_name(config)

    def score(self, kind: str, query: str, candidates: Sequence[Candidate], *, key: Secret,
              config: Mapping[str, preparation.Json]) -> Sequence[float]:
        return send(self.admitted, "skill-order", digest({"kind": kind, "query": query,
                    "items": [dataclasses.asdict(item) for item in candidates]}),
                    lambda: self.provider.score(kind, query, candidates, key=key, config=config), state=self.state)


def receive(run: Run, observation: Mapping[str, preparation.Json]) -> str | None:
    from oah.atomic import write_json_atomic
    from oah.capabilities import inventory

    try:
        write_json_atomic(run.file_path(inventory.RECEIVING_NAME), observation)
    except OSError as exc:
        return type(exc).__name__
    return None
