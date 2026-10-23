# 테스트 공용 도우미 — 임시 폴더, POSIX 가짜 도구, npm 설치 배치(layout)를 만든다.
from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import tempfile
import unittest


def make_tmp_dir(test: unittest.TestCase) -> pathlib.Path:
    """A fresh temporary folder that is removed when the test ends."""
    path = pathlib.Path(tempfile.mkdtemp(prefix="oah-test-"))
    test.addCleanup(shutil.rmtree, path, True)
    return path


def write_posix_tool(folder: pathlib.Path, name: str, body: str) -> pathlib.Path:
    """An executable /bin/sh script named `name` (POSIX only)."""
    folder.mkdir(parents=True, exist_ok=True)
    tool = folder / name
    tool.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tool


def npm_layout(prefix: pathlib.Path, package: str, bin_field, *, command: str,
               script_rel: str, script_body: str = "", local_node: bool = False,
               shim_body: str = "@echo off\r\n") -> pathlib.Path:
    """Lay out a Windows npm install: <prefix>/<command>.cmd plus node_modules/<package>/.

    Returns the shim path. `bin_field` is written to package.json as given.
    """
    package_dir = prefix / "node_modules" / pathlib.PurePosixPath(package)
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "package.json").write_text(
        json.dumps({"name": package, "version": "1.0.0", "bin": bin_field}), encoding="utf-8")
    script = package_dir / pathlib.PurePosixPath(script_rel)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(script_body, encoding="utf-8")
    shim = prefix / f"{command}.cmd"
    shim.write_text(shim_body, encoding="utf-8", newline="")
    if local_node:
        (prefix / "node.exe").write_bytes(b"")
    return shim


def require_node(test: unittest.TestCase) -> str:
    """Return the real node executable, or skip — or fail when OAH_REQUIRE_NODE_TESTS=1."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("OAH_REQUIRE_NODE_TESTS") == "1":
            test.fail("node is required for this test (OAH_REQUIRE_NODE_TESTS=1)")
        test.skipTest("node is not installed")
    return node
