# 문서 검사 — 문제 해결 문서의 OAH_* 코드가 src/oah 에 실제로 있는지, 고정 effort 상한 주장·깨진 링크·한국어 쌍·콜론 종결이 없는지, README 언어 전환 줄·앵커·배너 SVG 규칙을 본다.
from __future__ import annotations

import importlib.util
from html.parser import HTMLParser
import pathlib
import re
import shutil
import unittest
import xml.etree.ElementTree as ET

from tests.helpers import make_tmp_dir
from oah.toolkit import update

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
# P7m extends an existing module; keep its one new diagnostic pending only until its notice reader lands.
PENDING_CODES = {"OAH_MCP_UPDATE_AVAILABLE"} if not hasattr(update, "last_mcp_notices") else set()
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
    return [REPO / "README.md", REPO / "README.ko.md", *pages, EXAMPLE / "README.md", EXAMPLE / "README.ko.md"]


def all_docs() -> list[pathlib.Path]:
    docs = [REPO / "README.md", REPO / "README.ko.md", REPO / "CONTRIBUTING.md", *sorted((REPO / "docs").rglob("*.md")),
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


READMES = {"README.md": ("English", "한국어"), "README.ko.md": ("한국어", "English")}
BANNERS = {"README.md": "banner-", "README.ko.md": "banner-ko-"}
KOREAN_FONTS = ('"Apple SD Gothic Neo"', '"Malgun Gothic"', '"Noto Sans KR"')
SVG = "{http://www.w3.org/2000/svg}"


def heading_anchors(text: str) -> set[str]:
    """GitHub's anchors for the headings outside fenced code: lowercase, punctuation dropped, spaces to hyphens."""
    anchors: set[str] = set()
    for _, line in prose_lines(text):
        match = re.match(r"#{1,6} +(.+?) *#*$", line)
        if not match:
            continue
        slug = re.sub(r"[^\w\- ]", "", match.group(1).lower()).replace(" ", "-")
        anchor, n = slug, 0
        while anchor in anchors:
            n += 1
            anchor = f"{slug}-{n}"
        anchors.add(anchor)
    return anchors


def missing_anchors(page: pathlib.Path, text: str | None = None) -> list[str]:
    """Links to #anchors in the same page, or in a README beside it, that no heading there produces."""
    text = read(page) if text is None else text
    missing = []
    for _, line in prose_lines(text):
        for target in LINK.findall(line):
            path, _, anchor = target.partition("#")
            if not anchor or re.match(r"[a-z]+:", target):
                continue
            if path and path not in READMES:
                continue
            other = text if not path else read(page.parent / path)
            if anchor not in heading_anchors(other):
                missing.append(target)
    return missing


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
                missing = [code for code in missing_codes(read(page), SRC)
                           if code not in PENDING_CODES and not code.startswith(pending or ("-",))]
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
        pages = [REPO / "README.md", REPO / "README.ko.md", *sorted((REPO / "docs").glob("*.md")),
                 *sorted((REPO / "docs/ko").glob("*.md")),
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
        pages = [REPO / "docs" / "ko" / f"{page}.md" for page in PAGES]
        pages += [EXAMPLE / "README.ko.md", REPO / "README.ko.md"]
        for name, text in [(str(p.relative_to(REPO)), read(p)) for p in pages]:
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


class ReadmeLanguageTest(unittest.TestCase):
    def test_switch_row_sits_between_badges_and_title(self):
        for name, (current, other) in READMES.items():
            lines = read(REPO / name).splitlines()
            with self.subTest(page=name):
                title = lines.index("# open-agent-harness (`oah`)")
                badges_end = lines.index("</p>", max(i for i, line in enumerate(lines[:title]) if "badge" in line))
                between = [line for line in lines[badges_end + 1:title] if line.strip()]
                self.assertEqual(len(between), 1, between)
                row = between[0]
                self.assertTrue(row.startswith('<p align="center">') and row.endswith("</p>"), row)
                self.assertIn(f"<strong>{current}</strong>", row)
                self.assertNotIn(f">{current}</a>", row)
                self.assertIn(f'<a href="{"README.ko.md" if name == "README.md" else "README.md"}">{other}</a>', row)
                self.assertEqual(sum(">English<" in line or ">한국어<" in line for line in lines), 1)

    def test_readme_holds_one_language(self):
        english, korean = read(REPO / "README.md"), read(REPO / "README.ko.md")
        self.assertNotIn("## 한국어", english)
        self.assertIsNone(re.search(r"^#+ .*[가-힣]", english, re.MULTILINE))
        self.assertIn("## 이 프로젝트의 목적", korean)
        self.assertNotIn("## Purpose", korean)

    def test_same_file_anchors_resolve(self):
        for name in READMES:
            with self.subTest(page=name):
                self.assertEqual(missing_anchors(REPO / name), [])

    def test_anchor_check_catches_a_broken_anchor(self):
        text = "# Title\n\n## Install it\n\n[ok](#install-it) [bad](#nope) [en](README.md#nope)\n"
        self.assertEqual(missing_anchors(REPO / "README.ko.md", text), ["#nope", "README.md#nope"])
        self.assertEqual(heading_anchors("## v0.3.0: models\n## `oah` 로드맵\n## 개발\n## 개발\n"),
                         {"v030-models", "oah-로드맵", "개발", "개발-1"})


class BannerTest(unittest.TestCase):
    def banner_files(self) -> list[pathlib.Path]:
        return [REPO / "docs" / "assets" / f"{prefix}{theme}.svg"
                for prefix in BANNERS.values() for theme in ("light", "dark")]

    def test_banner_files_are_self_contained(self):
        forbidden = re.compile(r"<script|<foreignObject|<image|@import|@font-face|url\((?!#)|href=[\"'](?!#)",
                               re.IGNORECASE)
        for path in self.banner_files():
            with self.subTest(banner=path.name):
                raw = path.read_bytes()
                self.assertLess(len(raw), 12 * 1024)
                text = raw.decode("utf-8")
                self.assertIsNone(forbidden.search(text))
                self.assertNotIn("bounded", text.lower())
                root = ET.fromstring(text)
                self.assertTrue(root.findtext(f"{SVG}title"))
                self.assertTrue(root.findtext(f"{SVG}desc"))
                self.assertGreaterEqual(len(root.findall(f".//{SVG}text")), 10)

    def test_korean_banners_name_korean_fonts_first(self):
        for path in self.banner_files():
            if "-ko-" not in path.name:
                continue
            with self.subTest(banner=path.name):
                family = re.search(r"font-family: ([^;]+);", read(path)).group(1)
                positions = [family.find(font) for font in KOREAN_FONTS]
                self.assertTrue(all(0 <= p < family.rindex("sans-serif") for p in positions), family)

    def test_banner_text_is_legible_at_phone_width(self):
        for path in self.banner_files():
            with self.subTest(banner=path.name):
                root = ET.parse(path).getroot()
                width = float(root.attrib["viewBox"].split()[2])
                styles = "\n".join(node.text or "" for node in root.findall(f".//{SVG}style"))
                classes = {}
                for selector, declarations in re.findall(r"([^{}]+)\{([^{}]+)\}", styles):
                    size = re.search(r"font-size\s*:\s*([\d.]+)px\s*(?:;|$)", declarations)
                    if size:
                        self.assertRegex(selector.strip(), r"^\.[\w-]+$")
                        classes[selector.strip()[1:]] = float(size.group(1))
                for node in root.findall(f".//{SVG}text"):
                    sizes = [classes[c] for c in node.get("class", "").split() if c in classes]
                    if "font-size" in node.attrib:
                        sizes.append(float(node.attrib["font-size"].removesuffix("px")))
                    inline = node.get("style", "")
                    if "font-size" in inline:
                        match = re.search(r"font-size\s*:\s*([\d.]+)px", inline)
                        self.assertIsNotNone(match)
                        sizes.append(float(match.group(1)))
                    self.assertTrue(sizes, ET.tostring(node, encoding="unicode"))
                    for size in sizes:
                        self.assertGreaterEqual(size, 10 * width / 360)

    def test_banner_text_fits_its_rectangles(self):
        for path in self.banner_files():
            with self.subTest(banner=path.name):
                root = ET.parse(path).getroot()
                styles = "\n".join(node.text or "" for node in root.findall(f".//{SVG}style"))
                self.assertRegex(styles, r"text-anchor\s*:\s*middle\s*;")
                sizes = {selector.strip()[1:]: float(size)
                         for selector, size in re.findall(
                             r"(\.[\w-]+)\s*\{[^{}]*font-size\s*:\s*([\d.]+)px", styles)}
                rectangles = []
                for rect in root.findall(f".//{SVG}rect"):
                    x, y, width, height = (float(rect.get(key, "0"))
                                           for key in ("x", "y", "width", "height"))
                    rectangles.append((x, y, width, height))
                groups = {}
                for node in root.findall(f".//{SVG}text"):
                    x, y = float(node.attrib["x"]), float(node.attrib["y"])
                    size = sizes[node.attrib["class"]]
                    containing = [rect for rect in rectangles
                                  if rect[0] <= x <= rect[0] + rect[2]
                                  and rect[1] <= y <= rect[1] + rect[3]]
                    self.assertTrue(containing, ET.tostring(node, encoding="unicode"))
                    rect = min(containing, key=lambda r: r[2] * r[3])
                    rx, ry, width, height = rect
                    label = "".join(node.itertext())
                    estimated = size * sum(1.0 if "가" <= char <= "힣" else 0.62
                                           for char in label)
                    self.assertGreaterEqual(x - estimated / 2, rx + 16, label)
                    self.assertLessEqual(x + estimated / 2, rx + width - 16, label)
                    self.assertGreaterEqual(y - ry, 0.9 * size, label)
                    self.assertGreaterEqual(ry + height - y, 0.35 * size, label)
                    groups.setdefault(rect, []).append((y, size, label))
                    if node.get("class") == "rail":
                        self.assertGreaterEqual(height, 1.5 * size, label)
                canvas = max(rectangles, key=lambda r: r[2] * r[3])
                for rect, labels in groups.items():
                    if rect == canvas:  # The background is not a text box.
                        continue
                    labels.sort()
                    for previous, current in zip(labels, labels[1:]):
                        self.assertGreaterEqual(current[0] - previous[0],
                                                1.2 * max(previous[1], current[1]),
                                                (previous[2], current[2]))

    def test_readmes_pick_banner_by_theme_with_alt_matching_desc(self):
        class PictureParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.pictures = []
                self.current = None
                self.badge_seen = False
                self.picture_before_badges = False

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "picture":
                    if self.current is not None:
                        self.current.append(("nested-picture", {}))
                    else:
                        self.current = []
                        self.picture_before_badges = not self.badge_seen
                elif self.current is not None:
                    self.current.append((tag, attrs))
                if "badge-license.svg" in attrs.get("src", ""):
                    self.badge_seen = True

            def handle_endtag(self, tag):
                if tag == "picture" and self.current is not None:
                    self.pictures.append(self.current)
                    self.current = None
                elif self.current is not None:
                    self.current.append((f"/{tag}", {}))

        for name, prefix in BANNERS.items():
            with self.subTest(page=name):
                parser = PictureParser()
                parser.feed(read(REPO / name))
                self.assertIsNone(parser.current, "unclosed picture")
                self.assertEqual(len(parser.pictures), 1)
                self.assertTrue(parser.picture_before_badges)
                picture = parser.pictures[0]
                self.assertEqual(len(picture), 2)
                self.assertEqual(picture[0], ("source", {
                    "media": "(prefers-color-scheme: dark)",
                    "srcset": f"docs/assets/{prefix}dark.svg"}))
                tag, attrs = picture[1]
                self.assertEqual(tag, "img")
                self.assertEqual(set(attrs), {"src", "alt", "width"})
                self.assertEqual(attrs["src"], f"docs/assets/{prefix}light.svg")
                self.assertEqual(attrs["width"], "100%")
                self.assertTrue(attrs["alt"])
                for theme in ("light", "dark"):
                    root = ET.parse(REPO / "docs" / "assets" / f"{prefix}{theme}.svg").getroot()
                    self.assertEqual(root.findtext(f"{SVG}title"), "open-agent-harness")
                    self.assertEqual(root.findtext(f"{SVG}desc"), attrs["alt"])


if __name__ == "__main__":
    unittest.main()
