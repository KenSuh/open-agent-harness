# oah diagram style guide

Custom tokens for [diagram-design](https://github.com/cathrynlavery/diagram-design) at **3eb03b8**.
The project's supplied tokens override the upstream skin; the upstream copy stays read-only.
Font source: **system**. No network resources, icons, animation or shadows.

| Role | Light | Dark |
|---|---|---|
| paper | `#ffffff` | `#0d1117` |
| paper-2 / panel | `#f6f8fa` | `#161b22` |
| rule / rule-solid | `#d0d7de` | `#30363d` |
| ink | `#1f2328` | `#f0f6fc` |
| ink-2 / secondary text / soft | `#424a53` | `#c9d1d9` |
| muted / connectors | `#6e7781` | `#8b949e` |
| Claude Code | `#9a6700` | `#d29922` |
| Codex CLI / default accent | `#1a7f37` | `#3fb950` |
| Jev | `#8250df` | `#a371f7` |
| landing / link | `#0969da` | `#58a6ff` |

Use role colors only on the one or two focal nodes. Other nodes use paper, panel and ink.
Use opaque panel fill under accent strokes; no extra tint is needed. Ink is the strong text color.
Rules are decorative; muted strokes carry connections. Secondary text uses ink-2 so it stays legible
on both paper and panel. Label every role in words as well as color.

All prose and titles use `system-ui, -apple-system, "Segoe UI", sans-serif`.
Korean sources put `"Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", "Noto Sans CJK KR"`
first. Technical labels use `ui-monospace, "SFMono-Regular", Consolas, monospace`.
There is no downloaded or embedded font. Platform fonts may have different metrics.

The `fit` preset uses the standard type ramp: 28px title, 14px group heading, 12px node name,
and 8px technical arrow label. Plain explanatory labels use 12px sans. Korean arrow labels use
12px sans at weight 500, without tracking. Keep generous padding; budget a full em per wide glyph
and 0.62em per other glyph before checking a rendered preview.

Aim for density 4/10, a 4px grid, 40px outer margins and a 60px footer strip.
Use 6px node corners and oval flowchart endpoints. Flowcharts run down the page.
Sequence diagrams put actors above dashed lifelines and time runs downwards; requests are solid,
returns dashed. Numbered exchanges refer to the tables in the accompanying page.
Arrows precede nodes in the markup. Labels have opaque masks with an 8px gap from their arrow.
Any elbow added later must have an 8px radius. Do not overlap routes or labels.

Keep the upstream limits of nine nodes, twelve arrows, two focal accents and two callouts per figure.
Delete decoration before shrinking text. Split a long sequence rather than making its labels smaller.
HTML keeps a local horizontal scroller with SVG min-width equal to its viewBox; print releases it.
GitHub uses the exported SVG as a responsive image, with the full explanation also in Markdown.
