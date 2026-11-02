# Troubleshooting

Every `oah` refusal starts with a code such as `OAH_LEASE_CONFLICT`, and the rest of the message says what to
do next. This page lists the codes a beginner meets most often, grouped by the step where they appear. Search
it for the code you see.

[한국어](ko/troubleshooting.md)

A test (`tests/test_docs.py`) checks that every code on this page still exists in `oah`'s source, so the list
cannot point at a code that is gone.

## First steps

- **Run `oah doctor`.** It checks every tool `oah` uses and prints the official install command for each
  one that is missing.
- **Read the whole message.** It names the file, the lease or the gate involved, and the command to run.
- **Exit codes:** `0` done, `1` refused (fix what the message says and run the command again), `2` usage
  (a wrong or missing option; add `--help` to the command), except that `oah preflight` also returns `2`
  for an `ABORT` verdict (fix each `[abort]` line and retry). `69` means environment (a missing tool, a
  broken config file, or a folder that is not a repository). `75` means `oah wait` reached its timeout
  with the run still active; run `oah wait <run-id>` again.

## Setup and configuration

| Code | What it means | What to do next |
|---|---|---|
| `OAH_NOT_A_REPO` | The current folder is not inside a git repository. | `cd` into your project. For a new project, run `git init -b main` first. |
| `OAH_GIT_MISSING` | git is not installed or not on your PATH. | macOS: `xcode-select --install`. Windows: `winget install --id Git.Git -e`. Then open a new terminal. |
| `OAH_CONFIG_INVALID` | A value in `.oah/config.toml` has the wrong type, for example text where a number belongs. | Fix the key the message names, or delete it to use the default. |
| `OAH_CONFIG_UNKNOWN_KEY` | `.oah/config.toml` has a key `oah` does not know, often a typo. | Remove it, or use one of the keys the message lists. |
| `OAH_ENGINE_MISSING` | `claude` or `codex` is not on your PATH. | Install it (`oah doctor` prints the command), or set `command` under `[engines.claude]` or `[engines.codex]` in `.oah/config.toml`. |
| `OAH_UNSUPPORTED_BATCH_SHIM` | Windows: the engine is a `.cmd` or `.bat` file that `oah` cannot start safely. | Install the tool's native executable, or set an explicit `command = ["<interpreter>.exe", "<script>"]` in `.oah/config.toml`. |
| `OAH_EFFORT_METADATA_MISSING` | A tier asks for `highest` effort, but the engine has not reported the model's reasoning levels, so `oah` does not guess. | Run the engine once so it caches its model list, then `oah models --refresh`; or set a literal effort for the tier. |
| `OAH_EFFORT_UNSUPPORTED` | The tier's effort is not one the model lists. | Set the tier's effort to one of the levels in the message, or to `highest`. |
| `OAH_STATE_LOCK_TIMEOUT` | Another `oah` command holds the state lock. | Wait for it to finish, then run your command again. |

## Models

