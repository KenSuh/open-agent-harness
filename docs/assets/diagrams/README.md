# Editable diagrams

These standalone HTML sources adapt the minimal templates, default palette and components from
[diagram-design](https://github.com/cathrynlavery/diagram-design/tree/3eb03b8), **MIT, commit 3eb03b8**.
Copyright (c) 2025 Cathryn Lavery. Its full license is in
[THIRD_PARTY_NOTICES.md](../../../THIRD_PARTY_NOTICES.md#diagram-design).
The skill is not installed or vendored here. Selected monochrome icons come from its
`primitive-icons.md` reference; their license is included [below](#icon-license).

Each stem has `-light.html`, `-dark.html`, `-ko-light.html` and `-ko-dark.html` sources.
Matching SVGs live one directory above. Edit HTML first; SVGs are exports, not independent sources.
The banner is a hand-authored 1280 × 560 chat scene with its own neo-brutalist style.
Its palette, outlines, hard shadows and role colors are recorded in the style guide's [Banner section](style-guide.md#banner).

| Stem | Skill type · fit canvas | Nodes / arrows | Purpose |
|---|---|---|---|
| `how-it-works` | Swimlane, 1080 × 1420 | 8 / 8 | Whole harness; both READMEs and both how-it-works pages |
| `features` | Architecture, 960 × 1040 | 6 / 6 | Six parts together; both READMEs |
| `architecture-build` | Architecture, 1160 × 1388 | 9 / 8 | Task preparation, routing, Jev and evidence |
| `architecture-land` | Architecture, 1104 × 1236 | 7 / 8 | Review, git hooks, CI and confirmed landing |
| `loop-start` | Sequence, 960 × 916 | 3 / 6 | Numbered exchanges 1–6 |
| `loop-review` | Sequence, 1008 × 1212 | 4 / 9 | Numbered exchanges 7–15 and the correction loop |
| `concepts-loop` | Flowchart, 1232 × 724 | 8 / 8 | Packet to confirmed landing |
| `concepts-leases-lifecycle` | State machine, 880 × 1308 | 6 / 7 | Start, renew, expiry and release |
| `concepts-leases-finish` | State machine, 880 × 1164 | 5 / 6 | Finish checks and refusals |
| `concepts-routing-rules` | Flowchart, 960 × 1272 | 7 / 6 | Difficulty rules, tier, model and effort |
| `concepts-routing-skills` | Flowchart, 960 × 1352 | 7 / 9 | Deterministic skills, reranking and fallbacks |
| `concepts-impact-freshness` | Flowchart, 880 × 1268 | 6 / 6 | Freshness, refresh, stale input and default branch |
| `concepts-impact-ranked` | Flowchart, 920 × 1064 | 5 / 5 | pin, exec, link, mention and exact reruns |
| `concepts-review` | Flowchart, 880 × 948 | 5 / 5 | All review outcomes and correction/bind return |
| `concepts-landing-local` | Flowchart, 1000 × 1156 | 8 / 5 | Opt-in local hooks and push paths |
| `concepts-landing-checks` | Flowchart, 1000 × 1332 | 6 / 6 | CI, exact HEAD, exception and confirmation |
| `banner` | Hand-authored chat scene, 1280 × 560 | — | README banner in its own neo-brutalist style |

[The style guide](style-guide.md) records the shipped gallery tokens, dotted paper, system font stacks,
type ramp, chips, ghost numbers, zones, icons and connector rules for the other figures. These use
one focal accent hue. All four variants of each gallery figure have the same structural geometry; Korean keeps
the same meaning, with 12px-or-larger Hangul and Korean system fallbacks.

Both READMEs now embed `features` after the purpose, before the whole-harness overview. Each picture uses
its language's SVG description verbatim as the image alt text in both themes.

The overview and features wording follows the same contract as the pages: you ask, answer when a repeated
problem or design choice stops the AI, and confirm before landing. A lease reserves the paths a task changes;
an overlapping lease is refused. TypeSafe Jev is the recommended, optional judge. It re-orders chosen skills;
in v0.3.0 it judges difficulty, and its tier picks the model subject to confidence and minimum-level
rules. Without a key, rules decide. The guided design conversation stays planned for v0.4.0.

## Regenerate

Use a local read-only copy of the pinned skill. Set `DIAGRAM_DESIGN` to its `skills/diagram-design`
directory. No installation or download is needed; its check and export helpers use Python's standard library.

```sh
for source in docs/assets/diagrams/*.html; do
  case "$source" in */banner-*) continue ;; esac
  python3 -I "$DIAGRAM_DESIGN/scripts/self_check.py" --offline "$source" || break
  python3 -I "$DIAGRAM_DESIGN/scripts/export_svg.py" "$source" \
    "docs/assets/$(basename "$source" .html).svg" --system-fonts || break
done
```

To export a banner, run the same `export_svg.py <banner-*.html> <banner-*.svg> --system-fonts` command for each variant; its hand-authored chat scene retains its own style.

Keep page alt text identical to both exported descriptions. Every SVG needs a first-child title,
a description and prefixed accessibility IDs. Use a picture element with a dark source and light fallback.
No scripts, external images, remote fonts, font faces or CSS imports belong in a source or export.

Self-check covers completeness, accessibility, offline safety and canvas bounds. It does not establish
text fit or connector clarity. Render each SVG where available with
`qlmanage -t -s 1600 -o .tmp/rdr-previews <svg>`, then inspect both themes and languages.
When Quick Look is sandbox-blocked, an already-installed offline libvips/librsvg renderer can produce
the same per-file PNG previews. Keep previews under `.tmp/`. Native macOS, Windows and Linux browser
rendering still depends on the fonts installed on the viewer's system.

## Redraw fidelity

The architecture keeps **14 unique components, four original groups and all 16 directed connections**.
The build figure has 9 nodes / 8 arrows; landing has 7 / 8. Gates and impact repeat to join the pair.
Each original node remains separate, including the optional Jev judge and the four rails.
All original node wording is retained as a short title plus logically separated detail.
Group membership, optional paths, edge directions and the exact reviewed-HEAD landing path remain.
The route → Jev edge retains skill ordering and difficulty as “SKILL ORDER + DIFFICULTY” / “스킬 순서·난이도”.
Ports and routes are redrawn without crossings; no component or edge is dropped or merged.

The two sequence figures keep **five unique actors and all 15 numbered messages**.
The first contains messages 1–6 and three actors; the second contains 7–15 and four actors.
The loop still contains review, findings, correction, diff and bind, excludes the git actor,
and retains return directions and dashed replies. Numbers still refer to the unchanged page tables.

The whole-harness overview is a new **Swimlane** composition, replacing the old four-step summary.
It shows a plain-language request; installed rules preparing intent, scope, lease, impact and recommended, optional
Jev judgement; Claude Code building; gates and wiki evidence; Codex review and correction returning
to evidence; the user's question, answer to a decision, inspection and confirmation; and checked landing.
A separate unconnected note marks the guided design conversation **planned for v0.4.0, not yet available**.
It does not turn that future feature into a current workflow step.

The new **Architecture** features figure has six nodes and six supporting/evidence connections.
It brings path-reserving leases, impact, recommended optional Jev, the Claude/Codex loop, gates and wiki together with plain benefits.
This is an overview of the existing parts, not a new product behavior or a replacement for the detailed graphs.

### Concepts pages

The ten concepts figures remain faithful imports of the six original Mermaid blocks.
Flowchart and ordinary State Machine are retained for their branching and dense transition semantics.
No semantic-pattern overlay is added: the skill's narrower lifecycle-phase pattern would omit guards.
The import, semantic-patterns, type, output, layout, core primitives and export references guide the redraw.

Every source node ID, source-label value, visible word, edge ID, direction, condition and group membership
is retained. Short bold headings and separate detail lines replace the former long centered labels.
Conditional arrows carry short words and lettered transition keys for their full conditions. The lease note is retained in full across its two figures.
There are **no merges, collapses or drops**. Counts apply separately to English and Korean and to both themes.
Repeated nodes join split figures; each original edge appears exactly once across its pair.

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

## Icon license

The small user/agent, file, log, search and shield glyphs are selected Tabler Icons from the skill's
`references/primitive-icons.md`, adapted only for inline SVG grouping and inherited color.
Source: [Tabler Icons](https://github.com/tabler/tabler-icons), MIT.
This notice applies to those glyphs in the HTML sources and exported SVGs.

```text
MIT License

Copyright (c) 2020 Paweł Kuna

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
