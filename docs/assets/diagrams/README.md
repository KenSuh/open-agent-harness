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
| `how-it-works` | Flowchart, 640 × 480 | Both READMEs and both how-it-works pages, sharing assets |
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
The mixed-audience overview now groups them into four steps: describe, build, review/correct, confirm/land.
This intentionally uses Flowchart instead of Architecture: the entry page teaches the work order.
Leases/preflight/routing are grouped into preparation; gates/wiki/impact into evidence;
git hooks/GitHub/CI into landing. Jev stays in the adjacent explanation. The complete component
relationships, optional paths, commands and limitations remain in the how-it-works tables and prose.
The original per-tool boxes and container styling are not carried over.

The old English sequence Mermaid diagram had five actors and 15 messages. Two Sequence figures now
carry all 15 numbered exchanges, with three actors in the first and four in the second (You and oah
appear in both). Full message text moved into the adjacent numbered tables. The loop fragment and
return directions remain; no exchange was dropped. Korean now has the same figures and tables.

The banners keep the default build/review/confirmation story. Their former dense supporting labels
(Jev, leases, gates, impact map, wiki and optional review limit) now live in the page text.
