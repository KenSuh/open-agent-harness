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
| `concepts-loop` | Flowchart, 760 × 952 | Both concepts pages, The loop / 전체 흐름 |
| `concepts-leases-lifecycle` | State machine, 880 × 1120 | Both concepts pages, Leases / 리스, part 1 |
| `concepts-leases-finish` | State machine, 880 × 968 | Both concepts pages, Leases / 리스, part 2 |
| `concepts-routing-rules` | Flowchart, 840 × 1056 | Both concepts pages, Routing and difficulty / 라우팅과 난이도, part 1 |
| `concepts-routing-skills` | Flowchart, 880 × 1252 | Both concepts pages, Routing and difficulty / 라우팅과 난이도, part 2 |
| `concepts-impact-freshness` | Flowchart, 880 × 1104 | Both concepts pages, The impact map / 영향 지도, part 1 |
| `concepts-impact-ranked` | Flowchart, 880 × 852 | Both concepts pages, The impact map / 영향 지도, part 2 |
| `concepts-review` | Flowchart, 880 × 824 | Both concepts pages, The review loop and corrections / 리뷰 루프와 수정 |
| `concepts-landing-local` | Flowchart, 880 × 956 | Both concepts pages, Landing / 반영, part 1 |
| `concepts-landing-checks` | Flowchart, 880 × 1136 | Both concepts pages, Landing / 반영, part 2 |

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

### Concepts pages

The six Mermaid blocks on each concepts page are faithful imports: editable HTML plus exported SVG,
`fit` size, full source detail, mixed audience with CLI commands and diagnostics spelled exactly as in
the source. Flowcharts use the top-to-bottom convention; leases use ordinary State Machine for dense
transition guards, rather than the simplified lifecycle-phase pattern. The skill's import, semantic
patterns, Flowchart, State Machine, output, layout, primitives and export references guide the redraw.
No source node, edge, condition, note or container membership is removed or merged. Colors and renderer
layout are replaced by the project style. Long edge labels use short numbered tags and full transition
keys inside the figure. Node labels retain all words, with line wrapping for the available space.

The counts below apply separately to English and Korean. Nodes exclude group frames and include the
lease start dot and three separate end dots. Counts are the same in both themes. Repeated nodes join
split figures; every original edge is drawn exactly once across the pair. Every figure stays below
nine nodes and twelve arrows. The three original groups remain: ranked results, local opt-in hooks,
and this repository's GitHub Actions. The local hooks node for Claude remains unconnected.

| Source heading (English / Korean) | Source nodes / edges | Figures: nodes / edges | Repeated nodes; fidelity |
|---|---|---|---|
| The loop / 전체 흐름 | 8 / 8 | `concepts-loop`: 8 / 8 | None; correction returns to gates and wiki, convergence leads to finish and confirmed landing |
| Leases / 리스 | 9 / 13 | `concepts-leases-lifecycle`: 6 / 7; `concepts-leases-finish`: 5 / 6 | Active and STALE; 9 unique nodes, all 13 transitions and the full note retained |
| Routing and difficulty / 라우팅과 난이도 | 11 / 15 | `concepts-routing-rules`: 7 / 6; `concepts-routing-skills`: 7 / 9 | Task, Jev and brief; 11 unique nodes, all 15 connections retained |
| The impact map / 영향 지도 | 10 / 11 | `concepts-impact-freshness`: 6 / 6; `concepts-impact-ranked`: 5 / 5 | pin; 10 unique nodes, all 11 connections and ranked membership retained |
| The review loop and corrections / 리뷰 루프와 수정 | 5 / 5 | `concepts-review`: 5 / 5 | None; all three review outcomes and the correction/bind return retained |
| Landing / 반영 | 12 / 11 | `concepts-landing-local`: 8 / 5; `concepts-landing-checks`: 6 / 6 | Pull request and secrets; 12 unique nodes, all 11 connections and both groups retained |

Connection ledger (the IDs are those of the original Mermaid and the HTML's `data-node-id` attributes):

- **Loop:** P → L → C → G → R; R → F on changes requested; F → G; R → N on convergence; N → D.
- **Leases, lifecycle:** start → Active; Active → Active on renew, start with a raised packet Revision,
  or log; Active → Stale on TTL expiry (four hours by default); Active → Released and Stale → Released
  on release with reason; Stale → its end on pruning by the next start; Released → its end.
  The note retains read-only status, overlap and own-task start refusals, expired renew refusal, and
  the force requirement and refusal for releasing another agent's lease.
- **Leases, finish:** Active → finish_check and Stale → finish_check on finish; finish_check → Finished
  when return, wiki, gates and scope pass or recorded allow flags apply; finish_check → Active and
  finish_check → Stale on their respective refusals; Finished → its end. All four finish refusal codes
  remain in the note. The original single note is divided by subject between the two figures.
- **Routing, rules:** task → score → tier → pick → model → brief; Jev → pick stays dashed and keeps
  the `difficulty_min_confidence` condition and tier floor. Explicit axes, all four rule axes, critical
  risk, migration/security floors, tier sources, unobservable effort and downgraded-effort failure remain.
- **Routing, skills:** task → skills → ask; ask → Jev for enabled/configured reranking with candidates;
  ask → unused has two distinct arrows for yes without candidates and no; Jev → used on scoring success;
  Jev → unused on error with original order kept; used → brief and unused → brief. All seven unused
  reasons, the no-add/no-drop constraint and Intent/Scope difficulty inputs remain.
- **Impact, freshness:** change → fresh; fresh → pin on yes; fresh → refresh on no; refresh → pin;
  fresh → stale stays dashed for `--no-refresh` or moving inputs; default-branch map → pin. All freshness
  inputs, incremental/rebuild alternatives, read-only fetch and labelled-stale fallback remain.
- **Impact, ranked:** pin → exec → link → mention; pin → rerun and exec → rerun. All four definitions,
  their ranked group, and exact rerun commands with `--changed-since` remain.
- **Review:** R → V for approval or P2/P3 only; R → F for P0/P1; F → B → R after inspection, commit
  and bind; R → D when a corrected kind repeats or design must change, with its decision diagnostic.
- **Landing, local:** gcommit → pc and gcommit → post; gpush → pp; pp → secrets for push; pp → pr.
  Local opt-in membership, the isolated Claude hooks, active/stale lease check, and conditional gitleaks
  scan versus warning/unscanned push remain.
- **Landing, checks:** pr → tests and pr → secrets; tests → land and secrets → land both say gh reads
  HEAD's check runs; land → exc stays dashed; land → landed. Workflow contents, platforms, event/path
  filters (including literal `docs/**` and `*.md`), manual runs, exact-HEAD checks, head-specific recorded
  CI exception, recheck, user confirmation and fast-forward push remain.

No existing rdp asset shows any of these exact graphs: the entry-page overview merges steps, and its
architecture and sequence figures show different relationships. Reusing those would lose source detail.

The rdp follow-up also updates all four `architecture-build` sources: Jev reorders skills and judges
task difficulty; routing says that judgement may adjust the tier within its floor. The group reads
“Recommended, optional AI judge” / “권장 선택형 AI 판정기”. The descriptions and only their matching
how-it-works alt text are updated. Both English `loop-review` sources use the 12px `label` class for LOOP.
