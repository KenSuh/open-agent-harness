# Jev 프로젝트 도구 백엔드 — 기기 키 저장소에 키를 넣고 빼며, 프로젝트별 동의를 기기 상태에 두고, 고정 연결 점검과 Impact 약한 행 순서 조정을 맡는다.
"""Jev for one project on this device: the stored key, the user's approval, a connection check, Impact ordering.

Every call returns plain data. None prints, prompts or reads a terminal, and an ordinary refusal is a `status`
word in the result, never an exception that carries a value.

    status(root)                        what this device holds for the project            (no key lookup)
    prepare(root, scopes=("impact",))   the approval preview and its token                (no key lookup)
    enable(root, token=...)             record the approval that preview described        (no key lookup)
    disable(root)                       switch the project off on this device             (no key lookup)
    key_state()                         environment set or not; device store present, missing, locked, denied
    store_key(source)                   put the key in the OS key store: a Secret, or a pipe handed over
    delete_key()                        remove it from the OS key store
    prepare_test()                      what the connection check sends, and its token    (no key lookup)
    run_test(ack=...)                   one fixed request to the official address
    order_impact(rows, root=..., files=...)   opt-in order of the weak Impact rows already shown
    dispatch(argv, stdin=...)           all of the above but the ordering, for a packaged entry

Approval. It lives on this device, in `<OAH_HOME, else the home folder's .oah>/state/jev/projects/<id>.json`,
never in the project: a repository can ship configuration, and configuration alone sends nothing new. One record
holds one project (the canonical path of its git common directory, so its worktrees share it, or the folder
outside git), the exact destination the user saw (address, key name, model) and, per scope, the digest of what
that scope sends. An approval covers Impact ordering alone (`POLICIES`): no other Jev use is offered the key stored
on this device. The project is always the folder a caller names; the current folder is never a stand-in for it.
`oah.rerank.admit` asks `DeviceApproval.verdict` before any key lookup:

- `granted`: enabled, the same destination, every asked scope approved with today's digest;
- `no-consent`: no record, no project named, or a scope no approval covers;
- `consent-revoked`: the user switched the project off here, which stops every key source;
- `consent-stale`: the destination or what a scope sends changed since the approval;
- `device-state`: the record cannot be trusted (unreadable, malformed, another owner, writable by others, a
  relative `OAH_HOME`, or a location inside the project). It stops every key source until the user fixes it.

The preview token binds project, destination and scopes; `enable` records only what a token computed again from
the current files matches, so a configuration that changed after the preview is refused (`preview-changed`).

Key. `TYPESAFE_API_KEY`, under the labels of `oah.secrets`. The environment variable keeps its documented
precedence. `store_key` takes the value in process or over a pipe (bounded, never a terminal, never echoed) and
writes it to the OS key store alone; no result, error or record holds it.

Connection check. `run_test` sends `typesafe.build_probe_request()`, the same bytes for every user, project and
machine, to `typesafe.OFFICIAL_URL` alone, once, for at most `typesafe.PROBE_TIMEOUT` seconds, without proxies
or redirects. The token of `prepare_test` is the opt-in and is checked before any key lookup. The result is a
fixed word, the key source and the answering model's name; never response text.

Impact ordering. `order_impact` never adds or removes a row. Pin and exec rows, gate rows and DB rows keep their
place, and a link row never moves below a mention row. Sent per row, and nothing else: `IMPACT_FIELDS`
(the repository-relative path and line, the binding, the layer, the file kind as one fixed word) under the fixed
labels `IMPACT_TASK` and `IMPACT_QUERY`. Never the row detail, the queried target, file contents or an absolute
path. At most `rerank.CANDIDATE_LIMIT` rows go in one request per run, waiting at most `rerank.TIMEOUT_LIMIT`
seconds; the configuration can lower those bounds and cannot raise them. Any refusal or failure returns the rows
as given with `unused(<reason>)`.

Packaged entry. `dispatch` is the one call a packaged program makes: a fixed command word, the project folder as
`--root`, and a token or the key as one line on a byte stream. It takes no rows; ordering stays inside `oah impact`.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import hmac
import json
import os
import pathlib
import re
import stat
from typing import Callable, Iterable, Mapping, NamedTuple, Sequence

from oah import rerank, secrets
from oah.atomic import write_json_atomic
from oah.config import ConfigError, load_config
from oah.paths import GitMissing, RepoInfo, run_git, state_dir
from oah.rerank import typesafe
from oah.rerank.base import INSECURE_URL, ProviderError
from oah.toolkit.checks import CONTENT_RULES

__all__ = ["COMMANDS", "IMPACT_FIELDS", "IMPACT_QUERY", "IMPACT_TASK", "POLICIES", "DeviceApproval", "delete_key",
           "device_approval", "disable", "dispatch", "enable", "key_state", "order_impact", "policy_digest",
           "prepare", "prepare_test", "run_test", "status", "store_key"]

HOME_ENV = "OAH_HOME"
SCHEMA = 1
RECORD_LIMIT = 64 * 1024  # bytes; a larger record is malformed
ENABLED, DISABLED, ABSENT = "enabled", "disabled", "none"
KEY_NAME = typesafe.DEFAULT_KEY_NAME
READY, PREVIEW_CHANGED, SCOPE, CONFIG_INVALID, ACK_REQUIRED, NO_KEY, INVALID = (
    "ready", "preview-changed", "scope", "config-invalid", "ack-required", "no-key", "invalid")
ENVIRONMENT, DEVICE = "environment", "device"

# What an Impact request holds for one row, and the fixed text around the rows.
IMPACT_FIELDS = ("path", "line", "binding", "layer", "kind")
IMPACT_TASK = "impact"
IMPACT_QUERY = "Which of these weakly bound rows should be read first when a change is reviewed?"
IMPACT_BINDINGS = ("link", "mention")
IMPACT_LAYERS = ("lexical", "wiki", "graph", "cochange")  # gate rows name no file; DB rows keep their digest order
IMPACT_KINDS = ("workflow", "migration", "test", "manifest", "config", "doc", "code")
IMPACT_OTHER_KIND = "other"  # a kind the project's own [impact] kinds table named
PATH_LIMIT = 200  # characters in one path
LINE_LIMIT = 10_000_000
_PATH_UNSAFE = re.compile(r"[\x00-\x1f\x7f\\:]")
_HEX = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"v1:([a-z,-]{1,200}):([0-9a-f]{64})")
_REASONS = (rerank.NO_CONSENT, rerank.REVOKED, rerank.STALE, rerank.DEVICE_STATE, rerank.DESTINATION,
            rerank.KEY_LOCKED, rerank.KEY_DENIED)


class Policy(NamedTuple):
    """What one scope sends. A change to `revision`, `sends`, `labels` or `bounds` asks the user again."""

    revision: int
    title: str
    sends: tuple[str, ...]
    labels: tuple[str, ...] = ()  # fixed product text that goes with every request
    never: tuple[str, ...] = ()
    bounds: tuple[tuple[str, int], ...] = ()


POLICIES: dict[str, Policy] = {
    "impact": Policy(
        1, "Impact row order", IMPACT_FIELDS,
        labels=(f"task kind: {IMPACT_TASK}", f"query: {IMPACT_QUERY}"),
        never=("the row detail", "the queried target", "file contents", "absolute paths"),
        bounds=(("rows in one request", rerank.CANDIDATE_LIMIT), ("requests in one run", 1),
                ("seconds waited", int(rerank.TIMEOUT_LIMIT)), ("characters in one path", PATH_LIMIT))),
}  # every scope an approval can cover. The other scopes of `rerank.SCOPES` have none, so they never get one.


def policy_digest(scope: str) -> str:
    """The digest an approval of `scope` records: it changes when what the scope sends changes."""
    policy = POLICIES[scope]
    text = json.dumps([scope, policy.revision, policy.sends, policy.labels, policy.bounds], separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Project(NamedTuple):
    id: str
    kind: str  # "git" or "folder"
    path: str  # the canonical git common directory, or the canonical folder
    common_dir: pathlib.Path | None
    owned: tuple[str, ...]  # canonical folders a repository could fill: the work tree and the git directory
    root: pathlib.Path  # where the project's configuration is read: the top of the work tree, or the folder


def project(root: pathlib.Path | str) -> Project:
    """The project the folder `root` belongs to: worktrees of one repository are one. A caller names the folder."""
    if root is None:
        raise ValueError("a Jev project is named by its folder; the current folder is not a stand-in")
    folder = pathlib.Path(root)
    common = top = None
    try:
        found = run_git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel")
        lines = found.stdout.splitlines() if found.returncode == 0 else []
        if len(lines) == 2 and all(os.path.isabs(line) for line in lines):
            common, top = pathlib.Path(lines[0]), pathlib.Path(lines[1])
    except (GitMissing, OSError):
        pass
    kind, anchor = ("git", common) if common is not None else ("folder", folder)
    canonical = _canonical(anchor)
    digest = hashlib.sha256("\0".join(("oah-jev-project", kind, canonical)).encode("utf-8", "surrogateescape"))
    owned = tuple(dict.fromkeys(_canonical(item) for item in (top, common, folder) if item is not None))
    return Project(digest.hexdigest(), kind, canonical, common, owned, folder if top is None else top)


def _canonical(path: pathlib.Path | str) -> str:
    return os.path.normcase(os.path.realpath(path))


class Device(NamedTuple):
    folder: pathlib.Path | None  # <user folder>/state/jev; None when this device has no user folder
    trusted: bool  # False for a relative OAH_HOME and for a location inside the project


def device(found: Project, env: Mapping[str, str] | None = None) -> Device:
    """Where approvals live for the user who runs this: never a place the project's own files could fill."""
    environment = os.environ if env is None else env
    base = environment.get(HOME_ENV)
    if not base:
        home = environment.get("USERPROFILE" if os.name == "nt" else "HOME")
        if not home:
            return Device(None, True)
        base = os.path.join(home, ".oah")
    if not os.path.isabs(base):
        return Device(None, False)
    folder = _canonical(os.path.join(base, "state", "jev"))
    for owned in found.owned:
        try:
            inside = os.path.commonpath((folder, owned)) == owned
        except ValueError:  # another drive
            inside = False
        if inside:
            return Device(None, False)
    return Device(pathlib.Path(folder), True)


