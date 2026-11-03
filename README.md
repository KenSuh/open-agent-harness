<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
    <img src="docs/assets/banner-light.svg" alt="An adaptable AI workflow where Claude Code builds, Codex CLI reviews and corrects, and you confirm before changes land." width="100%">
  </picture>
</p>

<p align="center">
  <a href="../../actions/workflows/ci.yml"><img src="../../actions/workflows/ci.yml/badge.svg?event=pull_request" alt="CI status"></a>
  <a href="LICENSE"><img src="docs/assets/badge-license.svg" alt="License: MIT"></a>
  <img src="docs/assets/badge-macos.svg" alt="macOS 13+">
  <img src="docs/assets/badge-windows.svg" alt="Windows 11, native">
</p>

<p align="center"><strong>English</strong> · <a href="README.ko.md">한국어</a></p>

# open-agent-harness (`oah`)

## Purpose: a harness that keeps adapting

`oah` helps you build an AI development workflow that fits you and keeps changing as AI improves.
It is a working reference and a guide you can adapt. A **harness** is the set of tools, rules and records
around an AI coding assistant.

Take the parts that help, or start with the whole setup and make it yours. Older rules and skills
(instructions an AI can load) can hold a newer model back or make its results worse. Keep what helps,
remove what no longer does, and change the rules as your tools change. `oah` keeps evolving too.
Its defaults should be easy to understand and change; a rule that gets in your way is a design problem to fix.

### Why Claude implements and Codex reviews

This split comes from the author's own experience, not a measured comparison. In my use, Claude is good
at filling in details a beginner might miss while building the first version. Codex is good at checking
and correcting work within a fixed scope. So Claude writes first; Codex reviews, corrects and reviews again.

### Use it with any AI setup

The `oah` command drives **Claude Code** and **Codex CLI** today. The ideas work with **one AI or several**,
including other tools and harnesses. Take the parts you need and give each stage to the AI that suits you.

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/how-it-works-dark.svg">
  <img src="docs/assets/how-it-works-light.svg" alt="You set the goal, Claude Code implements it, Codex CLI reviews and corrects until the review converges, and you confirm landing after the checks, with optional TypeSafe Jev helping judge difficulty, model choice and skill order." width="100%">
</picture>

Write down what you want and how you will check it. Claude builds the first version. Codex reviews and
corrects it until no blocking findings remain (the review converges); a repeated problem or a design decision stops the loop for your
input. Checks and your confirmation come before the change lands in your project's main branch.
A round limit is optional.

