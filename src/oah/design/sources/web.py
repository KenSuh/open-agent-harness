# 제한된 HTTPS 읽기 — 자격 증명·쿠키·프록시 없이, 연결 전에 모든 주소가 공개 주소인지 확인하고 그 주소에 고정해 접속하며, 같은 출처 재지정만 다시 확인하고, 크기·시간·형식을 제한한다.
"""Bounded, credential-free GET for public source documents and the GitHub REST API.

Every request:
- refuses a URL with user information, and http except to loopback under the test-only policy;
- refuses to run when an HTTP proxy applies to the destination (`proxy-configured`): a proxy would resolve the
  name itself, so oah could not check where the connection goes;
- resolves the host once per hop and refuses unless every address is globally routable (`private-destination`),
  then connects to that vetted address, with TLS verifying the original host name (DNS rebinding cannot move it);
- follows at most three same-origin redirects, re-resolving and re-vetting each; any other redirect is refused
  (`redirect-off-origin`), and the GitHub API mode treats every redirect as `moved`;
- sends no cookie, credential or authorization header and keeps only an allowlisted set of response headers;
- bounds size by Content-Length and while reading, and bounds the whole request (DNS, connect, TLS, a slow drip)
  by running it in a fresh interpreter that is killed at the deadline (macOS and Windows alike).
Errors carry closed codes only; bodies of error responses are never kept.
"""
from __future__ import annotations

import base64
import dataclasses
import http.client
import ipaddress
import json
import os
import pathlib
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Mapping, Sequence

USER_AGENT = "open-agent-harness-sources"
KEPT_HEADERS = ("content-type", "content-length", "retry-after", "x-ratelimit-remaining", "x-ratelimit-reset")
REDIRECTS = (301, 302, 303, 307, 308)
_WORKER_CODE = "import sys; sys.path.insert(0, sys.argv[1]); from oah.design.sources.web import _worker; _worker()"
_ENV_KEYS = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL",
             "LC_CTYPE"}


@dataclasses.dataclass(frozen=True)
class WebPolicy:
    max_bytes: int = 1 << 20
    timeout: float = 30.0
    max_redirects: int = 3
    allowed_types: tuple[str, ...] = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown",
                                      "application/json")
    redirects: str = "same-origin"         # same-origin | none (any redirect is `moved`)
    allow_loopback: bool = False           # test fixtures only; never from configuration
    resolve: Mapping[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)  # test-only answers

    def to_json(self) -> dict:
        data = dataclasses.asdict(self)
        data["resolve"] = {host: list(addresses) for host, addresses in self.resolve.items()}
        data["allowed_types"] = list(self.allowed_types)
        return data

    @classmethod
    def from_json(cls, data: Mapping) -> "WebPolicy":
        return cls(max_bytes=int(data["max_bytes"]), timeout=float(data["timeout"]),
                   max_redirects=int(data["max_redirects"]), allowed_types=tuple(data["allowed_types"]),
                   redirects=str(data["redirects"]), allow_loopback=bool(data["allow_loopback"]),
                   resolve={str(k): tuple(str(a) for a in v) for k, v in dict(data["resolve"]).items()})


@dataclasses.dataclass(frozen=True)
class WebResult:
    status: int
    final_url: str
    headers: Mapping[str, str]
    body: bytes


class WebFailure(Exception):
    """A closed code (see model.FAILURES), an HTTP status when one was received, and a retry time when given."""

    def __init__(self, code: str, http_status: int | None = None, retry_after: float | None = None) -> None:
        self.code, self.http, self.retry_after = code, http_status, retry_after
        super().__init__(code)


def origin(url: str) -> tuple[str, str, int]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _check_url(url: str, policy: WebPolicy) -> urllib.parse.SplitResult:
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port
    except ValueError:
        raise WebFailure("insecure-url") from None
    host = (parts.hostname or "").lower()
    if not host or "@" in parts.netloc or parts.fragment:
        raise WebFailure("insecure-url")
    if parts.scheme == "https":
        return parts
    if parts.scheme == "http" and policy.allow_loopback and _is_loopback(host):
        return parts
    raise WebFailure("insecure-url")


