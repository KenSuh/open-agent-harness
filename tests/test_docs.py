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
PAGES = ("concepts", "troubleshooting", "upgrade", "keys", "how-it-works", "commands")
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


ASSETS = REPO / "docs" / "assets"
CONCEPT_DIAGRAMS = (
    "concepts-loop", "concepts-leases-lifecycle", "concepts-leases-finish",
    "concepts-routing-rules", "concepts-routing-skills", "concepts-impact-freshness",
    "concepts-impact-ranked", "concepts-review", "concepts-landing-local", "concepts-landing-checks",
)
DIAGRAMS = ("banner", "how-it-works", "architecture-build", "architecture-land", "loop-start", "loop-review",
            *CONCEPT_DIAGRAMS)
DIAGRAM_PAGES = {
    "README.md": ("banner", "how-it-works"),
    "README.ko.md": ("banner-ko", "how-it-works-ko"),
    "docs/how-it-works.md": ("how-it-works", "architecture-build", "architecture-land", "loop-start", "loop-review"),
    "docs/ko/how-it-works.md": ("how-it-works-ko", "architecture-build-ko", "architecture-land-ko", "loop-start-ko", "loop-review-ko"),
    "docs/concepts.md": CONCEPT_DIAGRAMS,
    "docs/ko/concepts.md": tuple(f"{stem}-ko" for stem in CONCEPT_DIAGRAMS),
}

# Original Mermaid graphs, before the faithful redraw. Split figures repeat shared nodes,
# but every original edge appears exactly once, including differently guarded parallel edges.
CONCEPT_GRAPHS = (
    (("concepts-loop",), "P L C G R F N D",
     "P>L L>C C>G G>R R>F F>G R>N N>D"),
    (("concepts-leases-lifecycle", "concepts-leases-finish"),
     "Active Stale Finished Released finish_check __start_1 __end_1 __end_2 __end_3",
     "__start_1>Active Active>finish_check Stale>finish_check finish_check>Finished "
     "finish_check>Active finish_check>Stale Active>Released Stale>Released Active>Stale "
     "Stale>__end_1 Finished>__end_2 Released>__end_3 Active>Active"),
    (("concepts-routing-rules", "concepts-routing-skills"),
     "task score tier pick model skills ask jev used unused brief",
     "task>score score>tier tier>pick pick>model model>brief task>skills skills>ask "
     "ask>jev ask>unused ask>unused jev>used jev>unused used>brief unused>brief jev>pick"),
    (("concepts-impact-freshness", "concepts-impact-ranked"),
     "change fresh refresh stale dflt pinR execR linkR mentionR rerun",
     "change>fresh fresh>pinR fresh>refresh refresh>pinR fresh>stale dflt>pinR "
     "pinR>execR execR>linkR linkR>mentionR pinR>rerun execR>rerun"),
    (("concepts-review",), "R V F B D", "R>V R>F F>B B>R R>D"),
    (("concepts-landing-local", "concepts-landing-checks"),
     "gcommit pc post gpush pp cc pr tests secrets land exc landed",
     "gcommit>pc gcommit>post gpush>pp pp>secrets pp>pr pr>tests pr>secrets "
     "tests>land secrets>land land>exc land>landed"),
)


def svg_resource_errors(text: str) -> list[str]:
    """SVGs must be static, offline figures; fragment references to their own defs are allowed."""
    root = ET.fromstring(text)
    errors = []
    if re.search(r"@import|@font-face", text, re.IGNORECASE):
        errors.append("remote font or stylesheet directive")
    for value in re.findall(r"url\(\s*([^)]*?)\s*\)", text, re.IGNORECASE):
        value = value.strip().strip("\"'").strip()
        if not value.startswith("#") or len(value) == 1:
            errors.append(f"non-fragment CSS reference: {value}")
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "foreignobject", "image", "a"}:
            errors.append(f"active or embedded content: {tag}")
        for key, value in node.attrib.items():
            name = key.rsplit("}", 1)[-1].lower()
            if name.startswith("on") or name == "base":
                errors.append(f"executable attribute or changed base: {key}")
            if name in {"href", "src"} and (not value.startswith("#") or len(value) == 1):
                errors.append(f"non-fragment reference: {value}")
    return errors


class PictureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pictures = []
        self.current = None
        self.outside_images = []
        self.badge_seen = False
        self.banner_before_badges = False
        self.nested = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "picture":
            if self.current is not None:
                self.nested = True
            else:
                self.current = []
                if not self.pictures:
                    self.banner_before_badges = not self.badge_seen
        elif self.current is not None:
            self.current.append((tag, attrs))
        elif tag == "img":
            self.outside_images.append(attrs)
        if "badge-license.svg" in attrs.get("src", ""):
            self.badge_seen = True

    def handle_endtag(self, tag):
        if tag == "picture" and self.current is not None:
            self.pictures.append(self.current)
            self.current = None


class BannerTest(unittest.TestCase):
    """The public image contract, independent of any particular banner layout or animation."""

    def diagram_files(self) -> list[pathlib.Path]:
        return sorted(path for path in ASSETS.glob("*.svg") if not path.name.startswith("badge-"))

    def test_all_diagrams_have_both_themes_and_languages_and_sources(self):
        files = self.diagram_files()
        self.assertTrue(files)
        families = set()
        for path in files:
            match = re.fullmatch(r"(.+?)(?:-ko)?-(light|dark)\.svg", path.name)
            self.assertIsNotNone(match, path.name)
            families.add(match.group(1))
        self.assertTrue(set(DIAGRAMS) <= families)
        for family in families:
            for language in ("", "-ko"):
                for theme in ("light", "dark"):
                    stem = f"{family}{language}-{theme}"
                    with self.subTest(asset=stem):
                        self.assertTrue((ASSETS / f"{stem}.svg").is_file())
                        self.assertTrue((ASSETS / "diagrams" / f"{stem}.html").is_file())

    def test_all_diagrams_are_static_and_self_contained(self):
        for path in self.diagram_files():
            with self.subTest(asset=path.name):
                self.assertEqual(svg_resource_errors(read(path)), [])
                root = ET.parse(path).getroot()
                ids = {node.get("id") for node in root.iter() if node.get("id")}
                for reference in re.findall(r"url\(#([^)]+)\)", read(path)):
                    self.assertIn(reference, ids, "fragment must resolve within this SVG")

    def test_resource_check_rejects_remote_or_active_content(self):
        for fragment in (
            '<script>alert(1)</script>',
            '<style>@import "https://example.invalid/font.css";</style>',
            '<style>@font-face { font-family: remote; }</style>',
            '<style>.node { fill: url("https://example.invalid/a.svg"); }</style>',
            '<use href="https://example.invalid/a.svg#shape"/>',
            '<use xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="a.svg#shape"/>',
            '<image href="data:image/png;base64,abc"/>',
            '<svg onload="alert(1)"/>',
        ):
            with self.subTest(fragment=fragment):
                self.assertTrue(svg_resource_errors(f'<svg xmlns="http://www.w3.org/2000/svg">{fragment}</svg>'))
        self.assertEqual(svg_resource_errors(
            '<svg xmlns="http://www.w3.org/2000/svg"><use href="#shape"/>'
            '<style>.node { fill: url( "#shape" ); }</style></svg>'), [])

    def test_diagrams_have_accessible_titles_and_descriptions(self):
        for path in self.diagram_files():
            with self.subTest(asset=path.name):
                root = ET.parse(path).getroot()
                self.assertEqual(root.tag, f"{SVG}svg")
                self.assertEqual(root.get("role"), "img")
                self.assertEqual(list(root)[0].tag, f"{SVG}title")
                title, desc = root.find(f"{SVG}title"), root.find(f"{SVG}desc")
                self.assertIsNotNone(title)
                self.assertIsNotNone(desc)
                self.assertTrue(title.text and title.text.strip())
                self.assertTrue(desc.text and desc.text.strip())
                self.assertEqual(title.get("id"), f"{path.stem}-title")
                self.assertEqual(desc.get("id"), f"{path.stem}-desc")
                self.assertEqual(root.get("aria-labelledby", "").split(), [title.get("id"), desc.get("id")])
                ids = [node.get("id") for node in root.iter() if node.get("id")]
                self.assertEqual(len(ids), len(set(ids)))
                viewbox = [float(number) for number in root.get("viewBox", "").split()]
                self.assertEqual(len(viewbox), 4)
                self.assertGreater(viewbox[2], 0)
                self.assertGreater(viewbox[3], 0)

    def test_themes_have_identical_text_and_layout(self):
        for light in self.diagram_files():
            if not light.name.endswith("-light.svg"):
                continue
            with self.subTest(asset=light.name):
                dark = light.with_name(light.name.replace("-light.svg", "-dark.svg"))
                roots = [ET.parse(path).getroot() for path in (light, dark)]
                self.assertEqual(roots[0].get("viewBox"), roots[1].get("viewBox"))
                def content(root):
                    return [(node.tag, "".join(node.itertext()), node.get("x"), node.get("y"))
                            for node in root.iter() if node.tag in {f"{SVG}title", f"{SVG}desc", f"{SVG}text"}]
                self.assertEqual(content(roots[0]), content(roots[1]))
                def geometry(root):
                    return [(node.tag, {k: v for k, v in node.attrib.items()
                                        if k in {"x", "y", "x1", "y1", "x2", "y2", "width", "height", "rx", "d", "points"}})
                            for node in root.iter() if node.tag in {f"{SVG}rect", f"{SVG}line", f"{SVG}path", f"{SVG}polygon"}]
                self.assertEqual(geometry(roots[0]), geometry(roots[1]))

    def test_korean_diagrams_name_korean_system_fonts_first(self):
        for path in self.diagram_files():
            if "-ko-" not in path.name:
                continue
            with self.subTest(asset=path.name):
                root = ET.parse(path).getroot()
                css = "\n".join(node.text or "" for node in root.findall(f".//{SVG}style"))
                family = re.search(r"font-family:\s*([^;]+);", css)
                self.assertIsNotNone(family)
                stack = family.group(1)
                self.assertTrue(stack.startswith(KOREAN_FONTS[0]), stack)
                positions = [stack.find(font) for font in KOREAN_FONTS]
                self.assertTrue(all(0 <= pos < stack.index("system-ui") for pos in positions), stack)

    def test_concepts_preserve_the_source_graph_and_visible_node_labels(self):
        for stems, expected_nodes, expected_edges in CONCEPT_GRAPHS:
            for language in ("", "-ko"):
                for theme in ("light", "dark"):
                    nodes, edges = set(), []
                    for stem in stems:
                        path = ASSETS / f"{stem}{language}-{theme}.svg"
                        with self.subTest(asset=path.name):
                            root = ET.parse(path).getroot()
                            drawn_nodes = [n for n in root.iter() if n.get("data-node-id")]
                            drawn_edges = [n for n in root.iter() if n.get("data-edge-id")]
                            self.assertLessEqual(len(drawn_nodes), 9)
                            self.assertLessEqual(len(drawn_edges), 12)
                            self.assertEqual(len({n.get("data-node-id") for n in drawn_nodes}), len(drawn_nodes))
                            for node in drawn_nodes:
                                key = node.get("data-node-id")
                                nodes.add(key)
                                if key.startswith("__"):
                                    self.assertIsNotNone(node.find(f"{SVG}circle"))
                                    continue  # Mermaid's start/end pseudostates are dots.
                                visible = " ".join("".join(t.itertext()) for t in node.findall(f"{SVG}text"))
                                self.assertEqual(visible.split(), node.get("data-source-label", "").split())
                            for edge in drawn_edges:
                                self.assertIn(edge.tag, {f"{SVG}line", f"{SVG}path"})
                                self.assertTrue(edge.get("marker-end"))
                                edges.append(f'{edge.get("data-from")}>{edge.get("data-to")}')
                    with self.subTest(stems=stems, language=language, theme=theme):
                        self.assertEqual(nodes, set(expected_nodes.split()))
                        self.assertEqual(sorted(edges), sorted(expected_edges.split()))

    def test_architecture_judgement_and_english_loop_label(self):
        for language in ("", "-ko"):
            for theme in ("light", "dark"):
                path = ASSETS / f"architecture-build{language}-{theme}.svg"
                with self.subTest(asset=path.name):
                    root = ET.parse(path).getroot()
                    labels = " ".join("".join(t.itertext()) for t in root.findall(f".//{SVG}text"))
                    self.assertIn("권장 선택형 AI 판정기" if language else "Recommended, optional AI judge", labels)
                    self.assertIn("작업의 난이도를 판정한다" if language else "Judges task difficulty", labels)
                    self.assertIn("Jev가 최저 기준 안에서" if language else "Jev can adjust tier within its floor", labels)
        for theme in ("light", "dark"):
            path = ASSETS / f"loop-review-{theme}.svg"
            with self.subTest(asset=path.name):
                root = ET.parse(path).getroot()
                loop = [t for t in root.findall(f".//{SVG}text") if t.text == "LOOP"]
                self.assertEqual(len(loop), 1)
                self.assertEqual(loop[0].get("class"), "label")
                self.assertRegex(read(path), r"\.label\s*\{\s*font-size:\s*12px;")

    def test_pages_pick_every_diagram_by_theme_with_alt_matching_desc(self):
        for name, stems in DIAGRAM_PAGES.items():
            page = REPO / name
            with self.subTest(page=name):
                parser = PictureParser()
                parser.feed(read(page))
                self.assertIsNone(parser.current, "unclosed picture")
                self.assertFalse(parser.nested)
                self.assertEqual(len(parser.pictures), len(stems))
                if name in READMES:
                    self.assertTrue(parser.badge_seen)
                    self.assertTrue(parser.banner_before_badges)
                for attrs in parser.outside_images:
                    self.assertIn("badge", attrs.get("src", ""), "a diagram bypasses theme selection")
                for picture, stem in zip(parser.pictures, stems):
                    self.assertEqual([tag for tag, _ in picture], ["source", "img"])
                    source, fallback = (attrs for _, attrs in picture)
                    self.assertEqual(source.get("media"), "(prefers-color-scheme: dark)")
                    self.assertEqual((page.parent / source["srcset"]).resolve(), ASSETS / f"{stem}-dark.svg")
                    self.assertEqual((page.parent / fallback["src"]).resolve(), ASSETS / f"{stem}-light.svg")
                    self.assertTrue(fallback.get("alt"))
                    for theme in ("light", "dark"):
                        root = ET.parse(ASSETS / f"{stem}-{theme}.svg").getroot()
                        self.assertEqual(root.findtext(f"{SVG}desc"), fallback["alt"])

    def test_readmes_and_new_pages_have_no_mermaid_and_all_anchors_resolve(self):
        names = [*DIAGRAM_PAGES, "docs/commands.md", "docs/ko/commands.md"]
        for name in names:
            page = REPO / name
            self.assertNotRegex(read(page), r"```\s*mermaid")
            for _, line in prose_lines(read(page)):
                for target in LINK.findall(line):
                    path, _, anchor = target.partition("#")
                    if not anchor or re.match(r"[a-z]+:", target):
                        continue
                    other = page.parent / path if path else page
                    with self.subTest(page=name, target=target):
                        self.assertTrue(other.is_file())
                        self.assertIn(anchor, heading_anchors(read(other)))


if __name__ == "__main__":
    unittest.main()
