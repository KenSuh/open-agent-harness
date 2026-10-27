# Toolkit

The toolkit is the set of skills, plugins, MCP servers and command-line tools that `oah` recommends, each
with its source and license. It ships three kinds of item:

- **Bundled skills** are vendored under `skills/<name>/`, byte for byte as published upstream at a
  pinned commit, with the upstream license file and an `UPSTREAM.json` provenance record. Only skills
  whose license permits redistribution are bundled. An **opt-in** item is never installed by `--all` and
  is selected only for the tasks it names. A **regional** item serves one language (for example
  `humanize-korean`, labelled "regional: Korean", for Korean prose); it is also opt-in.
- **Linked items** ship only their official install command and source, because their license does not
  allow redistribution or a file would fail this repository's privacy scan unchanged.
- **MCP templates** in `mcp/` start a server for Claude Code or Codex. They hold `${NAME}` placeholders
  (Claude) or `env_vars` names (Codex), never a value. `mcp/README.md` says where your keys live.
  Each template pins an npm version; the update check tells you when a newer one exists.

## Commands

| Command | What it does |
|---|---|
| `oah skills list` | Shows bundled, cached, linked and installed skills with version or commit, source, license and location, and the MCP template pins. Reads only. |
| `oah skills install <name>` | Copies a bundled skill into this project's `.claude/skills/` and `.agents/skills/` (`--target claude\|codex\|both`), under its own short lease. Shows the plan first. |
| `oah skills install <name> --global` | Copies into your user folders `~/.claude/skills/` and `~/.agents/skills/`. These are global files, so it asks first (`--yes` agrees). |
| `oah skills install <linked name>` | Runs the item's official install command after asking, or prints the step you type into Claude Code. |
| `oah skills update` | Refreshes the kit cache now and reports newer versions of the MCP template pins. `--check` only reports. |
| `oah skills update --project` | Updates this project's tracked skill copies, as its own leased task. |
| `oah skills update --global` | Updates your user-level copies, after confirmation. |
| `oah skills schedule install\|remove\|status` | Adds or removes a daily launchd agent (macOS) or Task Scheduler task (Windows) for the automatic check, with your consent. |

`--dry-run` on `install` shows the plan and writes nothing. Every refusal names the next command to run.

## Where skills are read from

For the same skill name, `oah` uses the first copy it finds:

1. the project's own copy in `.claude/skills/` or `.agents/skills/`;
2. the kit cache in oah's state folder (inside the repository's git folder, never the working tree);
3. your user copies in `~/.claude/skills/` and `~/.agents/skills/`, then plugin copies.

## How updates stay safe

- **Automatic updates write only the kit cache.** A due check currently runs from mutating `oah skills` commands at most
  once per `[skills] update_interval_days` (default 1). It waits while any task holds a lease, and it
  holds the state lock from the idle check through publication, so no lease can start mid-update.
  Turn it off with `auto_update = false` in `.oah/config.toml`.
  Wiring the same check into ordinary mutating commands such as `oah start` is a follow-up after this
  lane lands; until then, use a mutating skills command or `oah skills schedule install`.
- **Project copies are pins.** Your tracked `.claude/skills/` and `.agents/skills/` change only through
  `oah skills update --project`, which takes its own lease on those folders.
  Reinstalling preserves existing pins; if the cache has advanced, run `oah skills update --project`.
- **Global copies change only with `--global`** and your confirmation, never on a schedule.
- **One user's skill installs and updates publish one at a time.** Project, global, vendored and
  kit-cache writes all share the persistent `~/.oah/skills-publish.lock`, using the same home as
  user-level skill discovery. Commands take the repository state lock first, then this publication
  lock for preparation-input snapshots and from revalidation through replacement and any rollback;
  preparation itself runs unlocked. This also protects project and global folders that point to the
  same physical directory through symlinks or Windows junctions.
  Two OS accounts sharing one skill folder are not coordinated; run their commands separately.
  A changed input refuses the install or defers the update; run the command again against the current
  copy. If the publication lock times out, wait for the other command to finish, then retry.
