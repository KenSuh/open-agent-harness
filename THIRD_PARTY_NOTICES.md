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