These appear on `main`, which is not in a release yet. [How a model is chosen](concepts.md#how-a-model-is-chosen) has the
rule, and `oah models` shows each model's score and why a model was left out.

| Code | What it means | What to do next |
|---|---|---|
| `OAH_MODEL_UNRESOLVED` | A tier's `tier_pools` names no model the CLI lists and `allowed_models` permits, or no allowed model reaches the floor this work needs. | Fix that pool, or add a stronger model to `allowed_models` in `.oah/config.toml`. If the CLI lists none, update it. |
| `OAH_MODEL_CATALOG_UNAVAILABLE` | `oah` has no list of the engine's models, so nothing can be chosen. | Run the engine once so it refreshes its list, then `oah models --refresh`, or pin a model for the tier. |
| `OAH_MODEL_NO_SCORES` | No benchmark scores can choose a model: the packaged data is damaged, or no listed model has a usable score. | Run `oah models` to see why, reinstall `oah`, or pin a model for the tier. |
| `OAH_MODEL_NOT_LISTED` | A fresh Codex catalog no longer lists the model pinned for this tier. Nothing was launched. | Run `oah models`, then change the pin in `.oah/config.toml`. |
| `OAH_MODEL_BELOW_FLOOR` | A review's tier, model or effort is below its floor, or no allowed model reaches that floor. No round or attempt was spent. | Use the tier the message names, or change the pin, pool, `quality_order` or `allowed_models` in `.oah/config.toml`. |
| `OAH_MODEL_FLOOR_UNCHECKED` | A notice: a review ran on a model you named that has no score, so its floor was not checked. The receipt says so. | Name a scored model if you want the check. |
| `OAH_MODEL_SUPERSEDED` | A notice: a model you named has a newer generation. It still runs. | Change that line in `.oah/config.toml` when you choose to. |
| `OAH_MODEL_UNSCORED` | A notice: the CLI lists a model with no usable score yet, so the older one stays. | Nothing. It joins when a score arrives. To use it now, name it in `.oah/config.toml`. |
| `OAH_MODEL_UNKNOWN_FAMILY` | A notice: the CLI lists a model of a family `oah` does not know, so it is never chosen automatically. | Name it in `quality_order` or a tier, or update `oah`. |
| `OAH_MODEL_CHANGED` | A notice: a catalog rebuild changed the model the rule uses at a step. It appears once and stays in `changes.jsonl`. | Nothing. To keep a model, pin it. |
| `OAH_MODEL_ORDER_IGNORED` | A notice from `oah models`: `quality_order` is the old default, so it counts as unset and scores choose. | Remove the line, or list other models to rank them yourself. |
| `OAH_SCORES_REFRESH_FAILED` | A notice: a catalog rebuild could not fetch fresh scores, so the last good copy, or the snapshot shipped with `oah`, chose. The message says why. Nothing was stopped. | Nothing, for a passing network problem: `oah` tries again at the next rebuild. For a refused key, store `ARTIFICIAL_ANALYSIS_API_KEY` again. To stop these requests, set `[models] fetch_scores = false`. |
| `OAH_SCORES_AA_UNUSED` | A notice: your Artificial Analysis copy has no score for a model the CLI lists, so Epoch AI scores chose for that engine. | Nothing. It clears once Artificial Analysis scores that model. |
| `OAH_SCORES_FILE_INVALID` | A notice: a stored score file in `oah`'s state folder is damaged or was changed, so it counts as absent. | Delete the file the message names, or run `oah models --refresh` to fetch it again. |

## Jev and difficulty

These appear on `main`, which is not in a release yet. [Jev judges difficulty](concepts.md#jev-judges-difficulty) says what
Jev receives and when its tier counts.

| Code | What it means | What to do next |
|---|---|---|
| `OAH_RERANK_SCOPE_WIDENED` | A notice, shown once per project: Jev now receives the task's Intent text and Scope paths, up to 16,000 characters, to judge its difficulty. | Nothing. To stop every Jev request, set `enabled = false` under `[rerank]` in `.oah/config.toml`. |
| `OAH_RERANK_SECRET` | A privacy rule flagged the part the message names (the Intent text, the Scope paths or the request text) as holding a key or a private path, so Jev was not asked and the rule tier ran. Nothing was sent. | Remove that text from the packet or the request file, or keep the rule tier. |
| `OAH_DESIGN_UNJUDGED` | A warning from `oah packet new`: no design judgement is recorded for the task. The packet was still created. | Before writing the design, run the command the message names: `oah recommend --kind design --task <task> --request-file <file>`. |
| `OAH_RECOMMEND_USAGE` | The `oah recommend` arguments do not fit together: `--request-file` without `--kind design` or `packet`, or without a valid `--task`, `--task` or `--scope` without `--request-file`, or a missing axis. | Run the command the message shows, or `oah recommend --help`. |
| `OAH_RECOMMEND_REQUEST_TOO_LARGE` | The request file is over 1 MB. Nothing was sent or recorded. | Write the request itself to a smaller file. Jev reads at most 16,000 characters. |
| `OAH_RECOMMEND_REQUEST_UNREADABLE` | The request file is missing, not a file, unreadable or not UTF-8. Nothing was sent or recorded. | Save the request as a UTF-8 text file and give its path. |
| `OAH_AGENT_FILE_EDITED` | A notice from `oah doctor`: a `.claude/agents/oah-<tier>.md` file differs from what `oah` generates, so it is left alone and its model is no longer kept current. | Keep it as it is, or delete it and run `oah init --with-agents` to have `oah` write it again. |
| `OAH_AGENT_FILE_STALE` | A notice from `oah doctor`: a subagent file `oah` generated names a model or effort that is no longer the choice, and no session-start hook will update it. | Install the hook with `oah hooks install session-start`, or delete the file and run `oah init --with-agents`. |

## Leases

| Code | What it means | What to do next |
|---|---|---|
| `OAH_LEASE_CONFLICT` | Another lease already holds paths that overlap your scope. The message names its task, agent and worktree. | Wait for that task to finish, choose other paths, or ask its holder to release it. |
| `OAH_SCOPE_EXCEEDED` | `oah finish` found changed files outside your lease's scope. | Widen the scope with `oah renew --task <task> --agent <agent> --scope <path> …` (repeat every path), or pass `--allow-out-of-scope`, which is recorded. |
| `OAH_LEASE_EXPIRED` | The lease ran past its time (4 hours by default) and cannot be renewed. | Start a new lease with `oah start`. You can still finish or release the expired one. |
| `OAH_LEASE_NOT_YOURS` | You named the right task with another agent's name. | Use the holder's `--agent`, or add `--force` to `oah release` when you really mean to take it over. |
| `OAH_WORKER_LEASE_REFUSED` | A worker that `oah call` launched tried to change a lease. Workers use the lease of the session that launched them. | Nothing to fix in the worker; the session that ran `oah call` finishes the lease. |

## Packets and intent

| Code | What it means | What to do next |
|---|---|---|
| `OAH_SCOPE_EMPTY` | The packet's `## Scope` lists no paths, so skills cannot be chosen by file kind and `oah start --packet` refuses it. | Add one bullet per path the task may write, or `- repo` for the whole repository. `oah packet new <task> --scope <path>` fills it for you. |
| `OAH_PACKET_PLACEHOLDER` | Template text such as `<checkable condition>` is still in the intent. | Replace each placeholder the message lists. |
| `OAH_INTENT_NOT_ACCEPTED` | The intent's `Status` is still `draft`. A draft cannot be bound to a lease. | When the intent says what you want, change the line to `Status: accepted`. |
| `OAH_INTENT_ID_CHANGED` | `Intent-ID` no longer matches the task, or an earlier binding. | Keep the ID. A changed goal raises `Revision` instead. |
| `OAH_INTENT_NOT_CURRENT` | The packet changed after its intent was bound, so the binding is `stale`, `missing` or `unbound`. | Restore the packet as it was bound, or raise `Revision`, accept it, and rebind with `oah start --task <task> --agent <agent> --packet <PACKET.md>`. |
| `OAH_INTENT_RETURN_INVALID` | The return given to `oah finish --return` does not match the bound intent. | Copy the `INTENT` line from `oah intent show <task>` and write exactly one `AC<n>: met|unmet|deferred — <evidence>` line per success condition. |

## Starting work

| Code | What it means | What to do next |
|---|---|---|
| `OAH_PREFLIGHT verdict=ABORT` | A check failed: your branch is behind the default branch, or another lease holds your paths. | Fix each `[abort]` line (for example `git rebase origin/main`), then run `oah preflight` again. |
| `OAH_IMPACT_STALE` | The impact map could not be refreshed, and `oah` never works from a stale map. | Fix the reason in the message, run `oah impact refresh`, then retry. |
| `OAH_STAGE_ENGINE_MISMATCH` | You asked one engine to do a stage the config gives to the other, for example Codex for the initial stage. | Run the engine the message names, change `[stages]` in `.oah/config.toml`, or pass `--override-stage` (recorded). |
| `OAH_EFFORT_DOWNGRADED` | The engine reported that it ran at a lower effort than requested, so the run does not count. | Check the engine's configuration and version, then run again. `oah` never lowers effort silently. |

## Gates

| Code | What it means | What to do next |
|---|---|---|
| `OAH_UNLAZY_MISSING` | unlazy, the gate checker, is not installed. | Run `oah init --with-unlazy`, or the `git clone … checkout --detach <commit>` line the message prints. `oah gates status` still gives a structural report meanwhile. |
| `OAH_UNLAZY_UNPINNED` | An unlazy folder exists, but not at the commit `oah` was tested with. | Move that folder aside, then install the pinned copy with the line the message prints. |
| `OAH_NODE_MISSING` | Node.js, which unlazy needs, cannot be started. | Install the LTS version from <https://nodejs.org>, then open a new terminal. |
| `OAH_GATES_MISSING` | The task's gate ledger does not exist. | Create it with `oah gates new <task>`. |
| `OAH_GATES_UNMET` | Some gates have no passing evidence. On a first run, CHECK commands wait for your approval. | Read the CHECK commands, then run `oah gates check <task> --approve`. If a gate is truly impossible, write `ABANDON: G<n> <reason>` in the ledger. |
| `OAH_GATES_UNVERIFIED` | `oah finish` cannot verify the gates because unlazy is missing. | Install unlazy and run `oah gates check <task>`, or pass `--allow-structural` to finish on the ticked boxes alone (recorded as unverified). |
| `OAH_GATES_SUMMARY_STALE` | `GATES.summary.md` no longer matches the gate records. | Run `oah gates check <task>`, commit the rewritten summary, then finish. |

## Wiki

| Code | What it means | What to do next |
|---|---|---|
| `OAH_WIKI_MAINTENANCE_MISSING` | `oah finish` found no wiki upkeep for this task, or it is out of date. | Run `oah wiki maintain --task <task>` before you commit, run the gates and ask for review. `--allow-missing-wiki-maintenance` skips it and is recorded. |
| `OAH_WIKI_RECOVERY_PENDING` | A log rollover was interrupted, so wiki commands stop rather than guess. | Run `oah wiki recover`, then retry. |

## Review

| Code | What it means | What to do next |
|---|---|---|
| `OAH_REVIEW_DIRTY_TREE` | Tracked files have uncommitted changes. A review reads a commit. | Commit or stash them, then run `oah review` again. |
| `OAH_REVIEW_CHANGES_REQUESTED` | Not an error: the round found P0 or P1 problems. | Run `oah fix --task <task>`, inspect and commit the correction, run `oah fix --task <task> --bind`, then `oah review --task <task>` again. |
| `OAH_REVIEW_NEEDS_DECISION` | The loop stopped for you: a finding repeats a kind already corrected, or its fix needs a design or stored-format change. | Decide, then run `oah review --task <task> --decision "<what you decided>"`. The decision is recorded. |
| `OAH_REVIEW_ROUNDS_EXHAUSTED` | You set `[review] max_rounds`, and the series reached it. | Run `oah review --task <task> --extra-round` (recorded), or raise or remove `max_rounds`. |
| `OAH_REVIEW_ATTEMPTS_EXHAUSTED` | Several reviewer calls in this round failed without counting, so `oah` stops calling the paid reviewer. | Read the last run's `stderr.log` in the state folder's `runs/`, fix the cause, then run the command the message prints with `--extra-attempt`. |

`oah fix` prints one line per next step, starting with the stage it reached. Follow them in order.

## Push and landing

| Code | What it means | What to do next |
|---|---|---|
| `OAH_PUSH_REFUSED` | The `pre-push` hook found an unfinished lease on this worktree. | Finish it with `oah finish`, or release it with `oah release --reason`, then push. |
| `OAH_PUSH_SECRETS` | gitleaks found possible secrets in the commits you are pushing. | Remove them from those commits, rotate any real key with its provider, then push. See [Keys](keys.md). |
| `OAH_LAND_LEASE_OPEN` | A lease is still open on this worktree. | Run `oah finish` for it, then `oah land --check` again. |
| `OAH_LAND_REVIEW_REQUIRED` | No converged review approves this exact head. | Run `oah review --task <task>` until it converges. A commit after the converged review needs a new round. |
| `OAH_LAND_SCAN_REQUIRED` | gitleaks is not installed, so the commits that would land were not scanned. | Install gitleaks (`oah doctor` prints the command), then `oah land --check` again. |
| `OAH_LAND_CI_FAILED` | A CI check run failed for this head. | Open the run on GitHub, fix the cause, push, wait for CI, then check again. See [Reading CI results](ci-starter.md#read-the-results-on-github). |
| `OAH_LAND_CI_UNVERIFIED` | Missing gh, no results, or a required check missing for this head; landing blocks by default. | Install/sign in to gh, push the workflow and task branch, wait and retry; or add `--accept-unverified-ci "<reason>"` to check and merge. The exception is recorded per head; CI stays unverified, never passed. |
| `OAH_LAND_CI_PENDING` | CI is unfinished or its results could not be read. | Wait for results, check `gh auth status` and the pushed branch, then retry. A CI exception cannot bypass this. |
| `OAH_LAND_IMPACT_STALE` | The default branch could not be fetched or reassessed, for example with `--offline` or a remote that is a relative path. | Use a remote with a URL or an absolute path, run `oah impact refresh`, then `oah land --check` again. |
| `OAH_LAND_CHECKS_REQUIRED` | The latest default branch binds checks to files you changed, and they have no passing evidence for your head. | Add a gate with the same CHECK and EXPECT to your ledger, run `oah gates check <task>`, commit, then check again. |

## Tool updates

These appear on `main`, which is not in a release yet.
[Tool updates after the work finishes](concepts.md#tool-updates-after-the-work-finishes) explains the pass.

| Code | What it means | What to do next |
|---|---|---|
| `OAH_UPDATE_STARTED` | `oah finish` started the tool-update pass in the background and did not wait for it. | `oah update status` shows the result once the pass finishes. |
| `OAH_UPDATE_SKIPPED` | The pass did not run: `after_finish` under `[updates]` is off or unreadable, a pass started less than `interval_hours` ago, the caller is a dispatched worker, or the background start failed. | Read the reason on the line. Set `after_finish = true` under `[updates]` in `.oah/config.toml` to turn it on, or run `oah update` to run a pass now. |
| `OAH_UPDATE_BUSY` | Another pass on this machine holds `~/.oah/updates/pass.lock`. | Wait, then run `oah update status`. `oah update` exits 1 with this code. |
| `OAH_UPDATE_DEFERRED` | A lease is active or a run is in flight (or a run record cannot be read), so nothing was updated or recorded. | Finish the work: the next `oah finish` that leaves nothing in flight starts the pass, or run `oah update` then. A run stuck at `running` keeps deferring it; check it with `oah wait <run-id>`. |
| `OAH_UPDATE_STATE_UNAVAILABLE` | `~/.oah/updates/state.json` is missing although history exists, or a state/history record is unreadable or invalid. Every tool is checked again. Bad history lines are skipped and `oah update status` reports their count. | Delete the damaged file and run `oah update`; the pass writes a fresh record. Neither record existing yet counts as a fresh install. |
| `OAH_UPDATE_FINISHED` | The pass ended. `complete` means every result is updated, current or unsupported; `needs-attention` means at least one is not. A command exceeding `timeout_seconds` is stopped and recorded as `failed`: "timed out; the tool may be partly updated". | Follow the step on each `oah:` line above it; for a timeout, run the tool's own command printed there to check the installation. `oah update status` shows the steps again. |
| `OAH_UPDATE_OFF` | `oah doctor`: `.oah/config.toml` has no `after_finish` under `[updates]`, so tool updates are off. | Set `after_finish = true` to turn them on (then `oah update` runs a pass now), or `false` to keep them off and stop the notice. |
| `OAH_UPDATE_UNSETTLED` | `oah doctor`: the last pass left a result that is not `updated` or `current`. | Follow the next step on that line, then run `oah update`. |

## Still stuck

- `oah status` shows every lease and whether it is active or stale.
- `oah intent show <task>` shows the intent's binding state.
- `oah gates status <task>` shows which gates are met, without running anything.
- [Concepts](concepts.md) explains why each check exists.