- **Fetches use an allowlisted environment and a temporary home.** Proxy URLs with embedded user
  information are refused; configure a proxy without embedded credentials and retry.
- **Hold an item** with `pins = ["<name>"]` under `[skills]`. unlazy is never updated by this mechanism;
  it is installed at the commit the gates lane tests.
- **Every update is checked before it is published:** the license must keep the same SPDX id and text,
  each skill must load (frontmatter with `name` and `description`, links that resolve), and the files
  must pass the credential-assignment, key-shape and privacy scan (plus gitleaks when it is installed). A new executable, a
  binary file or a large size jump counts as suspicious.
  Required metadata must resolve to strings under YAML 1.2 core rules; quote numeric-looking text.
  Windows device names and portable path collisions are refused before staging writes any file.
- **A failed or suspicious update keeps the last good copy** and is reported with the reason. A failed
  swap restores the previous copy. Every result is logged with old and new commits, when and why in
  `toolkit/updates.jsonl` in oah's state folder.
- **MCP template pins stay pins; oah only notifies.** `oah skills update`, `--check`, `--vendored` and
  the automatic check ask the npm registry (`registry.npmjs.org`, the one extra network address) for
  the latest version of each catalog MCP entry, with the same allowlisted proxy rule and timeout as
  skill fetches. A newer version prints `OAH_MCP_UPDATE_AVAILABLE <name> <pinned> -> <latest>` with
  two steps: you change the version in your own MCP config when you choose to, and a kit maintainer
  bumps the template and the catalog. oah never edits `mcp/`, the catalog or your MCP config. The
  notice is logged in `toolkit/updates.jsonl` (status `notice`, old and new versions), and `oah doctor`
  shows the last one per server without going online. A version is printed only when it is a plain
  semver version; any other answer reads `unused(registry error)`, and no connection reads
  `unused(offline)`. Neither fails the command.
- **Kit maintainers** run `oah skills update --vendored --yes` from the actual kit checkout with its
  local installation. This takes a lease on `skills/` and the catalog; conflicting work must finish first.

## Bundled skills (vendored under `skills/`)

| Name | Source | Pinned commit | License | Verdict | Install |
|---|---|---|---|---|---|
| `computer-use` | <https://github.com/stablyai/orca> `skills/computer-use` | `a0cd1644f320` | MIT | vendor | `oah skills install computer-use` |
| `orca-cli` | <https://github.com/stablyai/orca> `skills/orca-cli` | `a0cd1644f320` | MIT | vendor | `oah skills install orca-cli` |
| `orchestration` | <https://github.com/stablyai/orca> `skills/orchestration` | `a0cd1644f320` | MIT | vendor | `oah skills install orchestration` |
| `typesafe-ai` | <https://github.com/typesafe-ai/skills> `skills/typesafe-ai` | `65a39f393687` | MIT | vendor | `oah skills install typesafe-ai` |
| `skill-creator` | <https://github.com/anthropics/skills> `skills/skill-creator` | `dbd4588f9e10` | Apache-2.0 | vendor | `oah skills install skill-creator` |
| `mattpocock-skills` | <https://github.com/mattpocock/skills> `skills` | `49dd158d1076` | MIT | vendor | `oah skills install mattpocock-skills` |
| `codex-plugin-cc` | <https://github.com/openai/codex-plugin-cc> `plugins/codex` | `db52e28f4d9d` | Apache-2.0 | vendor | `oah skills install codex-plugin-cc` |
| `ponytail` | <https://github.com/DietrichGebert/ponytail> `skills` | `2ed6c52c9d7e` | MIT | vendor | `oah skills install ponytail` |
| `humanize-korean` | <https://github.com/epoko77-ai/im-not-ai> `skills`, `scripts`, `agents`, `.claude-plugin` | `b37e0206d31f` | MIT | vendor, opt-in, regional: Korean (selected only for: korean-prose) | `oah skills install humanize-korean` |

