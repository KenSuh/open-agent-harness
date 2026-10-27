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
  (a wrong or missing option; add `--help` to the command), `69` environment (a missing tool, a broken
  config file, or a folder that is not a repository).

## Setup and configuration

| Code | What it means | What to do next |
|---|---|---|
| `OAH_NOT_A_REPO` | The current folder is not inside a git repository. | `cd` into your project. For a new project, run `git init -b main` first. |
| `OAH_GIT_MISSING` | git is not installed or not on your PATH. | macOS: `xcode-select --install`. Windows: `winget install --id Git.Git -e`. Then open a new terminal. |
| `OAH_CONFIG_INVALID` | A value in `.oah/config.toml` has the wrong type, for example text where a number belongs. | Fix the key the message names, or delete it to use the default. |
| `OAH_CONFIG_UNKNOWN_KEY` | `.oah/config.toml` has a key `oah` does not know, often a typo. | Remove it, or use one of the keys the message lists. |
| `OAH_ENGINE_MISSING` | `claude` or `codex` is not on your PATH. | Install it (`oah doctor` prints the command), or set `command` under `[engines.claude]` or `[engines.codex]` in `.oah/config.toml`. |
| `OAH_UNSUPPORTED_BATCH_SHIM` | Windows: the engine is a `.cmd` or `.bat` file that `oah` cannot start safely. | Install the tool's native executable, or set an explicit `command = ["<interpreter>.exe", "<script>"]` in `.oah/config.toml`. |
| `OAH_MODEL_UNRESOLVED` | No model could be found for a tier: no pin, no catalog entry and no default in the engine's own configuration. | Set the tier's `model` in `.oah/config.toml`, or run `oah models --refresh` after the engine has a default model. |
| `OAH_EFFORT_METADATA_MISSING` | A tier asks for `highest` effort, but the engine has not reported the model's reasoning levels, so `oah` does not guess. | Run the engine once so it caches its model list, then `oah models --refresh`; or set a literal effort for the tier. |
| `OAH_EFFORT_UNSUPPORTED` | The tier's effort is not one the model lists. | Set the tier's effort to one of the levels in the message, or to `highest`. |
| `OAH_STATE_LOCK_TIMEOUT` | Another `oah` command holds the state lock. | Wait for it to finish, then run your command again. |

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
| `OAH_LAND_IMPACT_STALE` | The default branch could not be fetched or reassessed, for example with `--offline` or a remote that is a relative path. | Use a remote with a URL or an absolute path, run `oah impact refresh`, then `oah land --check` again. |
| `OAH_LAND_CHECKS_REQUIRED` | The latest default branch binds checks to files you changed, and they have no passing evidence for your head. | Add a gate with the same CHECK and EXPECT to your ledger, run `oah gates check <task>`, commit, then check again. |

## Still stuck

- `oah status` shows every lease and whether it is active or stale.
- `oah intent show <task>` shows the intent's binding state.
- `oah gates status <task>` shows which gates are met, without running anything.
- [Concepts](concepts.md) explains why each check exists.