def _record_path(place: Device, found: Project) -> pathlib.Path:
    return place.folder / "projects" / f"{found.id}.json"


def _read_record(path: pathlib.Path, found: Project) -> tuple[str, dict | None]:
    """(ABSENT, None) when there is no record, (state, record) for a good one, else ("", None)."""
    handle = None
    try:
        try:
            handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            return ABSENT, None
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_size > RECORD_LIMIT:
            return "", None
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            return "", None
        data = b""
        while len(data) <= RECORD_LIMIT:
            chunk = os.read(handle, RECORD_LIMIT + 1 - len(data))
            if not chunk:
                break
            data += chunk
        record = json.loads(data.decode("utf-8")) if len(data) <= RECORD_LIMIT else None
    except (OSError, ValueError):
        return "", None
    finally:
        if handle is not None:
            os.close(handle)
    if not _valid_record(record, found):
        return "", None
    return record["state"], record


def _valid_record(record: object, found: Project) -> bool:
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return False
    if record.get("project") != {"id": found.id, "kind": found.kind, "path": found.path}:
        return False
    if record.get("state") == DISABLED:
        return True
    destination, scopes = record.get("destination"), record.get("scopes")
    return (record.get("state") == ENABLED
            and isinstance(destination, dict) and set(destination) == {"url", "key_name", "model"}
            and all(isinstance(value, str) and value for value in destination.values())
            and isinstance(scopes, dict) and bool(scopes)
            and all(scope in POLICIES and isinstance(digest, str) and _HEX.fullmatch(digest) is not None
                    for scope, digest in scopes.items()))


