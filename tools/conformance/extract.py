# 고정된 v0.4.0 패키지를 한 번 실행해 conformance/v0.4.0/ 아래 골든 픽스처를 쓰거나(--check 면) 커밋된 트리와 바이트 단위로 대조한다.
"""Write the v0.4.0 golden fixtures, or compare them with the committed tree.

    python3.11 -I tools/conformance/extract.py           # rewrite conformance/v0.4.0/
    python3.11 -I tools/conformance/extract.py --check   # extract into a temporary folder and compare

The frozen code is imported from src/ of this checkout; package resources come from the tag's Git blobs.
The extractor refuses to run unless src/oah is the tree of tag v0.4.0 with no uncommitted change.
It runs that code with HOME, USERPROFILE, XDG and OAH variables
pointed at synthetic temporary folders and rewrites every temporary path in an output to a fixed placeholder.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TARGET = ROOT / "conformance" / "v0.4.0"
TAG = "v0.4.0"
AREAS = ("config", "rules", "review", "license", "doctor", "models", "skills-roots", "hashes")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def frozen_problem() -> str | None:
    """Why src/oah is not the v0.4.0 tree, or None."""
    tag = _git("rev-parse", "--verify", "--quiet", f"{TAG}^{{tree}}:src/oah")
    if tag.returncode != 0:
        return f"tag {TAG} is not present in this repository (git fetch --tags)"
    head = _git("rev-parse", "--verify", "--quiet", "HEAD:src/oah")
    if head.returncode != 0 or head.stdout.strip() != tag.stdout.strip():
        return f"src/oah at HEAD is not the tree of {TAG}"
    status = _git("status", "--porcelain", "--untracked-files=all", "--", "src/oah")
    if status.returncode != 0:
        return "git status failed: " + status.stderr.strip()
    if status.stdout.strip():
        return "src/oah has uncommitted changes:\n" + status.stdout.rstrip()
    return None


def isolate(base: pathlib.Path) -> None:
    """Point every per-user location v0.4.0 may read at synthetic folders under `base`."""
    home = base / "home"
    home.mkdir()
    for name in list(os.environ):
        if name.startswith(("OAH_", "XDG_", "GIT_", "CLAUDE_", "CODEX_", "ANTHROPIC_", "OPENAI_")) or \
                name.endswith("_API_KEY") or name.lower().endswith("_proxy"):
            del os.environ[name]
    os.environ.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(base / "xdg" / "config"), "XDG_DATA_HOME": str(base / "xdg" / "data"),
        "XDG_STATE_HOME": str(base / "xdg" / "state"), "XDG_CACHE_HOME": str(base / "xdg" / "cache"),
        "OAH_HOME": str(base / "oah-home"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
    })


def extract(base: pathlib.Path) -> dict[str, bytes]:
    sys.dont_write_bytecode = True
    for path in (str(HERE), str(ROOT / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import importlib

    files: dict[str, bytes] = {}
    with importlib.import_module("frozen").package_resources(base):
        for area in AREAS:
            module = importlib.import_module("area_" + area.replace("-", "_"))
            built = module.build(base)
            if built.name != area:
                raise SystemExit(f"area module for {area} built {built.name}")
            files.update(built.files())
    leaks = [str(base), base.as_posix(), str(pathlib.Path(base).resolve()), REAL_HOME]
    for rel, data in files.items():
        text = data.decode("utf-8", "replace")
        for leak in leaks:
            if leak and leak in text:
                raise SystemExit(f"refusing to write {rel}: it contains a machine path")
    return files


def write(files: dict[str, bytes], target: pathlib.Path) -> None:
    for rel, data in sorted(files.items()):
        path = target.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def tree(root: pathlib.Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


REAL_HOME = os.path.expanduser("~") if os.path.expanduser("~") not in ("", "~", "/") else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check the v0.4.0 golden conformance fixtures.")
    parser.add_argument("--check", action="store_true",
                        help="extract into a temporary folder and compare with conformance/v0.4.0")
    args = parser.parse_args(argv)
    problem = frozen_problem()
    if problem is not None:
        print(f"CONFORMANCE_REFUSED: {problem}. The fixtures are extracted only from the frozen {TAG} package; "
              f"check out a commit whose src/oah equals tag {TAG} and retry.", file=sys.stderr)
        return 1
    base = pathlib.Path(tempfile.mkdtemp(prefix="oah-conformance-"))
    try:
        isolate(base)
        files = extract(base)
        if not args.check:
            if TARGET.exists():
                shutil.rmtree(TARGET)
            write(files, TARGET)
            print(f"CONFORMANCE_WRITTEN files={len(files)}")
            return 0
        scratch = base / "out"
        write(files, scratch)
        fresh, committed = tree(scratch), tree(TARGET)
        differences = [f"missing from the committed tree: {rel}" for rel in sorted(set(fresh) - set(committed))]
        differences += [f"not produced by the extractor: {rel}" for rel in sorted(set(committed) - set(fresh))]
        differences += [f"bytes differ: {rel}" for rel in sorted(set(fresh) & set(committed))
                        if fresh[rel] != committed[rel]]
        if differences:
            for line in differences:
                print(line)
            print(f"CONFORMANCE_MISMATCH differences={len(differences)}")
            return 1
        print(f"CONFORMANCE_MATCH files={len(fresh)}")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
