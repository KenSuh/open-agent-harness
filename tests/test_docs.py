# 문서 검사 — 문제 해결 문서의 OAH_* 코드가 src/oah 에 실제로 있는지, 고정 effort 상한 주장·깨진 링크·한국어 쌍·콜론 종결이 없는지 본다.
from __future__ import annotations

import importlib.util
import pathlib
import re
import shutil
import unittest

from tests.helpers import make_tmp_dir

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "oah"
PAGES = ("concepts", "troubleshooting", "upgrade", "keys")
EXAMPLE = REPO / "examples" / "hello-oah"
CODE = re.compile(r"\bOAH_[A-Z0-9]+(?:_[A-Z0-9]+)*\b")
# Environment variables and code prefixes that docs may name; they are not diagnostics.
NOT_DIAGNOSTICS = {"OAH_STATE_DIR", "OAH_WORKER", "OAH_RUN_ID", "OAH_TASK", "OAH_UNLAZY_DIR", "OAH_NODE"}
FIXED_CAP = re.compile(
    r"max(?:imum)?\s+(?:effort\s+)?is\s+(?:rejected|refused|not\s+(?:allowed|supported|permitted))"
    r"|effort\s+(?:is\s+)?capped|caps?\s+(?:the\s+)?effort\s+at|highest\s+(?:allowed\s+)?effort\s+is\s+(?:xhigh|high)"
    r"|xhigh\s+is\s+the\s+(?:highest|top|maximum)",
    re.IGNORECASE)
# Codes of commands from lanes still landing: allowed to be missing only while their module is absent.
PENDING_LANES = {"OAH_FIX_": "oah.fix_cli", "OAH_INIT_": "oah.init_cli"}
LINK = re.compile(r"\]\(([^)\s]+)\)")
HANGUL_COLON = re.compile(r"[가-힣][)\]`*]*:\s*$")


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def codes_in(text: str) -> set[str]:
    return set(CODE.findall(text)) - NOT_DIAGNOSTICS


def source_text(src_root: pathlib.Path) -> str:
    return "\n".join(read(path) for path in sorted(src_root.rglob("*.py")))


def missing_codes(doc_text: str, src_root: pathlib.Path) -> list[str]:
    """The OAH_* codes named in doc_text that no Python file under src_root contains."""
    source = source_text(src_root)
    return sorted(code for code in codes_in(doc_text) if not re.search(r"\b" + code + r"\b", source))


def lane_docs() -> list[pathlib.Path]:
    pages = [REPO / "docs" / f"{page}.md" for page in PAGES] + [REPO / "docs" / "ko" / f"{page}.md" for page in PAGES]
    return [REPO / "README.md", *pages, EXAMPLE / "README.md", EXAMPLE / "README.ko.md"]


def all_docs() -> list[pathlib.Path]:
    docs = [REPO / "README.md", REPO / "CONTRIBUTING.md", *sorted((REPO / "docs").rglob("*.md")),
            *sorted((REPO / "examples").rglob("*.md"))]
    return [path for path in docs if path.exists() and path.name != "PLAN.md" and "superpowers" not in path.parts]


