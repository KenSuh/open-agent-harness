# oah.setup 검사 — 도구 목록의 완결성, 설치 계획·동의·실행, 로그인 안내, python -m oah.setup 실행을 확인한다.
from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import unittest

from oah.setup import tools as cat
from tests.helpers import make_tmp_dir, make_unlazy

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
OS_NAMES = ("macos", "windows")


class CatalogTest(unittest.TestCase):
    def test_catalog_is_complete(self):
        tools = cat.catalog()
        self.assertEqual(len({t.name for t in tools}), len(tools))
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.purpose)
                if tool.detect == "path":
                    self.assertTrue(tool.check and tool.check[0])
                for os_name in OS_NAMES:
                    self.assertTrue(tool.url_for(os_name).startswith("https://"))
                    for argv in tool.install.get(os_name, ()):
                        self.assertTrue(argv and all(isinstance(part, str) and part for part in argv))
                        if argv[0] not in ("/bin/sh", "powershell"):
                            # Plain argv lists are shown joined by spaces, so no part may contain one.
                            self.assertFalse(any(" " in part for part in argv), argv)
                # Every tool can be installed by command on both OSes; node on macOS without
                # Homebrew falls back to the download page at plan time.
                self.assertTrue(tool.install.get("macos") and tool.install.get("windows"))

    def test_catalog_lists_amendment_tools(self):
        names = [t.name for t in cat.catalog()]
        self.assertEqual(names, ["git", "python", "uv", "node", "claude", "codex", "unlazy", "graphify", "gh",
                                 "gitleaks", "jq", "rg"])

    def test_required_tools(self):
        required = {t.name for t in cat.catalog() if t.required}
        self.assertEqual(required, {"git", "python", "claude", "codex"})

    def test_one_liners_use_explicit_interpreters(self):
        tools = {t.name: t for t in cat.catalog()}
        self.assertEqual(tools["uv"].install["macos"][0],
                         ("/bin/sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"))
        self.assertEqual(tools["uv"].install["windows"][0],
                         ("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                          "irm https://astral.sh/uv/install.ps1 | iex"))
        self.assertEqual(tools["claude"].install["macos"][0],
                         ("/bin/sh", "-c", "curl -fsSL https://claude.ai/install.sh | bash"))
        self.assertEqual(tools["claude"].install["windows"][0][-1], "irm https://claude.ai/install.ps1 | iex")
        self.assertEqual(tools["codex"].install["windows"][0][-1], "irm https://chatgpt.com/codex/install.ps1 | iex")
        self.assertEqual(tools["git"].install["windows"][0], ("winget", "install", "--id", "Git.Git", "-e",
                                                              "--source", "winget"))
        self.assertEqual(tools["unlazy"].install["windows"][0], ("npx", "skills", "add", "Leonxlnx/unlazy"))
        # Shell one-liners are shown exactly as the official page prints them.
        self.assertEqual(cat.display(tools["uv"].install["macos"][0]),
                         "curl -LsSf https://astral.sh/uv/install.sh | sh")
        self.assertEqual(cat.display(tools["gh"].install["macos"][0]), "brew install gh")

    def test_needs_table(self):
        cases = {
            ("brew", "install", "gh"): "brew",
            cat.sh("curl -LsSf https://astral.sh/uv/install.sh | sh"): "curl",
            cat.powershell("irm https://astral.sh/uv/install.ps1 | iex"): "powershell",
            cat.powershell("scoop install gitleaks"): "scoop",
        }
        for argv, wanted in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(cat.needs(argv), wanted)

    def test_sign_in_commands(self):
        tools = {t.name: t for t in cat.catalog()}
        self.assertEqual((tools["claude"].sign_in.status_args, tools["claude"].sign_in.login),
                         (("auth", "status"), "claude auth login"))
        self.assertEqual((tools["codex"].sign_in.status_args, tools["codex"].sign_in.login),
                         (("login", "status"), "codex login"))
        self.assertEqual([t.name for t in tools.values() if t.sign_in], ["claude", "codex"])

    def test_os_name_for(self):
        self.assertEqual(cat.os_name_for("darwin"), "macos")
        self.assertEqual(cat.os_name_for("win32"), "windows")
        self.assertEqual(cat.os_name_for("linux"), "macos")


if __name__ == "__main__":
    unittest.main()
