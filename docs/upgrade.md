# Upgrade

`oah` changes as the models and skills under it change. This page says how to update each part: `oah`
itself, the files `oah init` wrote into your project, the skill kit cache, your project's pinned skills, the
MCP templates and the models. It ends with what `docs/UPSTREAM_SYNC.md` records.

[한국어](ko/upgrade.md)

Finish or release your open leases first (`oah status` lists them). Skill updates wait while any lease is
active.

## 1. Update oah itself

**From a clone** (the way to install while the repository is private), pull and run the installer again. It
replaces the installed `oah` with the checkout's version:

```sh
cd open-agent-harness
git pull
bash install/install.sh --skip-tools
```

On Windows:

```powershell
cd open-agent-harness
git pull
powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -SkipTools
```

`--skip-tools` (`-SkipTools`) skips the tool checks and only reinstalls `oah`. Leave it out to check the tools
too.

**From a release,** download the new tag's source archive and `SHA256SUMS` from the repository's release page.
Before extraction, compare the archive's full SHA-256 with its entry in `SHA256SUMS`; stop if the entry is
missing or the digests differ. Only after they match, extract it and run the included installer from the
extracted source folder: `bash install/install.sh --skip-tools` on macOS, or
`powershell -ExecutionPolicy Bypass -File .\install\install.ps1 -SkipTools` on Windows. The checksum commands
are in [Install on macOS](install-macos.md) and [Install on Windows](install-windows.md).

Then open a new terminal and check:

```sh
oah --version
oah doctor
```

## 2. Refresh what oah init wrote in your project

Run these inside each project after updating `oah`:

| Command | What it does |
|---|---|
| `oah rules check` | Reports whether the managed rules block in `AGENTS.md` differs from the new version's text. It only reads. |
| `oah rules sync` | Replaces the managed block with the new text. Everything outside the block stays as it is. |
| `oah init` | Prints its plan, then writes only what is missing. It never replaces your files without `--force`, and never the wiki's index or log. |
| `oah models --refresh` | Reads the engines' model lists again now. |

Commit the changes these make, as their own small task.

## 3. Skills: the kit cache, your project's pins and global folders

Skills live in three kinds of place, and each one updates differently. The full rules are on the
[Toolkit](toolkit.md) page.

| Where | Updated by | When |
|---|---|---|
| **The kit cache**, in `oah`'s state folder, outside your working tree | `oah skills update`, or the automatic check | Automatically at most once per `[skills] update_interval_days` (1 by default) when `[skills] auto_update = true`, and never while a lease is active. `oah skills schedule install` adds a daily OS entry for the same check. |
| **Your project's tracked copies** in `.claude/skills/` and `.agents/skills/` | only `oah skills update --project` | When you run it. It takes its own lease on those folders, so run it as its own task and commit the result. |
| **Your user-level skill folders** | only `oah skills update --global` | When you run it. It asks first. Nothing global changes on a schedule. |

`oah skills update --check` reports what is available and writes nothing. Every update is checked before it is
published: the license must stay the same, each skill must load, and the files must pass the privacy scan.
A failed or suspicious update rolls back to the last good commit and is reported. To hold one skill where it
is, add its name to `pins = ["<name>"]` under `[skills]` in `.oah/config.toml`.

When two copies of the same skill exist, selection prefers your project's copy, then the kit cache, then
user and plugin copies.

## 4. unlazy, the gate checker

unlazy is pinned to one commit that `oah`'s gate contract is tested against, and skill updates never move it.
Only a new `oah` version can move the pin. Install the pinned copy with `oah init --with-unlazy`.
Avoid `--force`: it does not replace an incompatible project copy and can reset `.oah/config.toml` to defaults.
Gate commands refuse with `OAH_UNLAZY_UNPINNED` while the copy does not match. To repair it:

1. If `OAH_UNLAZY_DIR` is set, either point it at a copy matching the new pin or clear it
   (`unset OAH_UNLAZY_DIR` on macOS; `Remove-Item Env:OAH_UNLAZY_DIR -ErrorAction SilentlyContinue` in PowerShell).
   The override is the only location checked while set; installing another copy will not bypass it.
2. Inspect `.agents/skills/unlazy` and `.claude/skills/unlazy`. Move each incompatible project copy outside
   the project to a backup location, or remove it yourself if it is no longer needed. For example, for the
   first path use `mv .agents/skills/unlazy ../unlazy-backup` on macOS or
   `Move-Item .agents\skills\unlazy ..\unlazy-backup` in PowerShell, choosing a backup path that does not exist.
   Init reports `OAH_INIT_UNLAZY_EXISTS` for an existing incompatible project directory, even with `--force`.
3. Run `oah init --with-unlazy` again, inspect its plan and confirm the pinned installation. Keep your existing
   config; review the result before committing.

## 5. MCP templates

The templates in [`mcp/`](../mcp/README.md) pin the npm version of each server they start. Check for newer
versions from inside your project:

```sh
oah skills update --check
oah skills update
oah doctor
```

Both update commands query the npm registry for the catalog's MCP pins. `OAH_MCP_UPDATE_AVAILABLE` means a
newer version is available, not installed. `--check` only reports it and writes nothing; `oah skills update`
also updates the skill cache and records notices in the state folder's `toolkit/updates.jsonl`. `oah doctor`
shows the last recorded notice per MCP entry offline; it does not query npm or prove your config is updated.
`current` means no newer version was found; `unused(offline)` or `unused(registry error)` means the check
could not establish that, so retry later. None of these commands edits an MCP pin or your MCP configuration.

When you choose to upgrade, edit the `@<version>` suffix in the copy you merged into your configuration,
then restart Claude Code or Codex. Kit maintainers update the `mcp/` templates and catalog pins separately.
The [Toolkit](toolkit.md) page lists each server with its source and license.

## 6. Models

Models follow updates without edits:

- Claude tiers use Claude Code's own aliases (`opus`, `sonnet`, `haiku`), which Claude Code resolves to its
  newest models.
- Codex tiers start from the model your Codex configuration uses, and adopt a newer one only when Codex marks
  it as its default or your `quality_order` names it.
- Before every launch `oah` checks that its model catalog is fresh. A model you pinned for a tier always wins.
- Every change of model or effort is logged with the old value, the new one, when and why.

`oah models` shows the model and effort each tier uses now.

## 7. What `docs/UPSTREAM_SYNC.md` records

`oah` follows the owner's own harness as it improves. [`docs/UPSTREAM_SYNC.md`](UPSTREAM_SYNC.md) records the
last commit of that harness that was reviewed, and one row per reviewed change: the commit, a summary and a
verdict.

- **Port:** written fresh as a generic `oah` feature, with the plan item and pull request that carry it.
- **Skip:** private or owner-specific.
- **Defer:** decided later.

Nothing is copied from that harness, and it is never a runtime dependency. You receive its improvements as
ordinary `oah` releases, through step 1 above.
