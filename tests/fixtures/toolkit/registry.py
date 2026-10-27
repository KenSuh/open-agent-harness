# 가짜 npm 레지스트리 — 127.0.0.1 의 로컬 HTTP 서버로 정상·잘못된·과대·느린 응답을 돌려준다(네트워크 없음).
from __future__ import annotations

import http.server
import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def latest_answer(package: str, version: str, **extra) -> bytes:
    return json.dumps(dict({"name": package, "version": version}, **extra)).encode("utf-8")


class FakeRegistry:
    """Serves answers[path] as (status, body bytes[, headers]); unknown paths get 404."""

    def __init__(self, test: unittest.TestCase, answers: dict | None = None, *, delay: float = 0.0,
                 trickle: str | None = None, interval: float = 0.01) -> None:
        self.answers = dict(answers or {})
        self.delay = delay
        self.requests: list[tuple[str, dict[str, str]]] = []
        registry = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server naming)
                registry.requests.append((self.path, dict(self.headers.items())))
                if registry.delay:
                    time.sleep(registry.delay)
                status, body, *rest = registry.answers.get(self.path, (404, b'{"error":"Not found"}'))
                headers = rest[0] if rest else {"Content-Type": "application/json"}
                try:
                    if registry.trickle == "headers":
                        for byte in b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n":
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(interval)
                        self.wfile.write(body)
                        return
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if registry.trickle == "body":
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(interval)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.trickle = trickle
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def closed_port_url() -> str:
    """A loopback URL nothing listens on, so a connection is refused at once."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def without_proxies(test: unittest.TestCase) -> None:
    """Drop proxy variables (any case) for the test, so loopback requests go straight to the fake."""
    clean = {key: value for key, value in os.environ.items() if key.upper() not in PROXY_KEYS}
    patcher = mock.patch.dict(os.environ, clean, clear=True)
    patcher.start()
    test.addCleanup(patcher.stop)


def forbid_network(test: unittest.TestCase) -> None:
    """Fail the test if anything opens a TCP connection."""
    def refuse(*args, **kwargs):
        raise AssertionError("this code path must not touch the network")

    patcher = mock.patch("socket.create_connection", refuse)
    patcher.start()
    test.addCleanup(patcher.stop)