def proxy_applies(url: str) -> bool:
    """Whether an environment or system proxy would carry this request; then oah cannot vet the destination."""
    parts = urllib.parse.urlsplit(url)
    proxies = urllib.request.getproxies()
    if not (proxies.get(parts.scheme) or proxies.get("all")):
        return False
    try:
        return not urllib.request.proxy_bypass(parts.hostname or "")
    except (OSError, ValueError):
        return True


def _allowed_address(address: str, policy: WebPolicy) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if policy.allow_loopback and ip.is_loopback:
        return True
    return (ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_link_local
                                  or ip.is_loopback or ip.is_private))


def vetted_addresses(host: str, port: int, policy: WebPolicy) -> list[str]:
    """Every address of host, or a refusal when any is not public (a mixed answer is refused, not filtered)."""
    if host in policy.resolve:
        addresses = list(policy.resolve[host])
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError):
            raise WebFailure("dns") from None
        addresses = []
        for info in infos:
            address = info[4][0]
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise WebFailure("dns")
    if not all(_allowed_address(address, policy) for address in addresses):
        raise WebFailure("private-destination")
    return addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connects to one vetted address; TLS still checks the certificate for the original host name."""

    def __init__(self, host: str, port: int, address: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    """Loopback test fixtures only."""

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


def _retry_after(headers: Mapping[str, str], now: float) -> float | None:
    value = headers.get("retry-after", "")
    if value.isdigit():
        return now + min(int(value), 7 * 86400)
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return float(int(reset))
    return None


def _status_failure(status: int, headers: Mapping[str, str]) -> WebFailure:
    """An answer other than 200 (304, 3xx without a followed redirect, 4xx, 5xx) is never a body."""
    now = time.time()
    if status == 429 or (status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)):
        return WebFailure("rate-limited", status, _retry_after(headers, now) or now + 3600)
    if status == 404:
        return WebFailure("not-found", status)
    if status == 410:
        return WebFailure("gone", status)
    return WebFailure("http-error", status)


def _fetch(url: str, *, policy: WebPolicy, headers: Mapping[str, str], deadline: float,
           context: ssl.SSLContext | None = None) -> WebResult:
    """The request itself; the caller bounds wall time (the worker interpreter is killed at the deadline)."""
    current = url
    context = context or ssl.create_default_context()
    for _hop in range(policy.max_redirects + 1):
        parts = _check_url(current, policy)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = vetted_addresses(host, port, policy)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebFailure("timeout")
        if parts.scheme == "https":
            connection = _PinnedHTTPS(host, port, addresses[0], remaining, context)
        else:
            connection = _PinnedHTTP(host, port, addresses[0], remaining)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            connection.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                                                       **dict(headers)})
            response = connection.getresponse()
            kept = {name: response.getheader(name) for name in KEPT_HEADERS if response.getheader(name) is not None}
            if response.status in REDIRECTS:
                location = response.getheader("location") or ""
                response.close()
                if policy.redirects == "none":
                    raise WebFailure("moved", response.status)
                following = urllib.parse.urljoin(current, location)
                if origin(following) != origin(current):
                    raise WebFailure("redirect-off-origin", response.status)
                current = following
                continue
            if response.status != 200:
                response.close()
                raise _status_failure(response.status, kept)
            encoding = (response.getheader("content-encoding") or "identity").strip().lower()
            if encoding not in ("", "identity"):
                raise WebFailure("content-encoding", response.status)
            kind = (kept.get("content-type") or "").split(";", 1)[0].strip().lower()
            if kind not in policy.allowed_types:
                raise WebFailure("content-type", response.status)
            length = kept.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > policy.max_bytes):
                raise WebFailure("oversize", response.status)
            chunks, size = [], 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise WebFailure("timeout")
                if connection.sock is not None:
                    connection.sock.settimeout(left)
                chunk = response.read1(65536)  # at most one receive, so a slow drip meets the deadline check
                if not chunk:
                    break
                size += len(chunk)
                if size > policy.max_bytes:
                    raise WebFailure("oversize", response.status)
                chunks.append(chunk)
            if length is not None and size != int(length):
                raise WebFailure("network", response.status)
            return WebResult(response.status, current, kept, b"".join(chunks))
        except WebFailure:
            raise
        except (socket.timeout, TimeoutError):
            raise WebFailure("timeout") from None
        except ssl.SSLError:
            raise WebFailure("network") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise WebFailure("network") from None
        finally:
            connection.close()
    raise WebFailure("too-many-redirects")


def _worker() -> None:
    """Private interpreter entry point: only validated fields or a closed code cross the pipe."""
    try:
        request = json.load(sys.stdin)
        policy = WebPolicy.from_json(request["policy"])
        result = _fetch(request["url"], policy=policy, headers=request["headers"],
                        deadline=time.monotonic() + policy.timeout)
        answer = {"ok": True, "status": result.status, "final_url": result.final_url, "headers": dict(result.headers),
                  "body": base64.b64encode(result.body).decode("ascii")}
    except WebFailure as exc:
        answer = {"ok": False, "code": exc.code, "http": exc.http, "retry_after": exc.retry_after}
    except Exception:
        answer = {"ok": False, "code": "network", "http": None, "retry_after": None}
    sys.stdout.write(json.dumps(answer))


def _worker_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.upper() in _ENV_KEYS}


def get(url: str, *, policy: WebPolicy, headers: Mapping[str, str] | None = None) -> WebResult:
    """One bounded GET. Raises WebFailure with a closed code; never returns a partial body."""
    _check_url(url, policy)
    if proxy_applies(url):
        raise WebFailure("proxy-configured")
    payload = json.dumps({"url": url, "headers": dict(headers or {}), "policy": policy.to_json()}).encode("utf-8")
    src = str(pathlib.Path(__file__).resolve().parents[3])
    try:
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", _WORKER_CODE, src], input=payload,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_worker_env(),
                                   timeout=policy.timeout + 2.0)
    except subprocess.TimeoutExpired:
        raise WebFailure("timeout") from None
    except OSError:
        raise WebFailure("network") from None
    try:
        answer = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WebFailure("network") from None
    if not isinstance(answer, dict):
        raise WebFailure("network")
    if not answer.get("ok"):
        code = answer.get("code") if isinstance(answer.get("code"), str) else "network"
        http_status = answer.get("http") if type(answer.get("http")) is int else None
        retry = answer.get("retry_after") if isinstance(answer.get("retry_after"), (int, float)) else None
        raise WebFailure(code, http_status, retry)
    try:
        body = base64.b64decode(answer["body"], validate=True)
        headers_out = {str(k): str(v) for k, v in dict(answer["headers"]).items() if str(k) in KEPT_HEADERS}
        result = WebResult(int(answer["status"]), str(answer["final_url"]), headers_out, body)
    except (KeyError, TypeError, ValueError):
        raise WebFailure("network") from None
    if len(result.body) > policy.max_bytes:
        raise WebFailure("oversize")
    if origin(result.final_url) != origin(url):
        raise WebFailure("redirect-off-origin")
    return result


def get_in_process(url: str, *, policy: WebPolicy, headers: Mapping[str, str] | None = None,
                   context: ssl.SSLContext | None = None) -> WebResult:
    """The same request in this process (tests and the worker share `_fetch`); the deadline is checked between
    reads, so only the worker path bounds a stalled DNS lookup."""
    _check_url(url, policy)
    if proxy_applies(url):
        raise WebFailure("proxy-configured")
    return _fetch(url, policy=policy, headers=dict(headers or {}), deadline=time.monotonic() + policy.timeout,
                  context=context)


def visible_text(html: str) -> str:
    """The visible text of an HTML page: no script, style, noscript, template or comments; whitespace collapsed."""
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.skip = 0
            self.parts: list[str] = []

        def handle_starttag(self, tag: str, attrs: Sequence) -> None:
            if tag in ("script", "style", "noscript", "template"):
                self.skip += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in ("script", "style", "noscript", "template") and self.skip:
                self.skip -= 1

        def handle_data(self, data: str) -> None:
            if not self.skip:
                self.parts.append(data)

    parser = _Text()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):
        pass
    return " ".join(" ".join(parser.parts).split())
