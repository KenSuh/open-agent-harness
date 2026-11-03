# oah diagram style guide

The owner selected **keep the default** for
[diagram-design](https://github.com/cathrynlavery/diagram-design/tree/3eb03b8), commit **3eb03b8**.
Use its editorial gallery skin, with the dotted-paper option and **Font source: system**.
The upstream skill stays read-only. The banner has a separate owner; this guide covers the other figures.

## Tokens

| Role | Light | Dark |
|---|---|---|
| paper | `#f5f5f5` | `#2d3142` |
| paper-2 | `#ececec` | `#393e53` |
| ink | `#2d3142` | `#f5f5f5` |
| ink-strong | `#111111` | `#111111` |
| muted | `#4f5d75` | `#bfc0c0` |
| soft | `#5a6580` | `#949eb2` |
| rule | `rgba(45,49,66,.12)` | `rgba(245,245,245,.12)` |
| rule-solid | `#bfc0c0` | `rgba(191,192,192,.25)` |
| accent | `#bf4520` | `#f08a59` |
| accent-tint | `rgba(191,69,32,.08)` | `rgba(240,138,89,.10)` |
| link | `#2e5aa8` | `#739fdf` |

Use one accent hue on one or two focal nodes. Claude Code, Codex CLI and TypeSafe Jev are
identified by their names and `CLAUDE`, `CODEX` and `JEV` tags; engine identity does not choose a color.
The link token is reserved for an explicitly external/API connection; these figures use muted arrows.
Keep text at full opacity. Translucent washes and rules are decorative, never the only carrier of meaning.

## Fonts and readable sizes

Keep the gallery's family stacks, falling back to installed system fonts. Load nothing from outside
the file: no Google Fonts, `@font-face`, CSS import, script or external image.

| Role | Stack | Size / weight |
|---|---|---|
| Figure title | `"Instrument Serif", Georgia, serif` | 28px / 400 |
| Names and prose | `"Geist", system-ui, -apple-system, "Segoe UI", sans-serif` | Names 16px / 600; detail 12px / 400 |
| Technical sublabels, commands, arrow labels | `"Geist Mono", ui-monospace, "SFMono-Regular", Consolas, monospace` | 12px / 400 |
| Latin tag chips and eyebrow | Same mono stack | 8px / 500 |
| Ghost numbers | Same mono stack | 32px / 600, opacity .06 |

Use a **fit** canvas with the skill's larger 16px/12px presentation text roles for readable nodes
and detail, and its 28px standard title. Grow the canvas instead of shrinking essential text.
Names and explanatory prose use sans; commands and technical values use mono. Tags and ghost
numbers are nested decorative groups, excluded from the concepts nodes' source-label text.

Korean extends the sans stack with `"Noto Sans KR", "Apple SD Gothic Neo", "Malgun Gothic"`
after Geist and before `system-ui, sans-serif`. Titles use
`"Instrument Serif", "Noto Serif KR", "Batang", serif`.
Hangul detail, arrow labels, group labels and legends use sans at **12px or larger**, with no tracking
or uppercase transform. Latin-only tags retain the 8px mono treatment. Use plain 해라체 for sentences;
do not end a Korean sentence with a colon.

Reserve 1em per wide/full-width character and .60em sans or .62em mono per other character.
Combining marks add no width. Wrap at word boundaries, preserve logical phrase breaks, and budget
for the wider language. Both languages and both themes share the same boxes, routes, masks and canvas.
Fallback metrics still need preview inspection.

## Components and layout

- Paper fills the entire canvas. Overlay a 22 × 22 dot grid: 0.9px ink dots at .10 opacity, with
  the pattern layer at .55 opacity. No shadows, glow or gradients.
- Use a 4px structural grid, 40px outer margins, 16px node padding, and generous gaps.
  Text baselines, dot spacing and marker geometry are the skill's documented off-grid exceptions.
- Draw background, zones, arrows, arrow masks, then nodes. End with a separate horizontal legend.
- Nodes have an opaque paper mask under their treatment. Standard steps use white on light paper
  and paper on dark paper; input nodes use muted at .10; stores use ink at .05; optional nodes
  use ink at .02 and a `4,3` dashed border; focal nodes use accent-tint and an accent border.
- Rectangular tags have 2px corners; ordinary nodes 6px; states and zones 8px; flow endpoints 20px.
  Decisions use diamonds. Lease start/end pseudostates retain their dot/ring shapes.
- Zone frames use ink at .02 with .12 rules. Keep their headings clear of arrows and at least
  16px above nodes. Zones preserve source membership; they are not new graph nodes.
- Use the skill's monochrome primitive icons sparingly, at 24 × 24. They supplement a written
  label. The icon attribution and license are in [README.md](README.md#icon-license).
- Ghost numbers indicate a local reading order, not extra commands. Sequence numbers retain
  the original 1–15 exchanges and their adjacent table references.
- Use at most nine nodes, twelve arrows, two focal accents and two callouts per figure.
  Keep faithful detail in the existing paired stems; do not merge or drop source nodes.

## Connections and fidelity

Use muted strokes with filled arrowheads, 1.2px connections, and rounded orthogonal elbows of
radius 8px. Off-axis diagonal connections, shared strokes, shared attach points and transit through
another node are not allowed. Keep ports at least 12px apart. A crossing needs a bridge; current
figures avoid crossings entirely.

An arrow label has an opaque paper mask, 8px clear of the line. Place it above a horizontal route
or beside a vertical one. Keep inline labels short; full conditions belong in numbered transition keys
inside the figure. Label masks must also clear nodes and zone headings. Dash meanings belong in
the legend: optional paths, returns, recorded CI exceptions, or a planned future note as appropriate.
Sequence returns retain dashed lines; their loop frame covers only participating actors.

For every concepts node retain `data-node-id`, `data-source-label` and all visible source words in
source order. Preserve `data-parent` group membership. For every connection retain `data-edge-id`,
`data-from`, `data-to` and `data-label`; every condition remains visible inline or in a key.
Keep the lease refusal notes. `tests.test_docs` guards the original graphs, so changing a label's
wrapping must not change its words.

Keep the architecture's 14 unique components and 16 connections across its two figures, and all
15 numbered sequence messages. The overview may summarize these details, but must show the whole
harness and both user touchpoints. The guided design conversation is **planned for v0.4.0**, never
shown as available. The features figure explains the six supporting parts together.

## Files and checks

Edit the standalone HTML first and export with the pinned skill's `export_svg.py --system-fonts`.
Every SVG has a first-child title, a one-sentence description, prefixed accessibility IDs, and
`role="img"`. Page alt text must equal the description in both themes.

HTML uses a local `overflow-x: auto` wrapper with SVG `min-width` equal to the viewBox width.
Release both constraints for print. GitHub uses responsive SVG images; Markdown supplies the fuller
explanation.

Run the skill's `self_check.py --offline` on every changed source, the relevant documentation tests,
and visual checks of all four variants. Quick Look can render previews under `.tmp/rdr-previews/`;
an installed offline SVG rasterizer is a fallback when Quick Look is unavailable. A raster preview
does not establish native rendering in all three browser platforms.
