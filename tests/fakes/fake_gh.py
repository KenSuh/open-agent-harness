# 가짜 gh — oah land 시험에서 받은 인자를 기록하고, 시나리오 파일의 커밋별 체크 실행을 GitHub API 모양으로 쪽 단위로 돌려주며, 버려야 할 미끼 필드도 함께 싣는다.
"""A stand-in for `gh api --method GET -H <header> repos/{owner}/{repo}/commits/<sha>/check-runs?per_page=N&page=M`.

Environment:
- FAKE_GH_LOG: a JSONL file; each call appends its argv.
- FAKE_GH_SCRIPT: a JSON file {"check_runs": {"<sha>": [{"name", "status", "conclusion"}, ...]}, "exit": int,
  "stderr": str, "raw": str}. A non-zero "exit" prints "stderr" and exits with it; "raw" is printed instead of
  JSON. Without a script every commit has no check runs. Optional "expected_repo" selects these runs only
  when GH_REPO matches; otherwise "other_runs" models an unrelated configured repository. Local bare
  destinations use oah's digest-only selector, which real gh rejects before making a request.
  "pages" overrides fields for a numbered page; "sleep" delays that response and "total_count" overrides its total.
Every run carries the decoys a real response has (`html_url`, `app.owner.login`, `id`), so a test can prove
oah keeps only validated statuses, conclusions and positions.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import urllib.parse

ENDPOINT = re.compile(r"repos/\{owner\}/\{repo\}/commits/([0-9a-f]{40}|[0-9a-f]{64})/check-runs(?:\?(.*))?")


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_GH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    path = os.environ.get("FAKE_GH_SCRIPT")
    script = json.loads(pathlib.Path(path).read_text(encoding="utf-8")) if path else {}
    if argv[:1] != ["api"]:
        sys.stderr.write(f"fake gh: unsupported command {argv[:1]}\n")
        return 2
    match = next((m for m in (ENDPOINT.fullmatch(arg) for arg in argv[1:]) if m), None)
    if match is None:
        sys.stderr.write("fake gh: unsupported endpoint\n")
        return 2
    sha, query = match.group(1), urllib.parse.parse_qs(match.group(2) or "")
    per_page, page = int(query.get("per_page", ["30"])[0]), int(query.get("page", ["1"])[0])
    script = {**script, **script.get("pages", {}).get(str(page), {})}
    if script.get("sleep"):
        time.sleep(script["sleep"])
    if script.get("exit"):
        sys.stderr.write(script.get("stderr", ""))
        return int(script["exit"])
    if script.get("raw") is not None:
        sys.stdout.write(script["raw"])
        return 0
    runs = script.get("check_runs", {}).get(sha, [])
    if "expected_repo" in script and os.environ.get("GH_REPO") != script["expected_repo"]:
        runs = script.get("other_runs", [])
    chunk = runs[(page - 1) * per_page: page * per_page]
    body = {"total_count": script.get("total_count", len(runs)), "check_runs": [
        dict(run, head_sha=run.get("head_sha", sha), id=4000 + index,
             html_url=f"https://example.invalid/checks/{4000 + index}", app={"owner": {"login": "decoy-owner-login"}})
        for index, run in enumerate(chunk, start=(page - 1) * per_page)]}
    sys.stdout.write(json.dumps(body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
