# Commands at a glance

[README](../README.md) · [How it works](how-it-works.md) · [한국어](ko/commands.md)

Use this page to find a command. The [worked example](../examples/hello-oah/README.md) puts them in order.
A packet is a written task; a lease claims its files; gates record proof; an impact map lists what a change
affects; the wiki keeps decisions; a tier is a difficulty level; and skills are instructions for the AI.

| Command | What it does |
|---|---|
| `oah init` | Guided setup: tools, sign-ins, project files and the first impact map. |
| `oah doctor` | Checks the tools `oah` uses and prints the install command for each missing one. |
| `oah packet new` / `check` | Creates or checks a task packet (`.oah/tasks/<task>/PACKET.md`). |
| `oah intent show` / `check` | Shows a task's intent, its digest and its binding state. |
| `oah preflight` | Checks the base branch, the working tree and the leases before you start. |
| `oah start`, `renew`, `status`, `finish`, `release`, `log` | Leases: claim paths, extend, list, close with checks, drop, note. |
| `oah recommend` | Picks a tier from the four difficulty axes. |
| `oah models` | Shows, and with `--refresh` re-reads, the model each tier uses. |
| `oah call`, `oah wait` | Launches Claude Code or Codex for one stage and records the run; waits for a background run. |
| `oah gates` | Writes, lints and checks a task's gate ledger. |
| `oah check` | Portable assertions for gate CHECK lines. |
| `oah wiki` | Keeps the project wiki linked, sourced and tidy. |
| `oah impact` | Lists what to re-check when files or database objects change. |
| `oah eval` | Replays real cases with hidden expected answers and reports each variant, with its limits. |
| `oah graph` | Uses the graphify knowledge graph when graphify is installed. |
| `oah review` | Runs one Codex review round and records its receipt; `review summary` prints a block for the pull request. |
| `oah fix` | Has Codex correct the latest review's findings, then binds the committed correction. |
| `oah land` | Checks that the exact reviewed head may land, or lands it after you confirm. |
| `oah rules` | Keeps the shared rules block in `AGENTS.md` in sync. |
| `oah skills` | Lists, installs and updates bundled and linked skills. |
| `oah hooks` | Installs or removes the opt-in Claude Code and git hooks. |
| `oah ci` | Sets up GitHub Actions checks for your project. |

Every command takes `--help`. Exit codes are `0` done, `1` refused, `2` usage or an `ABORT` verdict from
`oah preflight`, `69` environment and `75` when `oah wait` reaches its timeout with the run still active
(run `oah wait <run-id>` again). Every refusal starts with an `OAH_*` code and says what to run next.

Optional TypeSafe Jev can also choose a model and effort among the allowed candidates in v0.2.0;
on main it can also judge difficulty. See
[Jev](how-it-works.md#jev-the-optional-judge) for the distinction between released and unreleased behavior.
[The loop](how-it-works.md#the-loop) explains what each stage guarantees, and
[Troubleshooting](troubleshooting.md) explains refusals.
