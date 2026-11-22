<p align="center">
  <a href="https://oah.rodeo/"><img src="docs/assets/badge-website.svg" alt="OAH website"></a>
</p>

<p align="center"><strong>English</strong> · <a href="README.ko.md">한국어</a></p>

# open-agent-harness (`oah`)

Open Agent Harness (OAH) gives AI coding agents a defined scope, review steps and recorded checks for work in your Git repository.

## Download v1.0.0

Get the desktop app with its command-line helper, or install the full CLI from source.

- **[Download macOS DMG](https://github.com/KenSuh/open-agent-harness/releases/download/v1.0.0/OAH\_1.0.0_aarch64.dmg)** · macOS 13+ on Apple silicon. Ad hoc signature without Apple notarization.
- **[Download Windows setup](https://github.com/KenSuh/open-agent-harness/releases/download/v1.0.0/OAH\_1.0.0_x64-setup.exe)** · Windows 11 x64. Unsigned NSIS installer.

Before opening your installer, check it against [SHA256SUMS](https://github.com/KenSuh/open-agent-harness/releases/download/v1.0.0/SHA256SUMS). Both platforms may show a security prompt. [v1.0.0 release notes](https://github.com/KenSuh/open-agent-harness/releases/tag/v1.0.0)

[First run](#first-run) · [CLI installation](#install-the-source-cli) · [Documentation](#documentation)

## First run

1. **Verify and install.** Check the downloaded file with `shasum -a 256` on macOS or `Get-FileHash -Algorithm SHA256` in PowerShell. Once the hash matches, open the DMG and drag OAH to Applications on macOS, or run the setup file on Windows. Then open OAH. [Apple's guidance for opening an app](https://support.apple.com/en-us/102445)
2. **Check your agents.** Open **Agents** to see which tools are installed and whether they are signed in. Follow the install or sign-in guidance for the agents you plan to use, then select **Rescan agents**.
3. **Open your repository.** Select **+ Open…** and choose your Git working folder. Open **Team Workflow** to check readiness and assign agents to stages. If **Rules** shows an out-of-sync notice, use **Sync now** to update the managed instructions in `AGENTS.md`.

The app defaults to `~/.oah` on macOS and `%USERPROFILE%\.oah` on Windows. It keeps its settings in `settings.toml` and copies the bundled helper into `bin/` there. The helper provides `doctor` for agent checks, `sections show`, and `rules check` and `rules sync`.

## Install the source CLI

Use the source CLI for project setup and the full task workflow. It uses Git and Python 3.11 or newer. The installers check prerequisites and ask before installing missing tools.

- **macOS.** Download [install.sh](https://github.com/KenSuh/open-agent-harness/releases/download/v1.0.0/install.sh), verify it against the checksum file above, then run `bash ./install.sh` from its folder. [macOS CLI guide](docs/install-macos.md)
- **Windows.** Download [install.ps1](https://github.com/KenSuh/open-agent-harness/releases/download/v1.0.0/install.ps1), verify it against the checksum file above, then run `powershell -ExecutionPolicy Bypass -File .\install.ps1` from its folder. [Windows CLI guide](docs/install-windows.md)

You can also verify and unpack the [macOS source archive](https://github.com/KenSuh/open-agent-harness/releases/download/v1.0.0/open-agent-harness-v1.0.0.tar.gz) or [Windows source archive](https://github.com/KenSuh/open-agent-harness/releases/download/v1.0.0/open-agent-harness-v1.0.0.zip), then run the corresponding installer under `install/`.

Open a new terminal and run `oah --version`. It should print `oah 1.0.0`. In a Git repository with a remote, run `oah init`, review and confirm the setup plan, then commit the project files it creates. Follow the [first-task walkthrough](examples/hello-oah/README.md).

## Purpose

1. **Define the work.** Record what should change, how to verify it and which paths the task may edit. A [lease](docs/concepts.md#leases) reserves those paths. The [impact map](docs/concepts.md#the-impact-map) shows related code, docs and checks.
2. **Build and review.** The source CLI runs Claude Code or Codex CLI according to its stage configuration. Claude Code is the default for implementation. The dedicated [review and correction commands](docs/concepts.md#the-review-loop-and-corrections) use Codex CLI to review a read-only snapshot and address findings.
3. **Keep the evidence.** [Gates](docs/concepts.md#gates) record which checks passed. The [project wiki](docs/concepts.md#the-project-wiki) preserves decisions and their reasons for the next session.
4. **Decide whether to land.** Inspect the diff, the review and the checks. [`oah land`](docs/concepts.md#landing) checks the reviewed commit against your project's requirements. You decide when to confirm landing on the target branch.

## Documentation

- [How it works](docs/how-it-works.md) · [Commands](docs/commands.md) · [Concepts](docs/concepts.md)
- [Troubleshooting](docs/troubleshooting.md) · [Upgrade](docs/upgrade.md) · [Keys](docs/keys.md)
- [Design sources](docs/design-sources.md) · [Skills and capabilities](docs/skills-and-capabilities.md) · [Toolkit](docs/toolkit.md)
- [CI starter](docs/ci-starter.md) · [Companion apps](docs/companions.md)

## Roadmap

See the [project plan](docs/PLAN.md) for the design and planned work, and [GitHub Releases](https://github.com/KenSuh/open-agent-harness/releases) for changes by version.

## New in v0.4.0

The [v0.4.0 release](https://github.com/KenSuh/open-agent-harness/releases/tag/v0.4.0) introduced revisioned designs, source snapshots and reviewed skills. See the [v0.4.0 upgrade notes](docs/upgrade.md#7-from-030-to-040) for details.

- Managed delivery in v0.4.0 required a qualified host capacity profile before sending skill text or relayed tool results. A missing profile returned `OAH_SKILLS_BUDGET_EXCEEDED(target-capacity-unknown)`. See the [ordinary-work admission rule](docs/skills-and-capabilities.md#ordinary-work-and-managed-admission).

<details>
<summary>Project artwork and workflow diagrams from v0.4.0</summary>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
  <img src="docs/assets/banner-light.svg" alt="open-agent-harness: “Just ask, then confirm.” Your AI builds, reviews and records the work; you answer when judgment is needed and confirm before it lands, with a line-drawn OAH monogram and oah.rodeo." width="100%">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/features-dark.svg">
  <img src="docs/assets/features-light.svg" alt="The impact map, path-reserving leases and the recommended, optional TypeSafe Jev judge support the Claude Code build and Codex CLI review loop, gates and the wiki keep evidence and decisions, overlapping leases are refused, and you ask, answer decisions and confirm before landing, with Jev difficulty-based model choice in v0.3.0." width="100%">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/how-it-works-dark.svg">
  <img src="docs/assets/how-it-works-light.svg" alt="You describe a change in plain words; installed rules prepare intent, scope, path-reserving leases, the impact map and recommended, optional TypeSafe Jev judgement, Claude Code builds, gates and the wiki keep evidence, Codex CLI reviews and corrects, and you answer when it stops for a repeated problem or design choice and confirm before landing, with Jev difficulty-based model choice in v0.3.0 and a guided design conversation planned for a later release." width="100%">
</picture>

</details>

## Contributing

See the [contribution guide](CONTRIBUTING.md) and [development checks](docs/how-it-works.md#development).

## Security

Keep API keys, tokens, passwords, cookies, OAuth secrets and account identifiers out of the repository and its history, releases, issues and CI logs. Report vulnerabilities privately through the [security policy](SECURITY.md).

## License

OAH is released under the MIT License. See [third-party notices](THIRD_PARTY_NOTICES.md) for the licenses of bundled skills, diagrams and related tools.

<a href="LICENSE"><img src="docs/assets/badge-license.svg" alt="Read the MIT License"></a>