## Linked skills and plugins

| Name | Source | Version | License | Verdict | Install | Why it is linked |
|---|---|---|---|---|---|---|
| `unlazy` | <https://github.com/Leonxlnx/unlazy> | 2.1.0 (16671491f667) | MIT | link | oah init --with-unlazy installs unlazy at the commit and scripts digest that lane P5 contract-tests (available once P5 lands); upstream: https://github.com/Leonxlnx/unlazy | unlazy is the gates' operational dependency: it is installed only at P5's contract-tested pin and never updated automatically. A test file in its repository also holds an absolute home folder path, which this kit's privacy scan refuses, and vendored files are never edited. |
| `superpowers` | <https://github.com/obra/superpowers> | 7.0.0 (bb92a7774141) | MIT | link | type in Claude Code: `/plugin install superpowers@claude-plugins-official` | Two files in its skills (a token variable assignment and an absolute home folder path) fail this kit's privacy scan unchanged, so it is linked. |
| `graphify` | <https://github.com/Graphify-Labs/graphify> | 0.9.93 (PyPI graphifyy) | Apache-2.0 | link | `pipx install graphifyy`<br>`graphify install` | Upstream has no skill folder: its CLI writes the skill from a template, so there is nothing to bundle unchanged. |
| `vercel-react-best-practices` | <https://github.com/vercel-labs/agent-skills> | 1.0.0 (063bee94c3f4) | NOASSERTION | link | `npx skills add vercel-labs/agent-skills` | Its upstream declares no license, so the kit cannot redistribute it. |
| `docx` | <https://github.com/anthropics/skills> | main (dbd4588f9e10) | LicenseRef-Anthropic-Proprietary | link | type in Claude Code: `/plugin marketplace add anthropics/skills`<br>type in Claude Code: `/plugin install document-skills@anthropic-agent-skills` | Its license is proprietary, so the kit cannot redistribute it. Claude Code's plugin marketplace installs it. |
| `pdf` | <https://github.com/anthropics/skills> | main (dbd4588f9e10) | LicenseRef-Anthropic-Proprietary | link | type in Claude Code: `/plugin marketplace add anthropics/skills`<br>type in Claude Code: `/plugin install document-skills@anthropic-agent-skills` | Its license is proprietary, so the kit cannot redistribute it. Claude Code's plugin marketplace installs it. |
| `pptx` | <https://github.com/anthropics/skills> | main (dbd4588f9e10) | LicenseRef-Anthropic-Proprietary | link | type in Claude Code: `/plugin marketplace add anthropics/skills`<br>type in Claude Code: `/plugin install document-skills@anthropic-agent-skills` | Its license is proprietary, so the kit cannot redistribute it. Claude Code's plugin marketplace installs it. |
| `xlsx` | <https://github.com/anthropics/skills> | main (dbd4588f9e10) | LicenseRef-Anthropic-Proprietary | link | type in Claude Code: `/plugin marketplace add anthropics/skills`<br>type in Claude Code: `/plugin install document-skills@anthropic-agent-skills` | Its license is proprietary, so the kit cannot redistribute it. Claude Code's plugin marketplace installs it. |
| `lazycodex` | <https://github.com/code-yeongyu/oh-my-openagent> | 5.1.40 | LicenseRef-Sustainable-Use-1.0 | link | `npx lazycodex-ai install` | Its source-available license (Sustainable Use 1.0) is not an open-source license, so the kit does not redistribute it. |

## MCP servers

context7 gives the coding agent up-to-date library and framework documentation on demand. The
documentation is always current because it comes from the context7 service; the template pins only
the small client package that talks to it, and its key (`CONTEXT7_API_KEY`) is optional. When npm
publishes a newer client, `oah skills update` prints `OAH_MCP_UPDATE_AVAILABLE` with the version to
change in your MCP config; oah never edits the pin for you.

