# rules 영역: AGENTS.md 관리 블록과 CLAUDE.md 가져오기 줄을 계산하는 rules.plan() 의 바이트 결과와 거부 코드를 사례로 만든다.
"""Area `rules`: the bytes `oah.rules.plan()` computes for AGENTS.md and CLAUDE.md, and every refusal."""
from __future__ import annotations

import pathlib

from fixture import Area, Case, Scratch, err, fn, ok, utf8

# The block body is a fixture input, so the algorithm is pinned apart from the v0.4.0 rules text.
BODIES = {
    "en": "## Fixture workflow\n\n1. Read the fixture rules first.\n2. Keep changes small.\n",
    "ko": "## 고정 작업 규칙\n\n1. 고정 규칙을 먼저 읽는다.\n2. 바뀐 만큼만 고친다.\n",
}
BEGIN = "<!-- OAH:RULES:BEGIN v1 -->"
END = "<!-- OAH:RULES:END -->"


def _plan(base: pathlib.Path, files: dict[str, bytes], lang: str | None) -> dict:
    from oah import rules

    scratch = Scratch(base, "<root>")
    original = rules.packaged

    def packaged(*parts: str) -> str:
        if parts[0] != "rules" or not parts[1].startswith("block.") or not parts[1].endswith(".md"):
            raise SystemExit(f"rules: unexpected packaged read {parts}")
        return BODIES[parts[1][len("block."):-len(".md")]]

    rules.packaged = packaged
    try:
        scratch.write(files)
        try:
            result = rules.plan(scratch.path, lang=lang)
        except rules.RulesError as exc:
            return err(exc.code)
        return ok({"lang": result.lang, "agents_after": result.agents_after.decode("utf-8"),
                   "claude_after": result.claude_after.decode("utf-8"), "block_bytes": result.block_bytes})
    finally:
        rules.packaged = original
        scratch.close()


