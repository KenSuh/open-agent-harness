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

Models follow updates without edits. This describes `main`, which is not in a release yet.

- Benchmark scores, the task's difficulty and `[models] profile` pick each automatic model for Claude and Codex.
  [How a model is chosen](concepts.md#how-a-model-is-chosen) has the rule.
- A newer generation of a known family is used from the next catalog rebuild once it has a score, and a superseded
  or vanished model stops being chosen at that rebuild. A rebuild happens when the CLI version changes, after
  `[models] ttl_hours`, or with `oah models --refresh`. `OAH_MODEL_CHANGED` says once what moved.
- At a catalog rebuild `oah` now downloads Epoch AI's public benchmark file from epoch.ai, so newer scores arrive
  without an `oah` update. The request sends nothing about your project. `[models] fetch_scores = false` in
  `.oah/config.toml` stops it, and the snapshot shipped with `oah` then chooses. With your own
  `ARTIFICIAL_ANALYSIS_API_KEY` ([Keys](keys.md#artificial-analysis-scores)) it uses Artificial Analysis's scores instead.
- Claude's `haiku` may now be chosen: a free check, which cannot be billed, confirms the Claude aliases that
  `claude --help` leaves out.
- Before every launch `oah` checks that its model catalog is fresh. A model you pinned for a tier always wins.
- Every change of model or effort is logged with the old value, the new one, when and why.

`oah` does not edit your config, and it keeps working:

- Pins, `tier_pools`, your own `quality_order`, `allowed_models` and `effort_caps` keep their meaning.
- The old default `quality_order = ["opus", "sonnet", "haiku"]` counts as unset, so scores choose.
  `OAH_MODEL_ORDER_IGNORED` says so in `oah models`. Remove the line, or list other models to rank them yourself.
- Codex's default flag and the model in your Codex configuration no longer choose. To keep one model, pin it with
  `[engines.<engine>.tiers] <tier> = { model = "<model>" }` in `.oah/config.toml`.

What can stop a launch after you update:

- A review below its floor refuses with `OAH_MODEL_BELOW_FLOOR`, for example with `--tier low`. Use the tier the
  message names.
- With no model list, or an unreadable `claude --help`, automatic choice stops with `OAH_MODEL_CATALOG_UNAVAILABLE`,
  and with no usable scores with `OAH_MODEL_NO_SCORES`. A pin gets past both.
- A narrow `allowed_models` no longer lowers a floor. When no allowed model reaches it, a review stops with
  `OAH_MODEL_BELOW_FLOOR` and any other call with `OAH_MODEL_UNRESOLVED`.

`oah models` shows the profile, the scores with their source and date, the model and effort each tier uses now, and
each listed model left out with its reason. `oah doctor` now shows a pin that cannot run as the error a call would give.
[Troubleshooting](troubleshooting.md#models) lists the codes.

### Jev now judges difficulty

With TypeSafe Jev on (`[rerank] enabled = true`), this update changes what Jev receives and what its answer does:

- To judge how hard a task is, Jev now receives the task's Intent text (Problem, Desired outcome, Success conditions,
  Constraints and Non-goals) and its Scope paths, up to 16,000 characters in all, where it used to receive only the
  task name. It never receives code, diffs or file contents, and when a privacy rule flags the text nothing is sent.
  The first such request in a project prints `OAH_RERANK_SCOPE_WIDENED` once, and you are not asked again. To stop
  it, set `enabled = false` under `[rerank]` in `.oah/config.toml`.
- A call's tier can now differ from the rule tier. When Jev's confidence is at or above
  `[rerank] difficulty_min_confidence` (0.6 unless you set it), Jev's tier runs, never below the floor, and the run
  record says `tier_source=jev` and keeps the rule tier in `rule_tier`. `--tier`, or a plan tier you edited, still
  wins. With Jev off, failing or unsure, the rule tier runs as before.
- The kit's rules now have the lead session run `oah recommend --kind design --task <task> --request-file <file>`
  before it writes a design or a packet, and `oah packet new` warns `OAH_DESIGN_UNJUDGED` when that step was skipped.
  Run `oah rules sync` to take the new rule into `AGENTS.md`.
- With the session-start hook installed, the subagent files `oah init` wrote follow the current model choice, so git
  may show a change in `.claude/agents/oah-<tier>.md`. A file you edited is left alone, and `oah doctor` names it.

### Claude Code, Codex and their plugins

`oah` can now keep the tools themselves current, so newer models reach you without you remembering to update. This
describes `main`, which is not in a release yet.

- Existing projects stay as they were. A `.oah/config.toml` without `after_finish` under `[updates]` counts as off,
  `oah init` does not rewrite it, and `oah doctor` mentions the setting once. To turn it on, add:

  ```toml
  [updates]
  after_finish = true
  ```

  A new `oah init` asks once (Yes is recommended) and writes the answer there. `false` keeps it off and stops the
  notice.
- With it on, an `oah finish` that leaves no lease and no run in flight starts a pass in the background, at most once
  every `interval_hours` (24 by default). Run `oah update` to run one now; `oah update status` prints the last pass
  without running anything.
- MCP servers are never changed. A Claude Code plugin whose marketplace-declared command needs your confirmation, and a
  Codex plugin whose hooks need approval again in `/hooks`, are reported for you to handle. Sessions already running
  keep their version until you restart them.

[Tool updates after the work finishes](concepts.md#tool-updates-after-the-work-finishes) has the details, and
[Troubleshooting](troubleshooting.md#tool-updates) lists the codes.

## 7. What `docs/UPSTREAM_SYNC.md` records

`oah` follows the owner's own harness as it improves. [`docs/UPSTREAM_SYNC.md`](UPSTREAM_SYNC.md) records the
last commit of that harness that was reviewed, and one row per reviewed change: the commit, a summary and a
verdict.

- **Port:** written fresh as a generic `oah` feature, with the plan item and pull request that carry it.
- **Skip:** private or owner-specific.
- **Defer:** decided later.

Nothing is copied from that harness, and it is never a runtime dependency. You receive its improvements as
ordinary `oah` releases, through step 1 above.