def prose_lines(text: str):
    """Lines outside fenced code blocks, with their line numbers."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


class TroubleshootingCodesTest(unittest.TestCase):
    def test_listed_codes_exist(self):
        for page in (REPO / "docs/troubleshooting.md", REPO / "docs/ko/troubleshooting.md"):
            with self.subTest(page=page.name, lang=page.parent.name):
                text = read(page)
                self.assertGreaterEqual(len(codes_in(text)), 20, "the page lists the common refusals")
                self.assertEqual(missing_codes(text, SRC), [], f"{page} names codes that src/oah no longer has")

    def test_renamed_code_fails(self):
        text = read(REPO / "docs/troubleshooting.md")
        code = "OAH_LEASE_CONFLICT"
        self.assertIn(code, codes_in(text))
        copy = make_tmp_dir(self) / "oah"
        shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
        for path in copy.rglob("*.py"):
            original = read(path)
            if code in original:
                path.write_text(original.replace(code, "OAH_LEASE_RENAMED_FOR_TEST"), encoding="utf-8")
        self.assertEqual(missing_codes(text, copy), [code])

    def test_troubleshooting_pages_list_the_same_codes(self):
        english = codes_in(read(REPO / "docs/troubleshooting.md"))
        korean = codes_in(read(REPO / "docs/ko/troubleshooting.md"))
        self.assertEqual(sorted(english ^ korean), [])

    def test_codes_in_other_docs_exist(self):
        pending = tuple(prefix for prefix, module in PENDING_LANES.items() if importlib.util.find_spec(module) is None)
        for page in lane_docs():
            with self.subTest(page=str(page.relative_to(REPO))):
                missing = [code for code in missing_codes(read(page), SRC) if not code.startswith(pending or ("-",))]
                self.assertEqual(missing, [])


class AccuracyTest(unittest.TestCase):
    def test_no_fixed_effort_cap(self):
        for page in all_docs():
            for number, line in enumerate(read(page).splitlines(), 1):
                with self.subTest(page=str(page.relative_to(REPO)), line=number):
                    self.assertIsNone(FIXED_CAP.search(line), line)

    def test_fixed_cap_pattern_catches_the_old_claim(self):
        for claim in ("Note: max is rejected for Codex.", "The effort is capped at xhigh.", "xhigh is the highest"):
            self.assertIsNotNone(FIXED_CAP.search(claim), claim)
        self.assertIsNone(FIXED_CAP.search("Effort is what the model supports, with no fixed cap."))

    def test_every_page_has_a_korean_twin(self):
        for page in PAGES:
            with self.subTest(page=page):
                self.assertTrue((REPO / "docs" / f"{page}.md").is_file())
                self.assertTrue((REPO / "docs" / "ko" / f"{page}.md").is_file())
        self.assertTrue((EXAMPLE / "README.md").is_file())
        self.assertTrue((EXAMPLE / "README.ko.md").is_file())

    def test_relative_links_resolve(self):
        pages = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md")), *sorted((REPO / "docs/ko").glob("*.md")),
                 EXAMPLE / "README.md", EXAMPLE / "README.ko.md"]
        for page in pages:
            for number, line in prose_lines(read(page)):
                for target in LINK.findall(line):
                    if re.match(r"[a-z]+:", target) or target.startswith("#"):
                        continue
                    path = target.split("#", 1)[0]
                    if path.startswith("../../actions"):
                        continue  # GitHub's own pages for the CI badge
                    with self.subTest(page=str(page.relative_to(REPO)), line=number, target=target):
                        self.assertTrue((page.parent / path).exists(), f"{target} does not exist")

    def test_korean_sentences_do_not_end_with_a_colon(self):
        pages = [REPO / "docs" / "ko" / f"{page}.md" for page in PAGES] + [EXAMPLE / "README.ko.md"]
        korean_readme = read(REPO / "README.md").split("## 한국어", 1)[1]
        for name, text in [(str(p.relative_to(REPO)), read(p)) for p in pages] + [("README.md (한국어)", korean_readme)]:
            for number, line in prose_lines(text):
                if line.lstrip().startswith("|"):
                    continue
                with self.subTest(page=name, line=number):
                    self.assertIsNone(HANGUL_COLON.search(line), line)

    def test_keys_page_names_the_lookup(self):
        for page in (REPO / "docs/keys.md", REPO / "docs/ko/keys.md"):
            text = read(page)
            for words in ("TYPESAFE_API_KEY", "unused(no key)", "codex login --with-api-key", "oah:<NAME>"):
                with self.subTest(page=str(page.relative_to(REPO)), words=words):
                    self.assertIn(words, text)


if __name__ == "__main__":
    unittest.main()
