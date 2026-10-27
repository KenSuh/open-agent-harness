# npm 레지스트리 질의 — MCP 템플릿 핀의 최신 버전을 자격 증명 없이 묻고, 엄격한 semver 로 검증된 값만 돌려준다.
"""Ask the npm registry for a package's latest version, for MCP template pin notices.

The query sends no credential: proxies come only from the allowlisted environment that skill fetches
use (a proxy with embedded user information is refused), the opener has no cookie or auth handler,
and redirects are not followed. The answer is capped in size and time. Registry text is untrusted:
only a version matching a strict semver pattern is returned, and errors never quote the answer.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from oah.toolkit.fetch import FETCH_TIMEOUT, FetchError, git_env

REGISTRY = "https://registry.npmjs.org"
MAX_ANSWER_BYTES = 1 << 20
MAX_VERSION_LENGTH = 64
_IDENT = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                       rf"(?:-({_IDENT}(?:\.{_IDENT})*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")

Opener = Callable[[urllib.request.Request, float], object]


class RegistryOffline(Exception):
    """The registry could not be reached in time; the pin stays and the next check asks again."""


class RegistryInvalid(Exception):
    """The registry answered with something oah does not accept; the answer itself is never quoted."""


def is_semver(text: object) -> bool:
    return isinstance(text, str) and len(text) <= MAX_VERSION_LENGTH and SEMVER_RE.fullmatch(text) is not None


def _key(version: str) -> tuple:
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ValueError("not a semver version")
    release = tuple(int(part) for part in match.groups()[:3])
    if match.group(4) is None:
        return release, (1,)
    # Numeric identifiers sort below alphanumeric ones; a shorter prerelease sorts first.
    parts = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in match.group(4).split("."))
    return release, (0, parts)


def newer(candidate: str, pinned: str) -> bool:
    """True when candidate has higher semver precedence than pinned (build metadata is ignored)."""
    return _key(candidate) > _key(pinned)


def _registry_base(registry: str) -> str:
    """https anywhere, or http to a loopback address (tests); no user info, query or fragment."""
    parts = urllib.parse.urlsplit(registry)
    loopback = False
    if parts.hostname:
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
    if (parts.scheme not in ("https", "http") or (parts.scheme == "http" and not loopback) or not parts.hostname
            or "@" in parts.netloc or parts.query or parts.fragment):
        raise RegistryInvalid("the registry address must be a plain https URL")
    return registry.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RegistryInvalid(f"the registry answered with a redirect (HTTP {int(code)}), which oah does not follow")


def _proxies(url: str) -> dict[str, str]:
    """Proxies from the allowlisted fetch environment only; refuses a proxy with embedded credentials."""
    try:
        env = git_env()
    except FetchError as exc:
        raise RegistryOffline(str(exc).partition(": ")[2]) from None
    found = {key.upper(): value for key, value in env.items() if key.upper().endswith("_PROXY") and value}
    host = urllib.parse.urlsplit(url).hostname or ""
    if "NO_PROXY" in found and urllib.request.proxy_bypass_environment(host, {"no": found["NO_PROXY"]}):
        return {}
    proxies = {}
    for scheme in ("http", "https"):
        value = found.get(f"{scheme.upper()}_PROXY") or found.get("ALL_PROXY")
        if value:
            proxies[scheme] = value
    return proxies


def _default_opener(url: str) -> Opener:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(_proxies(url)), _NoRedirect)
    return lambda request, timeout: opener.open(request, timeout=timeout)


def _read(response, deadline: float) -> bytes:
    chunks, size = [], 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError
        chunk = response.read(65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_ANSWER_BYTES:
            raise RegistryInvalid(f"the registry answer is larger than {MAX_ANSWER_BYTES} bytes")
        chunks.append(chunk)


def latest_version(package: str, *, registry: str | None = None, timeout: float | None = None,
                   opener: Opener | None = None) -> str:
    """The `latest` dist-tag version of an npm package. Raises RegistryOffline or RegistryInvalid."""
    base = _registry_base(registry if registry is not None else REGISTRY)
    timeout = FETCH_TIMEOUT if timeout is None else timeout
    url = f"{base}/{urllib.parse.quote(package, safe='@/')}/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "oah"})
    deadline = time.monotonic() + timeout
    try:
        with (opener or _default_opener(url))(request, timeout) as response:
            raw = _read(response, deadline)
    except urllib.error.HTTPError as exc:
        raise RegistryInvalid(f"the registry answered HTTP {int(exc.code)}") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, RegistryInvalid):
            raise exc.reason from None
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise RegistryOffline(f"the npm registry did not answer within {timeout:g}s") from None
        raise RegistryOffline("the npm registry could not be reached") from None
    except (TimeoutError, socket.timeout):
        raise RegistryOffline(f"the npm registry did not answer within {timeout:g}s") from None
    except http.client.HTTPException:
        raise RegistryInvalid("the registry answer could not be read as HTTP") from None
    except OSError:
        raise RegistryOffline("the connection to the npm registry failed") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RegistryInvalid("the registry answer is not UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise RegistryInvalid("the registry answer is not a JSON object")
    if data.get("name") != package:
        raise RegistryInvalid("the registry answer's name field does not match the package asked for")
    version = data.get("version")
    if not is_semver(version):
        raise RegistryInvalid("the registry answer's version field is not a plain semver version")
    return version
