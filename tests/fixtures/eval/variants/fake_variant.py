# 가짜 평가 변형 — miss 는 무관한 지적만 내고, catch 는 index.txt 를 다시 만들고 오래된 색인을 지적한다(제어 흐름 검증 전용).
"""A fake engine for `oah eval` tests. It never calls a model.

MODE miss   reports an unrelated finding about words.txt; records no usage
MODE catch  regenerates index.txt and reports it as stale; records usage, rounds and corrections
--marker PATH        append the mode on launch (proves whether a variant ran)
--record-dir FOLDER  write <mode>.json with what the variant saw
--exit-code N        exit code (default 0)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys


def _tree_digest(folder: pathlib.Path) -> dict:
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*")) if path.is_file() and ".git" not in path.parts}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8").stdout.strip()


def main(mode: str, *, marker: str | None = None, record_dir: str | None = None, exit_code: int = 0) -> int:
    prompt = sys.stdin.buffer.read().decode("utf-8")
    if marker:
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write(mode + "\n")
    if record_dir:
        seen = {"prompt": prompt, "cwd": os.getcwd(),
                "requirement": pathlib.Path(os.environ["OAH_EVAL_REQUIREMENT"]).read_text(encoding="utf-8"),
                "inputs": _tree_digest(pathlib.Path(os.environ["OAH_EVAL_INPUTS"])),
                "tree": _tree_digest(pathlib.Path.cwd()), "head": _git("rev-parse", "HEAD"),
                "commits": _git("rev-list", "--all", "--count"),
                "output_empty": not any(pathlib.Path(os.environ["OAH_EVAL_OUTPUT"]).iterdir())}
        pathlib.Path(record_dir, f"{mode}.json").write_text(json.dumps(seen), encoding="utf-8")
    if mode == "miss":
        result = {"engine": "fake", "findings": [
            {"id": "F1", "file": "words.txt", "summary": "words.txt is not sorted alphabetically"}]}
    else:
        subprocess.run([sys.executable, "build_index.py"], check=True)
        result = {"engine": "fake", "rounds": 1, "corrections": 1,
                  "usage": {"input_tokens": 120, "output_tokens": 30},
                  "findings": [{"id": "F1", "file": "index.txt",
                                "summary": "index.txt is stale: it was not regenerated after words.txt changed"}]}
    out = pathlib.Path(os.environ["OAH_EVAL_OUTPUT"]) / "result.json"
    out.write_text(json.dumps(result), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("miss", "catch"))
    parser.add_argument("--marker")
    parser.add_argument("--record-dir")
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args()
    sys.exit(main(args.mode, marker=args.marker, record_dir=args.record_dir, exit_code=args.exit_code))
