# Third-party notices

open-agent-harness vendors no third-party code. This repository contains none of the files of the
projects below. You install them yourself, and `oah` runs or links to them.

| Project | License | How open-agent-harness uses it |
|---|---|---|
| [unlazy](https://github.com/Leonxlnx/unlazy) | MIT | Runs gate checks and the Stop hook. You install it. Its gate format is linked, not copied. |
| [graphify](https://github.com/Graphify-Labs/graphify) (PyPI `graphifyy`) | Apache-2.0 (earlier contributions under MIT, per its NOTICE) | Optional. Builds the knowledge graph and is run as a CLI. |
| [Claude Code](https://code.claude.com/docs/en/setup) | Anthropic's terms | An external tool you install. Does the initial implementation stage. |
| [Codex CLI](https://github.com/openai/codex) | Apache-2.0 | An external tool you install. Does the review and correction stages. |
| [uv](https://docs.astral.sh/uv/) | See the project | Recommended installer for `oah` and graphify. |
| [superpowers](https://github.com/obra/superpowers) | MIT | Optional recommendation, linked. |
| [mattpocock skills](https://github.com/mattpocock/skills) | MIT | Optional recommendation, linked. |

Ideas only, credited. No text or code is copied from these:

| Project | License | What inspired us |
|---|---|---|
| [netwaif/multi-agent-starter](https://github.com/netwaif/multi-agent-starter) | MIT | File-as-memory, a backend registry and a re-entry protocol |
| [netwaif/knot](https://github.com/netwaif/knot) | No license published | Markdown wiki hygiene checks |

If code from any project is ever copied verbatim, its license text is added here first.

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
| `ponytail` | [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) `skills` | `2ed6c52c9d7e5e56942508591085fd45dea277d3` | MIT | DietrichGebert | `LICENSE` |