def _write_record(place: Device, found: Project, fields: Mapping[str, object]) -> bool:
    record = {"schema": SCHEMA, "project": {"id": found.id, "kind": found.kind, "path": found.path}, **fields,
              "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        for folder in (place.folder, place.folder / "projects"):
            os.makedirs(folder, mode=0o700, exist_ok=True)
        path = _record_path(place, found)
        write_json_atomic(path, record)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        return False
    return True


class DeviceApproval:
    """The approval recorded on this device for one project. `verdict` never raises and never looks a key up."""

    def __init__(self, root: pathlib.Path | str | Project | None, env: Mapping[str, str] | None = None) -> None:
        self._root = root
        self._env = env

    def verdict(self, scopes: Sequence[str], *, url: str, key_name: str, model: str) -> str:
        if self._root is None:
            return rerank.NO_CONSENT  # no project was named, so there is no approval to find
        try:
            found = self._root if isinstance(self._root, Project) else project(self._root)
            place = device(found, self._env)
            if not place.trusted:
                return rerank.DEVICE_STATE
            if place.folder is None:
                return rerank.NO_CONSENT
            state, record = _read_record(_record_path(place, found), found)
            if state == ABSENT:
                return rerank.NO_CONSENT
            if record is None:
                return rerank.DEVICE_STATE
            if state == DISABLED:
                return rerank.REVOKED
            if record["destination"] != {"url": url, "key_name": key_name, "model": model}:
                return rerank.STALE
            approved = record["scopes"]
            if any(scope not in approved for scope in scopes):
                return rerank.NO_CONSENT
            if any(scope not in POLICIES or approved[scope] != policy_digest(scope) for scope in scopes):
                return rerank.STALE
            return rerank.GRANTED
        except Exception:  # a store that cannot answer approves nothing
            return rerank.DEVICE_STATE


def device_approval(root: pathlib.Path | str, env: Mapping[str, str] | None = None) -> DeviceApproval:
    """The approval store `oah.rerank.admit` asks for the project a caller named, when the caller injects none."""
    return DeviceApproval(root, env)


def _refused(status: str, **extra: object) -> dict:
    return {"ok": False, "status": status, **extra}


def _destination(found: Project) -> tuple[dict | None, str]:
    """({kind, url, key_name, model}, "") the project's configuration names, read as text; else (None, why)."""
    try:
        section = load_config(found.root).section("rerank", rerank.DEFAULTS)
    except ConfigError:
        return None, CONFIG_INVALID
    key_name = rerank.PROVIDERS[rerank.TYPESAFE].key_name(section)
    try:
        secrets.check_name(key_name)
    except ValueError:
        return None, rerank.DESTINATION
    kind, url, model = typesafe.destination(section, key_name)
    if kind == typesafe.REFUSED or (kind == typesafe.CUSTOM and key_name == typesafe.DEFAULT_KEY_NAME):
        return None, rerank.DESTINATION
    if len(url) > 512 or len(model) > 128 or not url.isprintable() or not model.isprintable():
        return None, rerank.DESTINATION
    return {"kind": kind, "url": url, "key_name": key_name, "model": model}, ""


def _token(found: Project, target: Mapping[str, str], scopes: Sequence[str]) -> str:
    ordered = sorted(scopes)
    text = json.dumps([SCHEMA, found.id, target["url"], target["key_name"], target["model"],
                       [[scope, policy_digest(scope)] for scope in ordered]], separators=(",", ":"))
    return f"v1:{','.join(ordered)}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _scope_preview(scope: str) -> dict:
    policy = POLICIES[scope]
    return {"scope": scope, "title": policy.title, "sends": list(policy.sends), "labels": list(policy.labels),
            "never": list(policy.never), "bounds": {name: value for name, value in policy.bounds},
            "digest": policy_digest(scope)}


def status(root: pathlib.Path | str, *, env: Mapping[str, str] | None = None) -> dict:
    """What this device holds for the project, and where its configuration points. No key is looked up."""
    found = project(root)
    place = device(found, env)
    target, problem = _destination(found)
    result: dict = {"ok": True, "status": "ok", "project": {"id": found.id, "kind": found.kind, "path": found.path},
                    "destination": target, "destination_status": problem or "ok", "consent": ABSENT, "scopes": {}}
    if not place.trusted:
        return dict(result, consent=rerank.DEVICE_STATE)
    if place.folder is None:
        return result
    state, record = _read_record(_record_path(place, found), found)
    if state == ABSENT:
        return result
    if record is None:
        return dict(result, consent=rerank.DEVICE_STATE)
    if state == DISABLED:
        return dict(result, consent=DISABLED)
    same = target is not None and record["destination"] == {name: target[name] for name in ("url", "key_name", "model")}
    scopes = {scope: (rerank.GRANTED if same and digest == policy_digest(scope) else rerank.STALE)
              for scope, digest in record["scopes"].items()}
    return dict(result, consent=ENABLED if same else rerank.STALE, scopes=scopes,
                approved_destination=dict(record["destination"]))


def prepare(root: pathlib.Path | str, *, scopes: Iterable[str] = ("impact",),
            env: Mapping[str, str] | None = None) -> dict:
    """The approval preview for `scopes`: the project, the destination, what each scope sends, and the token
    `enable` takes. Nothing is written and no key is looked up, in the environment or in the key store. A scope
    without a policy (today every scope but `impact`) is refused with `scope`."""
    wanted = tuple(dict.fromkeys(scopes))
    if not wanted or any(scope not in POLICIES for scope in wanted):
        return _refused(SCOPE)
    found = project(root)
    place = device(found, env)
    if not place.trusted or place.folder is None:
        return _refused(rerank.DEVICE_STATE)
    target, problem = _destination(found)
    if target is None:
        return _refused(problem)
    return {"ok": True, "status": READY, "project": {"id": found.id, "kind": found.kind, "path": found.path},
            "destination": target,
            # By rule, not by looking: the key stored on this device goes to the official pair alone.
            "credential": ([ENVIRONMENT, DEVICE] if target["kind"] == typesafe.OFFICIAL else [ENVIRONMENT]),
            "scopes": [_scope_preview(scope) for scope in wanted], "token": _token(found, target, wanted)}


def enable(root: pathlib.Path | str, *, token: str, env: Mapping[str, str] | None = None) -> dict:
    """Record the approval a `prepare` preview described. The scopes in the token replace any recorded before."""
    match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    wanted = tuple(match.group(1).split(",")) if match else ()
    if not wanted or any(scope not in POLICIES for scope in wanted) or len(set(wanted)) != len(wanted):
        return _refused(PREVIEW_CHANGED)
    found = project(root)
    place = device(found, env)
    if not place.trusted or place.folder is None:
        return _refused(rerank.DEVICE_STATE)
    target, problem = _destination(found)
    if target is None:
        return _refused(problem)
    if not hmac.compare_digest(token, _token(found, target, wanted)):
        return _refused(PREVIEW_CHANGED)
    fields = {"state": ENABLED, "destination": {name: target[name] for name in ("url", "key_name", "model")},
              "scopes": {scope: policy_digest(scope) for scope in wanted}}
    if not _write_record(place, found, fields):
        return _refused(rerank.DEVICE_STATE)
    return {"ok": True, "status": ENABLED, "scopes": list(wanted)}


def disable(root: pathlib.Path | str, *, env: Mapping[str, str] | None = None) -> dict:
    """Switch the project off on this device: nothing is sent for it, whatever key exists. No key is looked up."""
    found = project(root)
    place = device(found, env)
    if not place.trusted or place.folder is None:
        return _refused(rerank.DEVICE_STATE)
    if not _write_record(place, found, {"state": DISABLED}):
        return _refused(rerank.DEVICE_STATE)
    return {"ok": True, "status": DISABLED}


def key_state(*, env: Mapping[str, str] | None = None, store: secrets.Keychain | None = None) -> dict:
    """Whether the key is in the environment, and what the device key store answers. The value is dropped."""
    state, found = secrets.find(KEY_NAME, keychain=store)
    del found
    return {"ok": True, "status": "ok", "name": KEY_NAME,
            ENVIRONMENT: bool((os.environ if env is None else env).get(KEY_NAME)), DEVICE: state}


def store_key(source: object, *, store: secrets.Keychain | None = None) -> dict:
    """Put the key in the OS key store. `source` is a `Secret`, or a stream a parent process hands over (a pipe;
    a terminal is refused). `added` or `updated`, else `invalid`, `locked`, `denied`, `unsupported` or `error`."""
    secret = None
    try:
        try:
            secret = source if isinstance(source, secrets.Secret) else secrets.read_handoff(source)
            if any(not 0x21 <= ord(char) <= 0x7e for char in secret.reveal()):
                return _refused(INVALID)  # the Authorization header takes visible ASCII alone
            return {"ok": True, "status": secrets.store(KEY_NAME, secret, keychain=store)}
        except secrets.KeyStoreError as exc:
            return _refused(exc.state)
        except ValueError:
            return _refused(INVALID)
    finally:
        del source, secret


def delete_key(*, store: secrets.Keychain | None = None) -> dict:
    """Remove the key from the OS key store: `removed` or `missing`, else the store's refusal."""
    try:
        return {"ok": True, "status": secrets.remove(KEY_NAME, keychain=store)}
    except secrets.KeyStoreError as exc:
        return _refused(exc.state)


def _probe_token(payload: Mapping) -> str:
    text = json.dumps([typesafe.OFFICIAL_URL, KEY_NAME, payload], sort_keys=True, separators=(",", ":"))
    return "v1:probe:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def prepare_test() -> dict:
    """What the connection check would send, where, and the token `run_test` takes. No key is looked up."""
    payload = typesafe.build_probe_request()
    return {"ok": True, "status": READY, "url": typesafe.OFFICIAL_URL, "key_name": KEY_NAME, "request": payload,
            "requests": 1, "timeout_seconds": typesafe.PROBE_TIMEOUT, "token": _probe_token(payload)}


def run_test(*, ack: str, env: Mapping[str, str] | None = None, store: secrets.Keychain | None = None,
             transport: Callable[..., object] | None = None) -> dict:
    """One fixed request to the official address with the environment key, else the key stored on this device.

    `ack` is the token of `prepare_test`; without it nothing is looked up or sent. `status` is `ok`, or one
    of `ack-required`, `no-key`, `key-locked`, `key-denied` and the provider codes (`unauthorized`,
    `rate-limited`, `overloaded`, `timeout`, `network`, `bad-response`, `http-<code>`)."""
    key = value = None
    try:
        payload = typesafe.build_probe_request()
        if not isinstance(ack, str) or not hmac.compare_digest(ack.encode("utf-8"), _probe_token(payload).encode()):
            return _refused(ACK_REQUIRED)
        value = (os.environ if env is None else env).get(KEY_NAME)
        source = ENVIRONMENT if value else DEVICE
        if value:
            key = secrets.Secret(value)
        else:
            state, key = secrets.find(KEY_NAME, keychain=store)
            if key is None:
                return _refused({secrets.LOCKED: rerank.KEY_LOCKED, secrets.DENIED: rerank.KEY_DENIED}.get(state, NO_KEY))
        try:
            send = typesafe.post_official if transport is None else transport
            typesafe.validate_probe_response(send(payload, key=key, timeout=typesafe.PROBE_TIMEOUT))
        except ProviderError as exc:
            return _refused(exc.code, source=source)
        return {"ok": True, "status": "ok", "source": source}
    finally:
        del env, store, key, value


# The packaged entry's whole Jev surface. Nothing that approves or authenticates is ever an argument.
COMMANDS = ("status", "prepare", "enable", "disable", "key-state", "key-store", "key-delete", "test-prepare",
            "test-run")
PROJECT_COMMANDS = ("status", "prepare", "enable", "disable")  # these take --root, and no other command does
USAGE, ROOT, INTERNAL = "usage", "root", "internal"
EXIT_DONE, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
TOKEN_LIMIT = 512  # bytes of the token line a caller hands over


def dispatch(argv: Sequence[str], *, stdin: object = None, env: Mapping[str, str] | None = None,
             store: secrets.Keychain | None = None,
             transport: Callable[..., object] | None = None) -> tuple[int, dict]:
    """(exit code, result) of one call from a packaged entry. It prints nothing and reports a failure as a word.

        status       --root <folder>
        prepare      --root <folder> [--scope <scope>]...   `impact` when none is named, and the only one today
        enable       --root <folder>                        the token of `prepare`, one line on `stdin`
        disable      --root <folder>
        key-state
        key-store                                           the key, one line on `stdin`
        key-delete
        test-prepare
        test-run                                            the token of `test-prepare`, one line on `stdin`

    `--root` is the absolute path of an existing folder, written as this system writes paths; no command reads
    the current folder. `stdin` is a byte stream (a pipe, never a terminal), read only by the three commands
    that take a value, and only up to `TOKEN_LIMIT` bytes for a token and `secrets.HANDOFF_LIMIT` for the key.
    A token or a key among the arguments is a usage error, whatever flag carries it.

    The result is the table of the function behind the command, ready for JSON, and always has `ok` and
    `status`. The exit code is `EXIT_DONE` (0) when `ok` is true; `EXIT_REFUSED` (1) with that function's
    refusal word, or `internal` when it failed unexpectedly; `EXIT_USAGE` (2) with `usage` (the command or its
    flags) or `root` (the folder)."""
    try:
        parsed = _arguments(argv)
        if parsed is None:
            return EXIT_USAGE, _refused(USAGE)
        command, root, scopes = parsed
        if root is not None and not _folder(root):
            return EXIT_USAGE, _refused(ROOT)
        try:
            if command == "status":
                result = status(root, env=env)
            elif command == "prepare":
                result = prepare(root, scopes=scopes or ("impact",), env=env)
            elif command == "enable":
                result = enable(root, token=_handed(stdin), env=env)
            elif command == "disable":
                result = disable(root, env=env)
            elif command == "key-state":
                result = key_state(env=env, store=store)
            elif command == "key-store":
                result = store_key(stdin, store=store)
            elif command == "key-delete":
                result = delete_key(store=store)
            elif command == "test-prepare":
                result = prepare_test()
            else:
                result = run_test(ack=_handed(stdin), env=env, store=store, transport=transport)
        except Exception:  # what failed stays here; a packaged caller gets one word
            result = _refused(INTERNAL)
        return (EXIT_DONE if result.get("ok") is True else EXIT_REFUSED), result
    finally:
        del stdin, env, store, transport


def _arguments(argv: object) -> tuple[str, str | None, tuple[str, ...]] | None:
    """(command, root, scopes) of a well-formed call, else None. A flag is two arguments: its name, its value."""
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(item, str) for item in argv):
        return None
    command, rest = argv[0], argv[1:]
    if command not in COMMANDS or len(rest) % 2:
        return None
    root, scopes = None, []
    for flag, value in zip(rest[::2], rest[1::2]):
        if flag == "--root" and root is None and command in PROJECT_COMMANDS:
            root = value
        elif flag == "--scope" and command == "prepare":
            scopes.append(value)
        else:
            return None
    if (root is None) == (command in PROJECT_COMMANDS):
        return None
    return command, root, tuple(scopes)