| Name | Source | Version | License | Verdict | Install |
|---|---|---|---|---|---|
| `context7` | <https://github.com/upstash/context7> | `@upstash/context7-mcp@4.4.4` | MIT | template | `mcp/claude/context7.json`, `mcp/codex/context7.toml` |
| `mcp-openai` | <https://github.com/mzxrai/mcp-openai> | `@mzxrai/mcp-openai@0.1.1` | MIT | template | `mcp/claude/mcp-openai.json`, `mcp/codex/mcp-openai.toml` |
| `aside` | <https://aside.com/> | CLI 1.26.1010.1739 | LicenseRef-Proprietary | link | Download it from <https://aside.com/download>, then run `aside mcp` to register it |
| `openaiDeveloperDocs` | <https://developers.openai.com/mcp> | hosted service | NOASSERTION | link | `codex mcp add openaiDeveloperDocs --url https://developers.openai.com/mcp` |

## Command-line tools

| Name | Source | Version | License | Verdict | Install (macOS; `oah doctor` prints the Windows command) |
|---|---|---|---|---|---|
| `git` | <https://github.com/git/git> | 2.50.1 (Apple Git-155) | GPL-2.0-only | link | xcode-select --install (or: brew install git) |
| `gh` | <https://github.com/cli/cli> | 2.91.0 | MIT | link | brew install gh |
| `node` | <https://github.com/nodejs/node> | v24.6.0 | MIT | link | brew install node |
| `npm` | <https://github.com/npm/cli> | 11.5.1 | Artistic-2.0 | link | ships with node |
| `npx` | <https://github.com/npm/cli> | 11.5.1 | Artistic-2.0 | link | ships with node |
| `pnpm` | <https://github.com/pnpm/pnpm> | 10.32.1 | MIT | link | npm install -g pnpm (or: corepack enable pnpm) |
| `uv` | <https://github.com/astral-sh/uv> | 0.12.17 | MIT OR Apache-2.0 | link | brew install uv |
| `python3` | <https://github.com/python/cpython> | 3.14.4 | PSF-2.0 | link | brew install python@3.14 |
| `claude` | <https://github.com/anthropics/claude-code> | 2.1.309 | LicenseRef-Anthropic-Commercial | link | npm install -g @anthropic-ai/claude-code |
| `codex` | <https://github.com/openai/codex> | codex-cli 0.163.0 | Apache-2.0 | link | npm install -g @openai/codex (or: brew install --cask codex) |
| `gitleaks` | <https://github.com/gitleaks/gitleaks> | 8.30.1 | MIT | link | brew install gitleaks |
| `graphify` | <https://github.com/Graphify-Labs/graphify> | 0.9.93 (PyPI graphifyy) | Apache-2.0 | link | pipx install graphifyy (or: uv tool install graphifyy) |
| `aside` | <https://aside.com/> | 1.26.1010.1739 | LicenseRef-Proprietary | link | Download from https://aside.com/download |
| `orca` | <https://github.com/stablyai/orca> | 1.4.237 | MIT | link | Download from https://github.com/stablyai/orca/releases (note: the Homebrew cask named 'orca' is a different project) |
| `jq` | <https://github.com/jqlang/jq> | 1.8.1 | MIT | link | brew install jq |
| `rg` | <https://github.com/BurntSushi/ripgrep> | 15.2.0 | Unlicense OR MIT | link | brew install ripgrep |

## Excluded

Owner-specific or private skills, account-bound connectors, and tools that ship inside another product are not part of the kit. Only their counts are listed.

| Category | Excluded items |
|---|---|
| Skills | 13 |
| Plugins | 4 |
| MCP servers | 4 |
| Command-line tools | 0 |
