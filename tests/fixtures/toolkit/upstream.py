# 툴킷 테스트 도우미 — 임시 폴더에 가짜 업스트림 저장소·허용 목록·번들 항목을 만든다(네트워크 없음).
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import shutil
import stat
import subprocess
import unittest

from oah.toolkit.catalog import Bundled, Linked, Step
from tests.helpers import git_commit, git_env, make_tmp_dir

MIT_TEXT = """MIT License

Copyright (c) 2026 Example Authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
"""

APACHE_TEXT = """                                 Apache License
                           Version 2.0, January 2004

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION
"""

TODAY = dt.date(2026, 10, 23)


def remove_readonly(function, path, exc_info):
    """Windows Git objects can be read-only; clear that bit and retry the failed removal."""
    if not isinstance(exc_info[1], PermissionError):
        raise exc_info[1]
    pathlib.Path(path).chmod(stat.S_IREAD | stat.S_IWRITE)
    function(path)


def remove_tree(path: pathlib.Path) -> None:
    shutil.rmtree(path, onerror=remove_readonly)


def skill_text(name: str, description: str = "Does a thing.", body: str = "# Skill\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def make_upstream(test: unittest.TestCase, files: dict | None = None) -> pathlib.Path:
    """A local upstream repository on branch main with one skill under skills/demo and an MIT LICENSE."""
    repo = make_tmp_dir(test) / "upstream"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "false"], check=True)
    test.addCleanup(lambda: remove_tree(repo) if repo.exists() else None)
    content = {"LICENSE": MIT_TEXT, "README.md": "upstream\n",
               "skills/demo/SKILL.md": skill_text("demo", body="See [ref](ref.md).\n"),
               "skills/demo/ref.md": "reference\n"}
    content.update(files or {})
    git_commit(repo, content, "initial")
    return repo


def commit(repo: pathlib.Path, files: dict, message: str = "change") -> str:
    return git_commit(repo, files, message)


def make_executable(repo: pathlib.Path, path: str) -> str:
    subprocess.run(["git", "-C", str(repo), "update-index", "--chmod=+x", path], check=True, env=git_env())
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "chmod"],
                   check=True, env=git_env())
    return head(repo)


def head(repo: pathlib.Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def bundled(repo: pathlib.Path, *, name: str = "demo", subpath: str = "skills/demo", skills=(".",),
            license_path: str = "LICENSE", spdx: str = "MIT", commit_id: str | None = None,
            notice_paths=()) -> Bundled:
    return Bundled(name=name, kind="skill", repository=str(repo), subpath=subpath, ref="main",
                   commit=commit_id or head(repo), spdx=spdx, license_path=license_path,
                   notice_paths=tuple(notice_paths), skills=tuple(skills), purpose="A demo skill.",
                   official_install="npx skills add example/demo")


def linked(name: str = "helper", steps=(Step(run=("python3", "-c", "print('ok')")),)) -> Linked:
    return Linked(name=name, kind="skill", source="https://github.com/example/helper", version="1.0",
                  license="MIT", steps=tuple(steps), reason="its upstream installs it", purpose="Helps.")


def catalog_json(entries_bundled=(), entries_linked=()) -> dict:
    def as_json(entry) -> dict:
        data = dataclasses.asdict(entry)
        if "steps" in data:
            data["steps"] = [{"run": list(step["run"])} if step["run"] is not None else {"manual": step["manual"]}
                             for step in data["steps"]]
        return {key: list(value) if isinstance(value, tuple) else value for key, value in data.items()}

    return {"version": 1, "bundled": [as_json(e) for e in entries_bundled],
            "linked": [as_json(e) for e in entries_linked]}


def write_catalog(folder: pathlib.Path, entries_bundled=(), entries_linked=()) -> pathlib.Path:
    path = folder / "upstreams.json"
    path.write_text(json.dumps(catalog_json(entries_bundled, entries_linked), indent=2), encoding="utf-8")
    return path