def _folder(path: str) -> bool:
    try:
        return os.path.isabs(path) and os.path.isdir(path)
    except (OSError, ValueError):
        return False


def _handed(stream: object) -> str:
    """The token line a caller handed over on a byte stream; "" when there is none that fits."""
    try:
        return secrets.read_handoff(stream, limit=TOKEN_LIMIT).reveal()
    except ValueError:
        return ""


def _safe_path(path: object) -> bool:
    if not isinstance(path, str) or not 0 < len(path) <= PATH_LIMIT:
        return False
    if _PATH_UNSAFE.search(path) or path.startswith(("/", "~")):
        return False
    if any(part in ("", ".", "..") for part in path.split("/")):
        return False
    return not any(pattern.search(path) for _rule, pattern in CONTENT_RULES)


def _row_name(row: object, files: Mapping[str, object] | Iterable[str]) -> str | None:
    """`path` or `path:line` for a weak row of a known repository file; None for a row that is never sent."""
    if getattr(row, "binding", None) not in IMPACT_BINDINGS or getattr(row, "layer", None) not in IMPACT_LAYERS:
        return None
    path, line = getattr(row, "source", None), getattr(row, "line", None)
    if not _safe_path(path) or path not in files:
        return None
    if line is None:
        return path
    if isinstance(line, bool) or not isinstance(line, int) or not 0 < line <= LINE_LIMIT:
        return None
    return f"{path}:{line}"