**Leases** reserve files for a task, **gates** record checks, the **impact map** shows what a change affects,
and the **wiki** keeps decisions. `oah` keeps these records together. [How it works](docs/how-it-works.md)
explains these parts and the full loop. **TypeSafe Jev**, an optional AI judge, helps with difficulty,
model choice and ranking the skills for a task; the [Jev section](docs/how-it-works.md#jev-the-optional-judge) distinguishes
what is in v0.2.0 from what is on main awaiting release.

> [!NOTE]
> **v0.2.0 is the current release.** It is for developers fluent with git and pull requests, on macOS and
> native Windows. Linux is not yet supported.

## Install

One command per OS installs `oah`. It asks before it installs any missing tool, shows the official command
for each one, and never sees your passwords or keys. To install from a clone, run the commands below.
For v0.2.0, you can instead download the tag’s source archive and `SHA256SUMS` from the repository’s release
page. Refuse to unpack it unless its SHA-256 matches that archive’s digest in `SHA256SUMS`; then run the
included installer. The OS guides below show how to check it.

macOS (you need git first: `xcode-select --install`):

```sh
git clone https://github.com/KenSuh/open-agent-harness.git
cd open-agent-harness
bash install/install.sh
```

Windows PowerShell (you need git first: `winget install --id Git.Git -e`):

```powershell
git clone https://github.com/KenSuh/open-agent-harness.git
cd open-agent-harness
powershell -ExecutionPolicy Bypass -File .\install\install.ps1
```

Add `--dry-run` (`-DryRun` on Windows) to see every step without changing anything. The installer checks the
tools, installs `oah` with uv (or pipx, or pip), checks that a new terminal finds `oah`, and runs `oah doctor`.
The full guides are [Install on macOS](docs/install-macos.md) and [Install on Windows](docs/install-windows.md).

Then open a new terminal and check:

```console
$ oah --version
oah 0.2.0
```

## First run

Open a terminal inside your project, which needs to be a git repository with a remote, and run:

```sh
oah init
```

The guided setup asks about optional tools, shows the complete plan, and changes files only after you
confirm. Commit the files it writes, then follow [your first task](examples/hello-oah/README.md): a small,
worked example with real commands that takes about 20 minutes. [Setup details](docs/how-it-works.md#first-run)
explain every option and generated file.

## Roadmap

Nothing below is **available in v0.2.0** yet. Plans can change as the tools change.

### v0.3.0: models chosen from benchmarks

Planned, after v0.2.0:

- Benchmark scores pick the model. Difficulty and a profile (performance, balanced, efficient) set the score a job needs. Balanced takes
  the cheapest model that reaches it, efficient the best score per price, and performance the top scorer for the hardest work. No table ties a difficulty to a
  model, and every current model is a candidate in every profile.
- Scores come from Epoch AI, with no key and no account. With your own Artificial Analysis key, oah uses those instead. Published
  scores measure general quality, not your repository.
- A new model generation joins once it has a score, and superseded or removed models drop out, with no file edited. Each change
  shows a plain notice and stays in the run record.
- TypeSafe Jev judges difficulty from the task's Intent and Scope paths. When Jev is off or has low confidence, the rule-based
  difficulty runs. A call is never blocked.
- A review never runs below the floor for its risk, whatever a flag, packet, profile or judge says.
- After the last lease finishes, `oah` can bring Claude Code, Codex and Claude Code plugins up to date with each tool's own update
  command, and the model lists refresh at the next call. Codex plugins and MCP servers are not updated; oah's skills keep their
  own daily check (`oah skills update`). `oah init` asks once, and you can turn it off.
- `oah models report` counts how model choices were made, and an export saves them as cases you can compare later. Counts alone do
  not prove Jev helped.

Your pinned models, tier pools, effort caps and task floors stay in force throughout.

## Documentation

| Page | What it covers |
|---|---|
| [How it works](docs/how-it-works.md) | How the parts fit, the full loop, checks, requirements and setup details. |
| [Commands](docs/commands.md) | Find a command and understand its result. |
| [Worked example](examples/hello-oah/README.md) | One task through the whole loop, step by step. |
| [Concepts](docs/concepts.md) | Leases, packets and intent, difficulty, skills, impact map, wiki, gates, review, landing, updates, toolkit, CI and companion apps. |
| [Troubleshooting](docs/troubleshooting.md) | The common `OAH_*` refusals, what they mean and what to run next. |
| [Keys](docs/keys.md) | Where each optional key lives, and what is off without it. |
| [Upgrade](docs/upgrade.md) | Updating `oah`, your project files, skills, MCP templates and models. |
| [Install on macOS](docs/install-macos.md), [on Windows](docs/install-windows.md) | The installers, step by step. |
| [CI starter](docs/ci-starter.md) | `oah ci init`, reading CI results and making checks required. |
| [Companion apps](docs/companions.md) | Orca and Aside: what they add and how to set them up. |
| [Toolkit](docs/toolkit.md) | Every bundled or linked skill, MCP template and CLI, with its source and license. |
| [The plan](docs/PLAN.md) | The full design and the reasons behind each rule. |

Every page has a Korean version under [`docs/ko/`](docs/ko/).

## Development

[Development checks](docs/how-it-works.md#development) covers tests, CI, privacy scans and commit attribution.

## Security

No API key, token, password, cookie, OAuth secret or account identifier may appear in the repository,
its history, releases, issues or CI logs.
[Security rules](docs/how-it-works.md#security) explains key storage, scans and the full policy.

## License and third-party notices

`oah` is released under the [MIT License](LICENSE), copyright open-agent-harness contributors.
Bundled skills under `skills/` are shipped unchanged where their licenses allow it, with their own licenses.
The diagram HTML and SVGs adapt the MIT-licensed diagram-design templates and styles.
[Third-party notices](THIRD_PARTY_NOTICES.md) credits these works and the tools oah runs or links to,
including unlazy, superpowers, graphify, Claude Code and Codex CLI.