def build(base: pathlib.Path) -> Area:
    from oah import rules

    cases: list[Case] = []

    def case(name: str, files: dict[str, bytes], lang: str | None = None, note: str = "") -> None:
        result = _plan(base, files, lang)
        cases.append(Case(name, result, {"lang": lang, "block_bodies": BODIES}, files,
                          function="oah.rules.plan", note=note))

    block = f"{BEGIN}\nold rules\n{END}"
    case("no-files", {}, note="AGENTS.md and CLAUDE.md are created")
    case("no-files-ko", {}, "ko")
    case("lang-unknown", {}, "fr")
    case("empty-agents", {"AGENTS.md": b""})
    case("append-lf", {"AGENTS.md": b"# Mine\n\nUse tabs.\n"})
    case("append-lf-no-final-newline", {"AGENTS.md": b"# Mine\nno final newline"})
    case("append-lf-blank-line-end", {"AGENTS.md": b"# Mine\n\n"})
    case("append-crlf", {"AGENTS.md": b"# Mine\r\n\r\nUse tabs.\r\n"})
    case("append-crlf-no-final-newline", {"AGENTS.md": b"# My agents\r\n\r\nUse tabs.\r\nno final newline"})
    case("append-whitespace-only", {"AGENTS.md": b"  \n"})
    case("replace-block-lf", {"AGENTS.md": utf8(f"intro ✓\n\n{block}\n\n## Mine\nkeep\tme  \n")})
    case("replace-block-crlf", {"AGENTS.md": utf8(f"top\r\n{BEGIN}\r\nold rules\r\n{END}\r\nend")})
    case("replace-block-mixed-outside", {"AGENTS.md": b"a\r\nb\nc\r\n" + utf8(f"{BEGIN}\r\nx\r\n{END}\r\n")
                                         + b"a\r\nb\nc\r\n"})
    case("replace-block-at-end-no-newline", {"AGENTS.md": utf8(f"top\n{block}")})
    case("replace-block-marker-trailing-space", {"AGENTS.md": utf8(f"{BEGIN}  \nx\n{END}\t\n")},
         note="marker lines may carry trailing whitespace")
    case("lang-detect-ko", {"AGENTS.md": utf8(f"{BEGIN}\n<!-- oah rules lang=ko; managed. -->\nx\n{END}\n")})
    case("lang-detect-unknown", {"AGENTS.md": utf8(f"{BEGIN}\n<!-- oah rules lang=xx; managed. -->\nx\n{END}\n")})
    case("lang-override", {"AGENTS.md": utf8(f"{BEGIN}\n<!-- oah rules lang=ko; managed. -->\nx\n{END}\n")}, "en")
    case("lang-param-on-append", {"AGENTS.md": b"# Mine\n"}, "ko")
    case("mention-is-not-marker", {"AGENTS.md": utf8(f"Docs: the block between `{BEGIN}` and `{END}`.\n  {BEGIN}\n")})
    case("claude-has-import", {"CLAUDE.md": b"# Notes\n  @AGENTS.md  \nmore\n"})
    case("claude-no-final-newline", {"CLAUDE.md": b"# Notes"})
    case("claude-crlf", {"CLAUDE.md": b"# Claude\r\n"})
    case("claude-empty", {"CLAUDE.md": b""})
    case("claude-duplicate-import", {"CLAUDE.md": b"@AGENTS.md\n@AGENTS.md\n"})
    case("claude-not-utf8", {"CLAUDE.md": b"caf\xe9\n"})
    case("local-rules-appended", {".oah/rules.local.md": b"- Use pnpm.\r\n- Never touch vendor/.\r\n"})
    case("local-rules-blank", {".oah/rules.local.md": b"\n\n  \n"})
    case("local-rules-crlf-agents", {"AGENTS.md": b"# Mine\r\n", ".oah/rules.local.md": b"- Use pnpm.\n"})
    case("local-rules-reserved-marker", {".oah/rules.local.md": utf8(f"Project rule.\r\n{END}  \r\n")})
    case("local-rules-not-utf8", {".oah/rules.local.md": b"caf\xe9\n"})
    case("agents-not-utf8", {"AGENTS.md": b"caf\xe9\n"})
    for name, text in (("begin-only", f"{BEGIN}\nx\n"), ("end-only", f"x\n{END}\n"), ("reversed", f"{END}\n{BEGIN}\n"),
                       ("no-version", f"<!-- OAH:RULES:BEGIN -->\n{END}\n"),
                       ("version-zero", f"<!-- OAH:RULES:BEGIN v0 -->\n{END}\n"),
                       ("unclosed-comment", f"<!-- OAH:RULES:BEGIN v1\n{END}\n"),
                       ("two-blocks", f"{BEGIN}\n{END}\n{BEGIN}\n{END}\n"),
                       ("newer-block", f"<!-- OAH:RULES:BEGIN v2 -->\n{END}\n")):
        case(f"marker-{name}", {"AGENTS.md": utf8(text)})
    case("block-too-large", {".oah/rules.local.md": b"- rule\n" * 1000})
    # The budget boundary: "x" * n + "\n\n" + block + "\n" is exactly AGENTS_BUDGET bytes at the limit.
    created = _plan(base, {}, None)["ok"]
    filler = rules.AGENTS_BUDGET - len(created["agents_after"].encode("utf-8")) - 2
    case("budget-at-limit", {"AGENTS.md": b"x" * filler + b"\n"}, note=f"{rules.AGENTS_BUDGET} bytes after the append")
    case("budget-over", {"AGENTS.md": b"x" * (filler + 1) + b"\n"}, note="one byte over the budget")

    return Area(
        "rules",
        functions=[fn("oah.rules", "plan"), fn("oah.rules", "render_block"), fn("oah.rules", "find_block")],
        mined_from=["tests/test_rules.py"],
        cases=cases,
        omitted=[
            {"behaviour": "OAH_RULES_LINK (AGENTS.md or CLAUDE.md is a symbolic link)",
             "reason": "a link cannot be stored as a portable fixture (Git checks links out as plain files on "
                       "Windows without symlink support), and the message names the resolved absolute target"},
            {"behaviour": "OAH_RULES_VERIFY (a written file does not read back)",
             "reason": "raised by sync() after writing, never by plan(); it needs a failing disk"},
            {"behaviour": "refusal messages", "reason": "only the codes are pinned"},
            {"behaviour": "the packaged v0.4.0 block bodies (block.en.md, block.ko.md)",
             "reason": "1.0 replaces the body; the body is a fixture input so the algorithm is pinned on its own"},
        ],
        notes=["The input files are the project root (AGENTS.md, CLAUDE.md, .oah/rules.local.md as present). input.json gives "
               "`lang` (the --lang value or null) and `block_bodies`, the text the packaged block.<lang>.md would "
               "hold.",
               "ok values: `lang`, `agents_after` and `claude_after` (the files' new contents) and `block_bytes`."],
    )