def _candidates(rows: Sequence[object], files: Mapping[str, object] | Iterable[str],
                limit: int) -> list[tuple[int, rerank.Candidate]]:
    """(row index, candidate) for the first `limit` rows that may be sent; a repeated name is sent once."""
    picked: list[tuple[int, rerank.Candidate]] = []
    names: set[str] = set()
    for index, row in enumerate(rows):
        if len(picked) >= limit:
            break
        name = _row_name(row, files)
        if name is None or name in names:
            continue
        names.add(name)
        kind = row.kind if row.kind in IMPACT_KINDS else IMPACT_OTHER_KIND
        picked.append((index, rerank.Candidate(name, f"{row.binding} {row.layer} {kind}")))
    return picked


def order_impact(rows: Sequence, *, root: pathlib.Path | str, files: Mapping[str, object] | Iterable[str],
                 offline: bool = False, config: Mapping | None = None, env: Mapping[str, str] | None = None,
                 keychain: secrets.Keychain | None = None, approval: rerank.Approval | None = None,
                 providers: Mapping[str, rerank.Provider] | None = None) -> tuple[list, str]:
    """(rows, status): the rows as given, where the weak rows that were sent may have changed places.

    `files` holds the repository-relative paths the Impact map knows; a row of any other source is not sent.
    `config` replaces the project's `[rerank]` table. `status` is `used` or `unused(<reason>)`:
    `not-configured`, `offline`, `no-candidates`, `no-consent`, `consent-revoked`, `consent-stale`,
    `device-state`, `destination`, `key-locked`, `key-denied`, `no key`, `unknown-provider`, the breaker's
    words and `error:<code>`."""
    original = list(rows)
    try:
        return _order_impact(original, root=root, files=files, offline=offline, config=config, env=env,
                             keychain=keychain, approval=approval, providers=providers)
    except ProviderError as exc:
        return original, rerank.unused(f"error:{exc.code}")
    except ConfigError:
        return original, rerank.unused("not-configured")
    except Exception:  # an ordering that cannot be made leaves the deterministic order
        return original, rerank.unused("error:internal")
    finally:
        del env, keychain, approval, providers


