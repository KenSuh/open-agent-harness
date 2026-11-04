# Third-party notices

open-agent-harness vendors the skills listed under "Vendored skills" below, unchanged under `skills/`,
and adapts diagram-design templates, styles and selected icons for documentation as credited below.
This repository contains none of the files of the projects in the next table.
You install them yourself, and `oah` runs or links to them.

| Project | License | How open-agent-harness uses it |
|---|---|---|
| [unlazy](https://github.com/Leonxlnx/unlazy) | MIT | Runs gate checks and the Stop hook. Linked, not vendored: it is installed at the commit the gates lane tests, and none of its files are in this repository. Its gate format is linked, not copied. |
| [graphify](https://github.com/Graphify-Labs/graphify) (PyPI `graphifyy`) | Apache-2.0 (earlier contributions under MIT, per its NOTICE) | Optional. Builds the knowledge graph and is run as a CLI. |
| [Claude Code](https://code.claude.com/docs/en/setup) | Anthropic's terms | An external tool you install. Does the initial implementation stage. |
| [Codex CLI](https://github.com/openai/codex) | Apache-2.0 | An external tool you install. Does the review and correction stages. |
| [uv](https://docs.astral.sh/uv/) | See the project | Recommended installer for `oah` and graphify. |
| [superpowers](https://github.com/obra/superpowers) | MIT | Optional recommendation, linked. |

Ideas only, credited. No text or code is copied from these:

| Project | License | What inspired us |
|---|---|---|
| [netwaif/multi-agent-starter](https://github.com/netwaif/multi-agent-starter) | MIT | File-as-memory, a backend registry and a re-entry protocol |
| [netwaif/knot](https://github.com/netwaif/knot) | No license published | Markdown wiki hygiene checks |

If code from any project is ever copied verbatim, its license text is added here first.

## diagram-design

The editable HTML sources under `docs/assets/diagrams/` and their exported SVGs under `docs/assets/`
adapt templates and styles from [Cathryn Lavery's diagram-design](https://github.com/cathrynlavery/diagram-design/tree/3eb03b8),
pinned at commit **3eb03b8**, under the **MIT License**. The non-banner figures use the skill's default
gallery look: warm dotted paper, one focal accent, tag chips, mono technical labels, zones and legends.
The adaptations use installed system fonts, English and Korean text, static layouts and accessible
descriptions, with no external resources loaded. The banner is a hand-authored chat scene in its own style,
exported with the same tool. The skill itself is not vendored, and the existing badge SVGs are not derived from it.

The figures use **Tabler Icons**, licensed under **MIT**, from the skill's `references/primitive-icons.md`.
These are the selected monochrome user/agent, file, log, search and shield glyphs, adapted for inline SVG
grouping and inherited color. The set and license are recorded in the pinned skill's
[THIRD_PARTY_LICENSES.md](https://github.com/cathrynlavery/diagram-design/blob/3eb03b8/THIRD_PARTY_LICENSES.md#tabler-icons).
Source: [Tabler Icons](https://github.com/tabler/tabler-icons). No other icon set from the skill is used here;
no font files or the skill's logo are bundled.

```text
MIT License

Copyright (c) 2025 Cathryn Lavery

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

### Tabler Icons

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

## Benchmark data

`src/oah/templates/engines/benchmarks.json` is part of Epoch AI's published benchmark data, under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Epoch AI, 'Capabilities & benchmarking'. Published online at epoch.ai. Retrieved from
'https://epoch.ai/benchmarks/use-this-data'. Accessed 2 Nov 2026.

Changes: `tools/build_benchmarks.py` keeps only the latest successful result of each model at maximum effort on
the Epoch tasks it allow-lists. Data from external projects keeps its own licence and is left out. `oah` scales
these results to compare models. Epoch AI does not endorse `oah` or its model choices.

## Vendored skills (amendment 3, A13)

These skills are vendored under `skills/<name>/`, byte for byte as published upstream at the pinned
commit. Each folder carries the upstream license file (and NOTICE where there is one) and an
`UPSTREAM.json` with the repository, subpath, commit, SPDX id, license path and fetch date. Their
licenses apply to those folders. `oah skills update --vendored` advances a pin only after the license,
load and privacy checks pass.

| Skill | Upstream (path at the pinned commit) | Commit | License | Copyright | Files kept |
|---|---|---|---|---|---|
| `computer-use` | [stablyai/orca](https://github.com/stablyai/orca) `skills/computer-use` | `a0cd1644f320f1327f7495650a816111b495f3cd` | MIT | Lovecast Inc. | `LICENSE` |
| `orca-cli` | [stablyai/orca](https://github.com/stablyai/orca) `skills/orca-cli` | `a0cd1644f320f1327f7495650a816111b495f3cd` | MIT | Lovecast Inc. | `LICENSE` |
| `orchestration` | [stablyai/orca](https://github.com/stablyai/orca) `skills/orchestration` | `a0cd1644f320f1327f7495650a816111b495f3cd` | MIT | Lovecast Inc. | `LICENSE` |
| `typesafe-ai` | [typesafe-ai/skills](https://github.com/typesafe-ai/skills) `skills/typesafe-ai` | `65a39f393687675ce170e6094757de20370365b9` | MIT | TypeSafe AI | `LICENSE` |
| `skill-creator` | [anthropics/skills](https://github.com/anthropics/skills) `skills/skill-creator` | `dbd4588f9e1033efb41dad4bef2f7947c8993d44` | Apache-2.0 | not stated in the license file | `LICENSE.txt` |
| `mattpocock-skills` | [mattpocock/skills](https://github.com/mattpocock/skills) `skills` | `49dd158d1076134a641b33efb035946536778336` | MIT | Matt Pocock | `LICENSE` |
| `codex-plugin-cc` | [openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc) `plugins/codex` | `db52e28f4d9ded852ab3942cea316258ae4ef346` | Apache-2.0 | OpenAI (per NOTICE) | `LICENSE`, `NOTICE` |
| `humanize-korean` | [epoko77-ai/im-not-ai](https://github.com/epoko77-ai/im-not-ai) `skills`, `scripts`, `agents`, `.claude-plugin` | `b37e0206d31fffd9149bf57168d4d62eb2086bee` | MIT | epoko77-ai | `LICENSE` |
| `ponytail` | [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) `skills` | `2ed6c52c9d7e5e56942508591085fd45dea277d3` | MIT | DietrichGebert | `LICENSE` |
