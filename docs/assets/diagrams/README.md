# Editable diagrams

These HTML sources adapt the minimal light/dark templates and styles from
[diagram-design](https://github.com/cathrynlavery/diagram-design/tree/3eb03b8), **MIT, commit 3eb03b8**.
Copyright (c) 2025 Cathryn Lavery. The full license is in
[THIRD_PARTY_NOTICES.md](../../../THIRD_PARTY_NOTICES.md#diagram-design).
No bundled icon set is used and the skill itself is not vendored here.

Each stem below has `-light.html`, `-dark.html`, `-ko-light.html` and `-ko-dark.html` sources.
The matching SVGs live one directory above. Edit HTML first; SVGs are exports, not separate sources.

| Stem | Skill type and size (`fit`) | Used by |
|---|---|---|
| `banner` | Flowchart, 640 × 352 | English and Korean README banners |
| `how-it-works` | Flowchart, 640 × 544 | Both READMEs and both how-it-works pages, sharing assets |
| `architecture-build` | Architecture, 840 × 848 | How-it-works pages, task preparation and the four rails |
| `architecture-land` | Architecture, 840 × 800 | How-it-works pages, review and landing |
| `loop-start` | Sequence, 720 × 504 | How-it-works pages, exchanges 1–6 |
| `loop-review` | Sequence, 840 × 712 | How-it-works pages, exchanges 7–15 |

[The style guide](style-guide.md) records the oah Primer palette, role colors, system font stacks,
type ramp, spacing and connector rules for later diagrams. Each source applies those tokens directly,
so its HTML and SVG work offline without a shared stylesheet. Light/dark twins carry identical content
and geometry; Korean twins carry the same meaning with Korean system fonts first.

## Regenerate

Use a local read-only copy of the skill at the pinned commit. Set `DIAGRAM_DESIGN` to its
`skills/diagram-design` directory, then run from the repository root. The scripts use only the Python
standard library. No installation or download is part of regeneration.

```sh
python3 -I "$DIAGRAM_DESIGN/scripts/self_check.py" --offline docs/assets/diagrams/*.html
for source in docs/assets/diagrams/*.html; do
  python3 -I "$DIAGRAM_DESIGN/scripts/export_svg.py" "$source" \
    "docs/assets/$(basename "$source" .html).svg" --system-fonts || break
done
```

Keep each page's image alt text identical to both exported `<desc>` elements. Every SVG needs a
first-child `<title>`, a `<desc>` and prefixed accessibility IDs. Use `<picture>` with a dark source
and a light fallback. Do not add scripts, external images, fonts, links or CSS imports.

The self-check checks document completeness, accessibility, offline safety and canvas bounds. It does
not replace visual review of text. On macOS, where the sandbox permits it, render each SVG with
`qlmanage -t -s 1600 -o .tmp/rdp-previews <svg>` after creating that preview directory. Inspect both
themes and languages for clipping, overlap and legibility. Keep all previews in that ignored directory.

## Redraw fidelity

The old README architecture Mermaid diagram had 14 components, four containers and 16 arrows.
Two Architecture figures redraw all 14 components and all 16 directed connections for a mixed audience.
`architecture-build` has nine nodes and eight arrows, including optional TypeSafe Jev and the four rails
(leases, gates, impact map and wiki). `architecture-land` has seven nodes and eight arrows. Gates and
the impact map repeat across the figures to join them; no component or connection is dropped or merged.
The four original groups (oah, installed AI engines, optional judge, git/GitHub) keep their membership,
with at most three zones in either figure. Detailed edge labels are kept as short connector labels and
node explanations; dashed arrows retain the optional paths. The parts table keeps the full wording.
The separate four-step Flowchart remains the entry-page overview, with a small optional Jev annotation.

The old English sequence Mermaid diagram had five actors and 15 messages. Two Sequence figures now
carry all 15 numbered exchanges, with three actors in the first and four in the second (You and oah
appear in both). Full message text moved into the adjacent numbered tables. The loop fragment and
return directions remain; no exchange was dropped. Korean now has the same figures and tables.

The banners keep the default build/review/confirmation story. Their former dense supporting labels
(Jev, leases, gates, impact map, wiki and optional review limit) now live in the page text.