def _order_impact(original: list, *, root, files, offline, config, env, keychain, approval, providers) -> tuple[list, str]:
    section = load_config(pathlib.Path(root)).section("rerank", rerank.DEFAULTS) if config is None else config
    provider = section.get("provider")
    if section.get("enabled") is not True or not isinstance(provider, str) or not provider.strip():
        return original, rerank.unused("not-configured")
    if offline:
        return original, rerank.unused("offline")
    section = rerank.bounded(section)
    picked = _candidates(original, files, section["max_candidates"])
    if not picked:
        return original, rerank.unused("no-candidates")
    found = project(root)
    breaker_folder = None
    if found.common_dir is not None:
        breaker_folder = state_dir(RepoInfo(pathlib.Path(root), found.common_dir, None, None),
                                   os.environ if env is None else env)
    result = rerank.rerank(IMPACT_TASK, [candidate for _index, candidate in picked], config=section,
                           query=IMPACT_QUERY, env=env, keychain=keychain, providers=providers,
                           state_dir=breaker_folder, scopes=("impact",), root=root,
                           approval=DeviceApproval(found, env) if approval is None else approval)
    if not result.used:
        if result.reason == INSECURE_URL:
            return original, rerank.unused(f"error:{INSECURE_URL}")
        return original, rerank.unused(result.reason) if result.reason in _REASONS else result.status
    scores = dict(result.scores)
    ordered = list(original)
    for binding in IMPACT_BINDINGS:
        slots = [index for index, candidate in picked
                 if original[index].binding == binding and candidate.name in scores]
        names = {index: candidate.name for index, candidate in picked}
        ranked = sorted(slots, key=lambda index: -scores[names[index]])  # stable: equal scores keep their order
        for slot, index in zip(slots, ranked):
            ordered[slot] = original[index]
    if collections.Counter(ordered) != collections.Counter(original):
        return original, rerank.unused("error:internal")
    return ordered, rerank.USED
