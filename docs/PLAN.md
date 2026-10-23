# open-agent-harness — Phase 0 plan

Status: **draft for approval** · 2026-10-23 · owner decisions recorded in section 13 · nothing is built yet.

This plan turns a private, single-owner multi-agent harness (called "the source" below) into a
general-purpose kit that one person can install on macOS or Windows and use on their own repository.
Section 4 lists every source component and what happens to it. Sections 5–11 describe what gets built.
Section 13 separates what the owner decided from what the builder decided.

---

## 1. Intent

**Problem.** Claude Code and Codex CLI are strong alone, but a person who uses both on one repository has
no shared memory between them. The tools don't know who is editing what. "Done" is whatever the agent
says. Reviews have no budget, no proof that the reviewer read the code, and no honest label when an
engine reviews its own work. Project knowledge evaporates between sessions. The source harness solved
these for its owner, but it is welded to one machine, one product and one person's tool choices.

**Desired outcome.** A beginner on a Mac or a Windows 11 PC follows the README and runs one install
script. They run `oah init` in their project and then finish the worked example end to end:
packet → preflight → lease → implement (Claude) → gates → review (Codex) → fix (Codex) → re-review →
finish → pull request. They edit nothing by hand except their own settings and their task text.

**GitHub is optional.** The worked example runs fully local in a fresh `git init` repository. A push and a
PR are the optional last step: `oah` prints the `gh pr create` command when `gh` and a remote exist,
and `oah review summary` prints the PR body.

**Release path.** v1 is built in this private repository. Going public is the owner's decision after a
final scan. The install docs cover a clone (works now) and the git URL (after it is public). Publishing
to PyPI is a later owner decision.

**Who it serves.** One person with one repository and at least one of Claude Code or Codex, ideally both.
Not teams, not servers.

## 2. Goals, non-goals, anti-goals

**Goals (all from the brief).**
- A shared lock/lease CLI so parallel sessions and worktrees don't collide.
- Engine routing by stage (initial implementation → Claude, review and correction → Codex) and by
  difficulty (tier → model and effort).
- Gate ledgers (CHECK / EXPECT / EVIDENCE) and a Stop hook, built on unlazy.
- Dispatch packets that carry the original intent and acceptance conditions.
- Preflight before work starts.
- A per-project knowledge wiki, a graph (graphify) and a code impact map.
- Reviewer trust ideas that generalize: round budget, canary, sanitized snapshot, honest independence label.

**Non-goals for v1.**
- Linux as a supported OS. It will probably work, but it is not tested.
- WSL as a requirement.
- Team or server mode, a web UI, or telemetry.
- Orca integration. It may come later as an optional adapter.
- Re-implementing what Claude Code or Codex already do, such as sandboxes, sign-in and subagents.

**Anti-goals (failure even if it "works").**
- A beginner has to read a long manual before the first useful run.
- The kit installs, changes or deletes anything global without asking: shell profiles, global agent
  settings, hooks.
- Private data from the source leaks into this repository.
- Third-party code is vendored instead of depended on.
- The kit works in bash but breaks in PowerShell, or only works on the author's Mac.
- Trust theater: a label or receipt that claims more than its mechanism proves.

## 3. Constraints

- **Platforms.** macOS 13+ and Windows 11 (Windows 10 22H2 best-effort, because Codex itself only
  recommends Windows 11). Native PowerShell. The Windows installer must run on the built-in
  Windows PowerShell 5.1.
- **Runtime.** Python ≥ 3.11, standard library only (`tomllib` needs 3.11). There are no third-party
  runtime dependencies. Node.js is needed only for unlazy's checker.
- **Beginner first.** There is one install command per OS. Every error says what to run next. Every
  optional feature is opt-in.
- **Privacy.** The repository holds no personal paths, names, emails, product names, keys or vault material
  (section 10).
- **Licensing.** The kit is MIT, with the copyright held by "open-agent-harness contributors".
  Third-party pieces are linked or depended on, never vendored (section 9).
- **CI cost.** CI runs on pull requests only, with one macOS job and one Windows job (section 8).

---

## 4. Source inventory and classification

The source has 286 files and 7.7 MB. Its main CLI is one 10,635-line Python file with 33 subcommands and
39 helper modules; 12 of the modules are Orca-only and 11 are OpenCode-only. There are also 69 contract
tests, about 180 KB of top-level rule documents and about 130 KB of rule fragments. The measuring
commands are in section 14.

Verdicts: **keep** (port the idea, rewritten portable), **simplify** (port a much smaller version),
**drop** (owner- or product-specific, historical, experimental, Orca-only, or too heavy for one person).

| # | Source component | What it does there | Verdict | What the kit does instead |
|---|---|---|---|---|
| 1 | Lease core: `start`, `claim`, `finish`, `release`, `status`, `log`, `context-save/show` over JSON ledgers in a per-user state root | Shared locks and task notes so parallel agents don't collide; an event log | **keep** | `oah start/renew/status/finish/release/log`. One lease record per task, state under the git common dir, real path-prefix overlap, a portable lock (5.4) |
| 2 | Hardened state publication: inode exchange through `renameatx_np`/`renameat2`, hardlink first-publish, `unlinkat` quarantine, `O_NOFOLLOW` | Defends ledgers against symlink and hardlink races | **drop** | Temp file + fsync + `os.replace`, with a Windows retry. The threat model is single-user (5.4) |
| 3 | Git gate `mah git` (three allowlists; workers may not push or merge) | A mechanical stop on agents pushing | **simplify** | Engine sandboxes, the rules block, and an optional pre-push hook that refuses while an unfinished lease exists (later) |
| 4 | Two lease roles (implementer vs. landing session) and the single-merge/push choreography (`READY_FOR_*` states) | Only one session lands | **simplify** | One rule: workers never push. You (the lead) push after `oah finish`. PR + CI decide |
| 5 | Worker call `mah call` (Claude or Codex) with an injected preamble | Runs another engine non-interactively with the protocol attached | **keep** | `oah call <engine> --stage <s>`: prompt on stdin, executable resolved per OS, process-tree kill on timeout (5.5) |
| 6 | Stage routing (`initial→claude`, `review/correction→codex`) plus kind/intensity/reasoning/risk scoring → profile → model/effort | Engine by stage, model and effort by difficulty | **keep, smaller** | `[stages]` and `[engines.*.tiers]` in `.oah/config.toml`, `oah recommend`, no model ids baked in (5.5) |
| 7 | Model catalog with TTL refresh (refuses to run when stale) | Keeps model lists current | **drop** | Config names the models; empty means the CLI default; `oah doctor` shows CLI versions |
| 8 | Orca dispatch: launch half of the dispatcher, launch matrix, heartbeat, orchestration gates and land queue (12 modules) | Supervised workers inside the Orca app | **drop** | None in v1; maybe an optional adapter later |
| 9 | Tier subagents (5 Claude agent files, tier table, tier sync, PreToolUse tier guard) | Model + effort per subagent by difficulty | **simplify** | `oah init` writes `.claude/agents/oah-{low,medium,high,max}.md`; `oah tiers sync/check`. The guard hook comes later |
| 10 | Rule-block sync (shared rules source, marker blocks in CLAUDE.md/AGENTS.md, AGENTS.md byte budget) | The same rules in every engine's instruction file | **keep** | `oah rules sync/check`: one block in `AGENTS.md`, and `CLAUDE.md` imports it with `@AGENTS.md`. Global files are never touched (5.11) |
| 11 | Session continuity (entry-prompt generator, SessionStart hooks) | A new session resumes where the last one stopped | **simplify, later** | `oah handoff` plus an optional SessionStart hook |
| 12 | Dispatch packets (`mah packet`, worker brief template) | One file that holds intent, scope, preflight, gates and return format | **keep** | `oah packet new/check` → `.oah/tasks/<task>/PACKET.md` (5.6) |
| 13 | Original-intent block (`MAH_INTENT`: id, revision, status, six sections, sha256 digest, per-criterion `AC<n>: met/unmet/deferred` lines) | The goal stays byte-identical across implement, review and fix | **keep** | `OAH_INTENT` block, the same digest rule (CRLF→LF), `oah intent show/check` (5.6) |
| 14 | Preflight script: stale base, active locks, recent commits, dirty tree, migration-number check, impact on touched files; ABORT / PROCEED_WITH_WARNINGS / PROCEED | Stops work on a stale or contested base | **keep, port to Python** | `oah preflight`: generic checks; project-specific checks such as migration numbers become config commands (5.7) |
| 15 | Gate ledgers through unlazy: `finish` refuses unmet gates, isolated re-run, Stop-hook routing, static ledger lint | "Done" means evidence, not a claim | **keep** | unlazy runs the checks. `oah` has a read-only status parser, the `finish` guard and a lease-aware Stop hook (5.8) |
| 16 | Verify-what-changed rule: re-run only the gates bound to changed files; heavy gates once on the final head | Cheap review rounds without losing rigor | **keep** | Rule text in the rules block, plus `oah impact --changed-since` |
| 17 | Impact map: lexical `git grep`, gate-CHECK bindings (pin/exec), a product database backend, an auto-fresh worktree, an external re-ranking layer | "If I change X, what must I re-check?" | **keep the generic part** | `oah impact`: names exact gates (`gates#G3`), lexical and wiki references, co-change history, optional graph neighbours. Backends become plugins later (5.10) |
| 18 | Edit-time impact hint hook | Shows bindings before editing risky files | **later** | Optional hook |
| 19 | graphify wrapper (report/query/path/explain/update, with update blocked where unsafe) | Safe access to the knowledge graph | **keep, thin** | `oah graph …` when graphify is installed |
| 20 | Wiki audit, inspired by `netwaif/knot` (broken wikilinks, unindexed, missing source, no frontmatter, orphans, duplicates, stale) | Wiki hygiene | **keep** | `oah wiki lint`, our own code (knot has no license, so nothing is copied) |
| 21 | Wiki conventions: catalog index, newest-first log with an insertion marker, a source line on every note, wikilinks | Long-term project memory agents can trust | **keep** | `oah init` scaffolds `docs/wiki/`; `oah wiki new/log` |
| 22 | Review loop: R1–R3 round ceiling counted at call time, series key, per-series lock, explicit extra-round override | Bounded review cycles | **keep** | `oah review` with `max_rounds = 3` and `--extra-round` (5.9) |
| 23 | Review artifact schema and validation (verdict/findings consistency, base/head echo) | A machine-checkable review result | **keep, trimmed** | A JSON schema passed to `codex exec --output-schema`; one receipt per round |
| 24 | Canary token (random per call, written into the reviewer's tree, never in the prompt, must appear in the evidence) | Proves access to the snapshot's token file only | **keep** | Same mechanism (5.9) |
| 25 | Sanitized review snapshot (fresh clone at the exact head, agent instruction files removed, reviewer config and rules ignored, minimal environment) | Reduces automatic instruction loading from repo files | **keep** | Portable Python version that also strips `CLAUDE.md` and `.claude/` (5.9) |
| 26 | "Same engine is not independent" rule | Honest trust labels | **keep, as a label** | Receipt field `independent: true/false` and a visible warning |
| 27 | Reviewer binary pinning (hash + macOS code signature + login-mode requirement) | Trust the reviewer binary | **simplify** | Record CLI path, version and sha256; pinning is optional; no code-signature check |
| 28 | Five-lane cooperative check (goal, QA, quality, security, context) | Self-check axes on the final head | **simplify** | A review rubric in the prompt and in the PR summary |
| 29 | Landing audit: review bundles bound to CI check-runs, fast-path vs. critical audits, landing-closure receipts, `verify-landing` | Final audit before merge | **drop** | PR + CI + `oah review summary`, plus critical-path warnings |
| 30 | Skill machinery: suggest/pack/route/audit/telemetry/lock, auto-maintenance, vendored third-party skills with local patches | Manage and inject skills | **drop** | Link to upstream skills; nothing vendored |
| 31 | External AI re-ranking service layer (difficulty shadow judge, model-upgrade shadow, footers) | Optional AI re-ranking | **drop** | — |
| 32 | Experimental third engine and free-model variants (OpenCode, Spark, shadow runs) | Experiments | **drop** | — |
| 33 | Retired engines (Antigravity, Grok, Kimi) | Old routing targets | **drop** | — |
| 34 | Web policy: route all web work to Aside and block built-in web tools | The owner's tool choice | **drop** | Aside is listed as an optional tool |
| 35 | Owner operations scripts (pre-push checks with fixed paths, CI and lane watchers, two historical audit lanes, session context) | Daily operations | **drop** | The pre-push secret-scan idea is kept for this repository's own development (section 10) |
| 36 | 69 contract tests | Guard the source | **drop** | New tests written from scratch (section 8) |
| 37 | Long rule documents (shared rules about 94 KB, routing policy about 47 KB, review procedure, rule fragments) | Operating manual | **drop** | Rewritten as beginner docs (section 11, P9) |

**Private data in the source that must never be copied.** 57 files contain the owner's absolute home path.
47 name the owner's product, and 23 mention keychain items (section 14). One test fixture alone has 681
lines with the home path. Nothing is copied file-for-file. Every kit file is written fresh, and the scan
in section 10 runs before every push.

**Drift found in the source** (do not port the inconsistency):
- A 3-lane rigor tier is documented, but the validator demands 5 lanes.
- "R4 needs user approval" in one place, "no per-round approval" in another.
- A doc says `finish` uses the unlazy CLI, but the code calls its library parser.
- Snapshot sanitization removes `AGENTS.md` but not `CLAUDE.md`.

---

## 5. Target design

### 5.0 Who drives the loop

- **The lead is your interactive Claude Code session**, or you at a terminal.
  - It does the initial implementation itself, under a lease, with Claude Code's normal permission
    prompts.
  - It runs `oah` through its shell tool: `preflight`, `start`, `gates check`, `review`, `fix`, `finish`.
  - You approve what it asks to run.
- **Codex runs as a worker** launched by `oah review` and `oah fix` (`codex exec`, non-interactive).
- **`oah call claude`** (headless `claude -p`) is for a second lane or a Codex-led session. It defaults to
  `--permission-mode acceptEdits` with the tool allowlist from `[engines.claude]` in the config. It never
  defaults to `bypassPermissions`.
- **Long runs.** `oah review|fix|call --background` returns a run id at once. `oah wait <run-id>` blocks for
  at most 9 minutes and can be called again. That fits agent shell tools that stop a command after about
  10 minutes.
- **A Codex-led session** works the same way, with the stages changed in the config. Its `workspace-write`
  sandbox cannot write `.git`, so it must approve `oah`'s state writes or set `OAH_STATE_DIR`.

### 5.1 What a project gets from `oah init`

```text
your-project/
  AGENTS.md              # gets one managed block: <!-- OAH:RULES:BEGIN v1 --> … <!-- OAH:RULES:END -->
  CLAUDE.md              # gets one managed line: @AGENTS.md (Claude Code imports it)
  .oah/
    config.toml          # stages, engine tiers, lease TTL, review and preflight settings
    tasks/<task>/        # one folder per task, committed with the work
      PACKET.md          #   intent + scope + plan + acceptance + return format
      GATES.md           #   unlazy-format gate ledger (git-ignored by default, see 5.8)
      GATES.summary.md   #   path-free summary written by `oah finish` (committed)
    templates/           # packet, gates and wiki-note templates the commands copy from
  docs/wiki/
    index.md             # catalog by category
    log.md               # newest first, under <!-- OAH:LOG_INSERT_BELOW -->
    decisions/           # decision records (ADR style)
  .claude/agents/        # optional: oah-low/medium/high/max.md (model + effort per tier)
  .graphifyignore        # optional: sensible defaults when graphify is used
```

Runtime state (leases, events, run logs, review receipts) lives **outside the working tree**, in
`<git common dir>/oah/`. Every worktree of the repository sees it. It never shows up in `git status`,
and deleting the clone deletes it. `OAH_STATE_DIR` overrides the location.

### 5.2 The kit repository

```text
open-agent-harness/
  README.md, README.ko.md          # quick start (English, Korean)
  LICENSE, THIRD_PARTY_NOTICES.md
  pyproject.toml                   # distribution "open-agent-harness", console script "oah"
  src/oah/
    cli.py                         # argparse entry, grouped help
    paths.py  lockfile.py  atomic.py  state.py  lease.py  events.py
    config.py  routing.py  engines/{claude.py,codex.py,proc.py}  call.py
    packet.py  intent.py  preflight.py  gates.py  hooks.py
    review/{snapshot.py,canary.py,schema.py,receipts.py,rounds.py}
    wiki.py  graph.py  impact.py  rules.py  tiers.py  init.py  doctor.py
    templates/                     # rules block (en, ko), packet, gates, wiki, agents, config
  install/install.sh  install/install.ps1
  docs/  PLAN.md  install-macos.md  install-windows.md  concepts.md  example.md  (+ ko/)
  examples/hello-oah/              # tiny Python project for the worked example
  tests/                           # stdlib unittest; fake engines; temp repos
  tools/privacy_scan.py            # this repository's own pre-push scan
  .github/workflows/ci.yml         # added only after the owner approves CI (section 8)
```

### 5.3 Runtime and packaging

- **Language: Python 3.11+, standard library only.** The source logic is Python, and graphify already
  needs Python. `tomllib` reads the config. Zero third-party runtime dependencies means a short
  install and nothing to audit.
- **Install with `uv tool install`.** uv installs Python itself when it is missing and puts `oah` on
  PATH on both OSes. The fallbacks are `pipx install` and `python -m pip install --user`, with a PATH
  note. While the repository is private, users install from a clone. Once public, they can install from
  the git URL, and later from PyPI. The name `open-agent-harness` is free on PyPI. `oah` is taken there
  by an unrelated project, so only the command is called `oah` (section 13).
- **Portability rules for all code.**
  - Never use `shell=True`; prompts go on stdin, avoiding the Windows 32K command-line limit.
    `shell=False` alone does not prevent Windows batch-file argument injection.
  - Resolve executables with `shutil.which` and prefer `.exe`. Never launch `.cmd` or `.bat` directly.
    For recognized npm shims, resolve the installed package's declared bin script and invoke it with
    `node.exe` and an argv list, without evaluating shim text. Otherwise refuse with
    `OAH_UNSUPPORTED_BATCH_SHIM` and tell the user to install a native executable or configure an
    explicit `[interpreter.exe, script, ...]` command. Paths and configured arguments remain literal.
  - Every file read and write states UTF-8 explicitly (Korean Windows defaults to cp949). Parsers accept
    CRLF; writers keep the target file's line endings.
  - No `fcntl`-only, `O_NOFOLLOW`-only or signal-only code paths without a Windows branch.

### 5.4 State and leases

- **Location.** `git rev-parse --git-common-dir` + `/oah/`. All worktrees share it. Codex's
  `workspace-write` sandbox makes `.git` read-only, so only `oah` itself (run by you or by
  `oah call` in the parent process) writes state. Sandboxed workers never write state.
- **Files.** `.lock`, `leases.json`, `events.jsonl`, `runs/<task>/<run-id>/…` and `reviews/<task>/…`.
- **Lock.** One lock file per state dir: `fcntl.flock` on macOS, `msvcrt.locking` with retry and backoff
  on Windows. Critical sections are short and time-bounded.
- **Atomic writes.** Write a temp file in the same directory, flush, fsync, then `os.replace`. On Windows,
  retry a few times on `PermissionError` (antivirus and indexers hold files briefly).
- **Lease record.**
  `{id, task, agent, scope[], note, worktree, branch, head_at_start, packet?, intent{id,revision,digest}?, gates[], created_at, expires_at}`.
- **Overlap.** `repo` overlaps everything. Path scopes overlap when one normalized, case-folded,
  repo-relative path is a prefix of the other. The source compared exact strings only, so `src` and
  `src/a.py` did not collide there.
- **Scope is advisory while working and checked at the gates.** Nothing can intercept an editor's writes.
  So `oah` compares the actual changes (base..head plus the working tree) with the lease scope at
  `finish`, at `review` and after `fix`, and lists any files outside the scope. `finish` refuses until
  the scope is widened (`oah renew --scope …`) or `--allow-out-of-scope` is given. Both are recorded.
- **TTL.** The default is 4 h. `oah renew` extends it. `status` shows expired leases as STALE without
  deleting them. `start` prunes them.
- **Commands.**
  - `start` refuses on an overlap and names the holder.
  - `finish` checks the declared gates, records the summary and releases.
  - `release` needs `--reason` (and `--force` for someone else's lease), and is logged as aborted.

### 5.5 Engines and routing

```toml
# .oah/config.toml (excerpt; created by `oah init`, edit freely)
[stages]
initial    = "claude"   # first implementation
review     = "codex"    # every review round
correction = "codex"    # fixes for review findings

[engines.claude]
command = ["claude"]
[engines.claude.tiers]
low    = { model = "sonnet", effort = "low",    timeout_min = 15 }
medium = { model = "sonnet", effort = "medium", timeout_min = 30 }
high   = { model = "opus",   effort = "high",   timeout_min = 60 }
max    = { model = "opus",   effort = "max",    timeout_min = 90 }

[engines.codex]
command = ["codex"]
[engines.codex.tiers]   # model = "" means "use Codex's own default model"
low    = { model = "", effort = "low",    timeout_min = 15 }
medium = { model = "", effort = "medium", timeout_min = 30 }
high   = { model = "", effort = "high",   timeout_min = 60 }
max    = { model = "", effort = "xhigh",  timeout_min = 90 }
```

- **Difficulty to tier.** `oah recommend --kind <k> --intensity <i> --reasoning <r> --risk <r>`, or `--tier`
  directly. The source's scoring is kept unchanged:

  | Axis | Score per value |
  |---|---|
  | kind | quick, docs, research → 1 · review, implementation, refactor, broad-audit → 2 · adversarial-review, migration, security, high-risk-review → 3 |
  | intensity | low 0 · medium 1 · high 2 · max 3 |
  | reasoning | low 0 · medium 1 · high 2 · xhigh 3 · max 4 |
  | risk | low 0 · normal 0 · high 2 · critical 4 |

  - **Base tier:**
    - **max** if risk is critical, reasoning is max, or the sum is ≥ 8;
    - otherwise **high** if risk is high, intensity is max, or the sum is ≥ 6;
    - otherwise **medium** if intensity is high, reasoning is high or xhigh, or the sum is ≥ 3;
    - otherwise **low**.
  - **Lifts:**
    - Intensity lifts the tier to at least medium, high or max for medium, high or max intensity.
    - Reasoning high or xhigh lifts it to at least high, and reasoning max to max.
  - **Kind floors, applied last:**
    - broad-audit, migration, security and high-risk-review are at least high;
    - research, review, implementation, refactor and adversarial-review are at least medium.

  A user with only one engine sets all three stages to it, and `oah` warns that same-engine review is not
  independent.
- **Stage enforcement.** `oah call <engine> --stage <s>` refuses when the engine is not `stages[s]`
  (`OAH_STAGE_ENGINE_MISMATCH`). `--override-stage` exists and is recorded in the events. A review is
  always read-only.
- **Command lines.** These are adapters, checked against the official CLI references on 2026-10-23.
  - Codex: `codex exec -C <dir> -s read-only|workspace-write [-m M] [-c model_reasoning_effort="E"] [--output-schema F] -o <out> [--ephemeral] -`, with the prompt on stdin.
  - Claude: `claude -p --output-format json [--model M] [--effort E] --permission-mode <mode> [--tools …]`, with the prompt on stdin. `--tools` restricts which tools exist; `--allowedTools` only pre-approves.
  - When the requested effort is above what a non-interactive run allows, `oah` stops with
    `OAH_SEPARATE_SESSION_REQUIRED` and prints the exact command for a separate session. It never
    lowers the effort quietly.
- **Runs.** Each call writes `prompt.md`, `stdout.log`, `stderr.log` and `result.json` (exit code,
  duration, CLI version, model, effort). The preamble is a short template: task, stage, scope, rules
  (stay in scope, never push, write evidence into the ledger), the packet, and the return format.

### 5.6 Packets and intent

`oah packet new <task>` creates `.oah/tasks/<task>/PACKET.md`:

```markdown
<!-- OAH_INTENT:BEGIN -->
## Intent
Intent-ID: <task>
Revision: 1
Status: draft            # becomes "accepted" when you approve it
### Problem
### Desired outcome
### Success conditions
- AC1: <checkable condition>
### Constraints
### Non-goals
### Open questions
<!-- OAH_INTENT:END -->
## Scope        (paths this task may write)
## Plan         (stage → engine → tier)
## Acceptance   (GATES.md in this folder)
## Return       (INTENT <id>@r<n> <digest> + one "AC<n>: met|unmet|deferred — evidence" line each)
```

- `oah packet check` verifies the required sections, unique AC ids, `Status: accepted` before
  `start --packet`, and that the ledger parses.
- **Digest.** sha256 over the bytes from BEGIN to END, after CRLF→LF and with one trailing LF. Changing
  the intent means a new revision, never an in-place edit.
- **Binding.** The lease records `{id, revision, digest}`. Review prompts carry the intent verbatim, and
  receipts record the digest. A mismatch is reported.
- **Not ported.** The source's POSIX owner and link-count checks on the packet file. The kit checks:
  regular file, not a symlink, under 1 MiB, unchanged between stat and read.

### 5.7 Preflight

`oah preflight [--task T] [--touch PATH…] [--offline]` runs these checks:

1. **Base freshness.** `git fetch` the default branch, then count how far HEAD is behind it. Behind
   means ABORT, with the rebase command. If a remote exists but cannot be fetched, that is also ABORT,
   unless `--offline` is given. If the repository has no remote at all, freshness does not apply and
   the result is a WARN.
2. **Dirty working tree.** WARN.
3. **Overlapping active leases** held by other tasks. ABORT. Stale leases are a WARN.
4. **Recent commits on the default branch** since the packet's base that touch the scope. WARN, with
   the list.
5. **Impact for `--touch` paths.** The bound gates are listed as information.
6. **The packet**, if `--task` is given. It must be accepted and must have ACs, otherwise ABORT.
7. **Project checks** from `[preflight].checks`. Each is an argv list and must exit 0. This is where
   things like a "migration number free" check go.

Verdicts and exit codes are PROCEED (0), PROCEED_WITH_WARNINGS (0, read the warnings) and ABORT (2). The
output always ends with a "not proven by this preflight" list.

### 5.8 Gates (unlazy)

- The format is unlazy's (`- [ ] G1:` … `CHECK:` / `EXPECT:` / `EVIDENCE:` / `ABANDON:`). `oah` links to the
  upstream format doc and does not copy it.
- `oah gates new <task>` writes `.oah/tasks/<task>/GATES.md` from a template whose CHECK lines run
  the same in `cmd.exe` and `sh`. The CHECK lines call `python`, `node` or `oah check …`, never `grep`
  or pipes.
- **`oah check exists|contains|absent|json …`.** Tiny portable assertions for gate CHECK lines. Each
  prints a success token, so beginners on Windows can write gates without POSIX tools.
- `oah gates status|check|lint` delegates to unlazy's `gate-check.mjs` / `gate-lint.mjs` when unlazy is
  installed. It is found through `OAH_UNLAZY_DIR`, then the project's skill dirs, then the user's skill
  dirs. **Running** a CHECK always goes through unlazy's own approval flow; `oah` never executes CHECK
  lines itself.
- **unlazy is required for gates, which makes Node required.** `oah finish` refuses while the task's
  declared ledger is not met according to unlazy's status. An `ABANDON:` line is reported as a required
  handoff, never as success.
- **Without unlazy**, a built-in read-only parser can still report the boxes, pending evidence and
  abandonments. Its result is labelled "structural only". It lets `finish` pass only with
  `--allow-structural`, and that is recorded in the events as unverified.
- **The lead runs the gates.** It runs `oah gates check` after implementing and after every `oah fix`.
  Workers may run tests to iterate, but they never approve CHECK commands or record gate evidence.
  - `oah` stores a per-gate input manifest and digest in runtime state alongside the evidence digest
    and gate-definition digest. Hash input paths, bytes and execution modes, including additions and
    deletions. Inputs include source, tests, config, tool versions and relevant env; default to all
    tracked and non-ignored untracked files, excluding generated gate outputs. Narrower
    input sets must be declared explicitly; unknown dependencies invalidate evidence conservatively.
  - Changed definitions or inputs invalidate evidence even if unlazy still marks the gate met.
    `gates check` runs affected gates through unlazy with `--reverify`; it never relies on the default
    skip-met behaviour. Compare inputs before and after the check; changes during it invalidate the run.
  - Unaffected gates need an explicit input-identical carry record with old/new input digests and the
    original evidence digest. `finish` and the lease-aware Stop hook reject stale or missing bindings
    against the final inputs, including working-tree changes; unlazy's green status alone is insufficient.
- **Ledger privacy.** unlazy's automatic evidence records the resolved working directory, which is an
  absolute path that contains the user name.
  - `oah init` therefore adds `.oah/tasks/*/GATES.md` to `.gitignore`.
  - `oah finish` writes a committed, path-free `GATES.summary.md`: only validated `G<n>` ids,
    fixed status enums and sha256 digests of titles, CHECK, EXPECT, evidence and input/carry records.
    No raw titles, commands, expectations, paths, task labels or other free text are exported; the
    writer rejects fields outside this allowlist. Full text stays in the ignored ledger or runtime state.
  - `[gates] commit_ledgers = true` commits the full ledger instead.
- **Pinned contract.** P5 pins an unlazy commit and writes down its CLI contract: arguments, output and
  exit codes. Contract tests against a stub keep `oah` honest when unlazy changes.
- **Stop hook (opt-in).** `oah hooks install` adds a Claude Code Stop hook that runs `oah hook stop`.
  When an active lease governs the session's worktree, the hook blocks while that task's ledger is
  unmet. A progress guard releases after N blocks with no progress, so it cannot wedge. When no lease
  governs, the hook hands over to unlazy's own stop hook if one is installed. The hook writes to
  `.claude/settings.local.json` (untracked) by default. Because the command is just `oah hook stop`,
  a shared install contains no absolute paths.

### 5.9 Review loop and trust

`oah review --task T [--base REV]` (stage `review`, Codex by default) does this:

1. **Resolve base and head.** Base is the merge-base with the default branch. Head is HEAD. Both are
   resolved to full SHAs. The tracked tree must be clean, so that what is reviewed is a commit.
2. **Rounds.** The series key is (task, base SHA). Only completed rounds count. At `max_rounds` (3),
   the review stops with `OAH_REVIEW_ROUNDS_EXHAUSTED`. `--extra-round` continues and is recorded.
   A per-series lock stops two rounds running at once.
3. **Snapshot.** `git clone --no-local --no-checkout`, then a detached checkout of the head and removal
   of the remote. Then strip `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, `CLAUDE.local.md`,
   `.claude/`, `.codex/` and `.agents/`. The prompt says they were removed and that the reviewer may
   read them only through `git show` or `git diff`.
4. **Canary.** A random token is written to `<snapshot>/.oah-review-canary` (exclusive create). The
   prompt asks for `REVIEWED-TREE-CANARY: <token>` in the evidence. The token never appears in the
   prompt. Its limit is stated: verification proves access to the snapshot's token file only.
   It proves neither source or diff coverage, review quality, nor tree identity; echoed SHAs do not
   establish those properties.
5. **Prompt.** The intent verbatim, the AC list, base and head, the diff stat, critical-path warnings,
   and a rubric: goal, tests, quality, security, context and regressions.
   The prompt treats all reviewed repository content, including source, comments, documents and files
   read through git, as untrusted evidence, never instructions to follow. Sanitization reduces automatic
   instruction loading; it does not block prompt injection in content the reviewer reads.
6. **Run.** The reviewer runs read-only inside the snapshot with `--output-schema` and `--ephemeral`.
   Codex runs with its user config and rules ignored, and with a minimal environment.
7. **Validate.** The schema, the consistency of verdict and findings, and the base/head echo. A missing
   canary gives `OAH_REVIEW_CANARY_MISSING`, and the round does not count.
8. **Receipt.** `reviews/<task>/R<n>.json` holds:
   `{task, round, base, head, intent_digest, reviewer{engine, cli_path, cli_version, cli_sha256, model, effort}, author_engines[], independent, verdict, findings[], canary_verified, prompt_sha256, output_sha256, started_at, finished_at}`.
   Each receipt is also appended to `reviews/<task>/index.jsonl`.

After the review:
- **`oah fix --task T`** (stage `correction`, Codex by default) runs in the real worktree with
  `workspace-write`. It gets the findings of the latest receipt, the intent and the scope. Codex cannot
  commit there (`.git` is read-only), so `oah` shows the diff and you commit. The next `oah review` is
  round R(n+1).
- **Who wrote what.** Commits carry an `Agent-Engine: claude|codex|human` trailer.
  - `oah fix` prints the exact commit command with `Agent-Engine: codex`.
  - The rules block tells Claude to add `Agent-Engine: claude`. Claude Code's own
    `Co-Authored-By: Claude` trailer is also recognized.
  - A commit with neither is `unknown`.
- **`independent`** is `true` only when every commit in base..head has a known engine that differs from
  the reviewer. It is `false` when the reviewer engine wrote any of them, and `unknown` when any commit
  is unknown, never `true`.
  - R2 after a Codex fix is therefore labelled "not independent".
  - That is the honest label for the chosen engine order, not an error.
- **`oah review summary --task T`** prints a PR-ready block: the rounds, verdicts, receipt digests, the
  independence label, gate status and touched critical paths. The critical paths are configurable;
  the defaults are `.github/workflows/**`, the agent instruction files and `**/migrations/**`.
- **The reviewer binary is recorded, not trusted blindly.** `oah` records its path, version and sha256.
  `[review].codex_sha256` can pin it. There is no macOS code-signature check, because it would not
  apply on Windows.

### 5.10 Knowledge: wiki, graph, impact map

- **Wiki (`docs/wiki/`).**
  - Every note has frontmatter (`title`, `date`, `sources`, `tags`) and at least one source: a file
    path with a commit, a URL, or a packet.
  - Notes are linked with `[[wikilinks]]` and listed in `index.md`. `log.md` is newest first, below
    `<!-- OAH:LOG_INSERT_BELOW -->`.
  - `oah wiki new "<title>"` creates a note from the template and lists it in the index.
    `oah wiki log "<title>"` inserts a dated entry.
  - `oah wiki lint` is read-only. It reports broken wikilinks, unindexed notes, missing sources, missing
    frontmatter, orphans, duplicate titles and stale notes, as text or JSON.
- **Graph (optional).** `oah graph update|query|path|explain|report` is a thin pass-through to
  graphify, run when it is installed (`uv tool install graphifyy`). `oah init --with-graph` writes
  `.graphifyignore` (wiki log, build outputs, `.oah/tasks/*/GATES.md` evidence).
- **Impact map.** `oah impact <path>… [--changed-since REV] [--strong-only] [--json] [--ref REV]` uses
  deterministic layers only. Results rank by binding (pin > exec > link > mention).
  1. **Gate bindings.** Ledgers are parsed per gate. A CHECK or EXPECT that hashes, greps or pins the
     path is **pin**; one that runs it is **exec**. The output names the gate, for example
     `.oah/tasks/T/GATES.md#G3 binding=exec`. This closes a gap in the source, where mapping a file back
     to a gate was manual.
  2. **Lexical references.** `git grep -n -I -F` at a ref, for the path, the basename and the stem.
     Results are classified by glob into workflow, test, doc, config, manifest and migration.
  3. **Wiki references.** Wiki pages that mention the file: the knowledge to update.
  4. **Co-change.** Files changed in the same commits over the last N commits that touched the path.
  5. **Graph neighbours.** Read from graphify's output when it exists; skipped silently otherwise.
- **`--changed-since REV`** turns the changed files into the list of gates to re-run. That is the
  verify-what-changed rule as a command. `--strong-only` lists every pin and exec result without a cap,
  ending with `total/shown/overflow`. Exit codes: 0 ok, 1 usage, 69 environment.
- **Later.** Project-specific impact backends (for example a database-object map) plug in as commands
  that print JSON.

### 5.11 Rules block and tier agents

- `oah rules sync|check` owns one block in `AGENTS.md`, between `<!-- OAH:RULES:BEGIN v1 -->` and
  `<!-- OAH:RULES:END -->`.
  - It creates the file when missing, appends the block when it is absent, and replaces only the block
    contents.
  - It refuses to write anything when markers are duplicated or broken. It keeps line endings and
    writes atomically.
  - After writing it checks that `AGENTS.md` stays under Codex's 32 KiB project-doc limit.
  - `CLAUDE.md` gets a single `@AGENTS.md` import line, which Claude Code supports. Both engines read
    the same text with no duplication.
  - The block template ships in English and Korean (`--lang`). `.oah/rules.local.md` is appended for
    project-specific rules.
  - Global files (`~/.claude`, `~/.codex`) are never written.
- **The block itself is under 6 KB.** It covers the stage order, leases, packets and intent, preflight,
  gates and the verify-what-changed rule, the review loop and its honest labels, "workers never
  push", and keeping the wiki current.
- `oah init --with-agents` writes `.claude/agents/oah-{low,medium,high,max}.md` (frontmatter: `name`,
  `description`, `model`, `effort` from the config). The body says: stay in scope, cite evidence, no
  destructive git, and return honestly when blocked. `oah tiers sync|check` rewrites only those
  frontmatter lines.

### 5.12 Hooks — all opt-in, project-local, removable

| Hook | Command | Phase |
|---|---|---|
| Claude Code Stop: block while the governing lease's gates are unmet | `oah hook stop` | P5 |
| Claude Code SessionStart: active leases, last log entries, latest handoff | `oah hook session-start` | later |
| git pre-push: refuse while an unfinished lease exists on this worktree | `oah hook pre-push` | later |
| Claude Code PreToolUse: impact hint before editing gates, workflows, migrations | `oah hook impact-hint` | later |

`oah hooks install|uninstall|status` shows the exact change before it writes. It writes atomically and
leaves a backup.

### 5.13 v1 command set

| Group | Commands |
|---|---|
| Setup | `doctor`, `init` |
| Task | `packet new/check`, `intent show/check`, `preflight`, `start`, `renew`, `status`, `finish`, `release`, `log` |
| Engines | `recommend`, `call` |
| Gates | `gates new/status/check/lint`, `check exists/contains/absent/json` |
| Review | `review`, `fix`, `review summary` |
| Knowledge | `wiki new/log/lint`, `graph …`, `impact` |
| Agent files | `rules sync/check`, `tiers sync/check` |
| Hooks | `hooks install/uninstall/status`, `hook stop` |

`oah doctor` checks git, Python, Node (only when gates are used), uv, claude, codex (`codex login status`)
and the optional tools. It prints the official install command for this OS for each missing one, and
exits non-zero when a required tool is missing.

---

## 6. Install per OS

### 6.1 Prerequisites (official sources, checked 2026-10-23)

| Tool | Needed for | macOS | Windows | Official page |
|---|---|---|---|---|
| Git | everything | `xcode-select --install` or `brew install git` | Git for Windows installer, or `winget install --id Git.Git -e --source winget` | https://git-scm.com/install/mac · https://git-scm.com/install/windows |
| uv (installs Python 3.11+ for you) | installing `oah` and graphify | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` | https://docs.astral.sh/uv/getting-started/installation/ |
| Python 3.11+ (only if not using uv) | `oah` runtime | python.org installer or Homebrew | Python Install Manager (python.org), or `winget install Python.Python.3.13` | https://www.python.org/downloads/ · https://docs.python.org/3/using/windows.html |
| Node.js LTS (24 today; 26 becomes LTS on 2026-10-28) | unlazy's gate checker (Node 16+) | `brew install node@24` or the nodejs.org installer | `winget install OpenJS.NodeJS.LTS` | https://nodejs.org/en/download |
| Claude Code | initial implementation stage | `curl -fsSL https://claude.ai/install.sh \| bash` | PowerShell: `irm https://claude.ai/install.ps1 \| iex` (WinGet: `Anthropic.ClaudeCode`) | https://code.claude.com/docs/en/setup |
| Codex CLI | review and correction stages | `brew install --cask codex` or `npm install -g @openai/codex` | `powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 \| iex"` | https://github.com/openai/codex · https://developers.openai.com/codex/windows/windows-sandbox |
| unlazy (required for gates) | running gate checks, Stop hook | `npx skills add Leonxlnx/unlazy` | same | https://github.com/Leonxlnx/unlazy |
| graphify (optional) | knowledge graph | `uv tool install graphifyy` | same | https://docs.graphify.com/installation |
| GitHub CLI (optional) | PRs from the terminal | `brew install gh` | `winget install --id GitHub.cli --source winget` | https://cli.github.com |
| gitleaks (optional) | secret scan before push | `brew install gitleaks` | `scoop install gitleaks` | https://github.com/gitleaks/gitleaks |
| Aside (optional) | browser and web tasks | installer from the download page | installer from the download page | https://aside.com/download · CLI/MCP: https://docs.aside.com/help/developers |
| Orca (optional, unused in v1) | multi-agent desktop app | `brew install --cask stablyai/orca/orca` or the download page | `.exe` installer from the download page | https://www.onorca.dev/docs/install |

**Accounts.**
- Claude Code needs a Pro, Max, Team, Enterprise or Console account; the free plan does not include it.
- Codex accepts a ChatGPT plan sign-in (Plus, Pro, Business, Edu, Enterprise) or an API key.

**System.**
- Claude Code needs macOS 13+ or Windows 10 1809+.
- Codex recommends Windows 11, and treats Windows 10 as best effort.
- Git for Windows is optional for Claude Code. Without it, Claude Code uses PowerShell.

### 6.2 macOS — `install/install.sh`

The script is bash 3.2 compatible (macOS ships 3.2). Run it from a clone with `bash install/install.sh`.

1. **Detect** git, uv or Python ≥ 3.11, Node, claude, codex, gh, gitleaks and graphify, and print a
   status table.
2. **Show fixes.** For each missing tool, print the official command from 6.1. With `--yes`, or after
   a y/N prompt per tool, it runs that command: Homebrew where available, otherwise the official
   one-liner. Without consent it changes nothing.
3. **Install `oah`.** `uv tool install --force <clone>`, falling back to pipx, then to
   `pip --user` with a PATH note.
4. **Check PATH.** Make sure `oah` resolves in a fresh shell. If it doesn't, offer
   `uv tool update-shell`. The script never edits shell profiles silently.
5. **Optional extras.** `--with-unlazy` and `--with-graphify`.
6. **Verify.** Run `oah doctor`.

`--dry-run` prints every step without running it. CI uses it.

### 6.3 Windows — `install/install.ps1`

**How to run it.** Open PowerShell in the clone and run
`powershell -ExecutionPolicy Bypass -File .\install\install.ps1`. The policy applies to that one process
only; no system setting changes. Alternatively, `Unblock-File .\install\install.ps1` lets you run the
reviewed file under `RemoteSigned`.

**Compatibility rules for the script.**
- It runs on Windows PowerShell 5.1 and on PowerShell 7.
- The script text is **ASCII only**. PowerShell 5.1 misreads UTF-8 without a BOM, so Korean help lives
  in the docs.
- The repository's `.gitattributes` gives `.ps1` CRLF and `.sh` LF.

**Steps.** They are the same as on macOS, with these differences:
- It uses `Get-Command`.
- It uses `winget` for git, Node, gh and Python, and the official PowerShell one-liners for uv, Claude
  Code and Codex.
- After each install it refreshes `$env:Path` from the registry, so new tools work in the same window.

**Encoding.**
- `oah` writes all files and run logs as UTF-8. Capture subprocess stdout/stderr as bytes, then decode
  with an explicit encoding (UTF-8 by default, configurable to cp949 per tool) and `errors="replace"`.
- Before any printing, configure `oah`'s stdout and stderr with their existing encoding and
  `errors="backslashreplace"`, including redirected streams; use an equivalent encoding-safe writer
  for streams without `reconfigure`. Unencodable emoji and U+FFFD are escaped, never fatal.
- Native Windows CI covers paths with spaces or Korean user names and redirected cp949 stdout/stderr
  carrying Korean, emoji and malformed subprocess bytes, with no `UnicodeEncodeError`.

### 6.4 Why native Windows and not WSL-only

- Claude Code ships a native Windows installer and runs shell commands through PowerShell.
- Codex ships a native Windows installer with a native sandbox; WSL is documented as an alternative.
- `oah` is standard-library Python and runs natively.
- Requiring WSL would add a Linux distribution, a second file system and a second set of credentials
  for a beginner, for no gain. WSL users can still run the macOS/Linux path.

## 7. `oah init`

```text
oah init [--lang en|ko] [--with-agents] [--with-hooks] [--with-unlazy] [--with-graph] [--dry-run] [--force]
```

- **Plans before writing.** It prints the list of files it will create or change, then writes only
  missing files. Existing files are never overwritten without `--force`; managed blocks are the
  exception, and `rules sync` updates those in place.
- **Always creates** `.oah/config.toml` (with the defaults from 5.5 and the detected default branch),
  `.oah/templates/`, the managed block in `AGENTS.md`, the `@AGENTS.md` line in `CLAUDE.md`, and
  `docs/wiki/` (index, log with marker, decisions template).
- **Options.**
  - `--with-agents`: the tier agent files.
  - `--with-graph`: `.graphifyignore`, plus a hint to run `oah graph update`.
  - `--with-unlazy`: runs `npx skills add Leonxlnx/unlazy` after a prompt.
  - `--with-hooks`: the Stop hook, after showing the diff.
- **Idempotent.** A second run changes nothing and says so.
- **The impact map needs no scaffolding.** It reads ledgers, git and the wiki on demand. The only
  scaffold for it is `[impact]` in the config: co-change depth and channel globs.

---

## 8. Tests and CI

**Unit tests** use the standard library `unittest`, so there are no test dependencies. Areas:
- the lock under concurrent processes (`multiprocessing`, with both platform branches);
- atomic writes;
- lease overlap, TTL and the `finish` guard;
- config defaults and validation;
- routing scores;
- engine command lines; native Windows CI checks npm-shim resolution and unsupported batch-shim
  refusal with spaces and Windows-valid metacharacters (`& ^ % ! ( )`) in paths, plus
  `& | < > ^ % ! ( )` in configured arguments, verifying literal argv and no injected command execution;
- packet and intent parsing, including the digest being equal for CRLF and LF files;
- the gate status parser and unlazy delegation, using a stub `node` script; an unchanged CHECK with
  changed source must trigger `--reverify`, a failing rerun must block finish, and input-identical carry
  must pass while input changes during a check must fail;
- summary export with paths and personal-identifier fixtures in every free-text field: only validated
  ids, fixed statuses and digests survive;
- subprocess decoding and encoding-safe stdout/stderr, including the redirected cp949 cases in 6.3;
- impact bindings in a temp repo;
- rules-block sync: idempotence, refusal on broken markers, line endings kept, the 32 KiB budget;
- `init`: idempotence, no overwrite, dry run;
- review: snapshot stripping, canary, schema validation and round budget, using a **fake codex**;
  a fake reviewer reading only the token file can pass canary verification, which must not be labelled
  as source coverage or tree-identity verification;
- preflight verdicts against a temp bare remote;
- the Stop-hook decision JSON;
- installer dry runs.

**Fake engines** are small Python scripts that the config points at
(`command = ["python", "tests/fakes/fake_codex.py"]`). That is one reason `command` is a list.

**Local runs on the development Mac** use
`TMPDIR=$PWD/.tmp python3.11 -m unittest discover -s tests -v`, so every test write stays inside this
checkout (`.tmp/` is git-ignored). Run them again with the newest local Python.

**CI** is `.github/workflows/ci.yml`:
- It triggers on `pull_request` to `main`, plus `workflow_dispatch`, and skips docs-only changes
  (`docs/**`, root `*.md`).
- It runs one job each on `macos-latest` and `windows-latest`, with Python 3.11 and no larger matrix.
- `concurrency` cancels superseded runs. `permissions: contents: read`. Actions are pinned to full
  commit SHAs. Timeout is 15 minutes.
- Steps: unit tests, the installer `--dry-run` (bash on macOS; `powershell` 5.1 **and** `pwsh` on
  Windows), and `oah init --dry-run` in a temp project.

**Cost.**
- Private-repository list prices are $0.062 per minute for macOS and $0.010 for Windows; Linux is
  $0.006. GitHub Pro includes 3,000 standard-runner minutes a month
  (https://docs.github.com/en/billing/reference/actions-runner-pricing).
- An expected run of about 3 macOS minutes plus 4 Windows minutes costs about $0.23 at list price once
  the included minutes are used up.
- Not verified: how the included minutes are counted against macOS and Windows runners.
- Public repositories run standard runners for free.

**This Mac has no PowerShell and no Windows.** Windows behaviour is therefore verified **only by CI**.
The owner approved CI from P1 on 2026-10-23.

## 9. Licensing and third-party pieces

| Piece | License | How the kit uses it |
|---|---|---|
| this kit | **MIT**, "Copyright (c) 2026 open-agent-harness contributors" | — |
| unlazy (Leonxlnx/unlazy) | MIT | dependency, installed by the user; the docs link its format; nothing copied |
| graphify (Graphify-Labs/graphify, PyPI `graphifyy`) | Apache-2.0 (earlier contributions under MIT, per NOTICE) | optional dependency, run as a CLI |
| Claude Code | Anthropic's terms | external tool the user installs |
| Codex CLI (openai/codex) | Apache-2.0 | external tool the user installs |
| uv | Astral (dual MIT / Apache-2.0, not verified) | external tool the user installs |
| superpowers (obra/superpowers) | MIT | optional recommendation, linked |
| mattpocock skills | MIT | optional recommendation, linked |
| netwaif/multi-agent-starter | MIT | ideas only (file-as-memory, backend registry, re-entry), credited |
| netwaif/knot | **no license** | ideas only, credited; no text or code copied |

`THIRD_PARTY_NOTICES.md` lists these. If anything is ever copied verbatim, its license text goes in
first.

## 10. Privacy guard for this repository

`tools/privacy_scan.py` (stdlib) runs before **every** push:

- It runs `gitleaks detect` across the git history, redacted.
- It regex-scans tracked and staged files for:
  - personal absolute home paths on macOS and Windows (placeholders allowed);
  - the owner's identifiers as whole words;
  - product names;
  - email addresses (GitHub noreply allowed);
  - API-key and access-token shapes (OpenAI, GitHub classic and fine-grained, Slack);
  - key, token and secret assignments in uppercase environment-variable style.
- It checks the author and committer emails of the commits about to be pushed.
- It prints `PRIVACY_SCAN_CLEAN` or the hits.
- Allowlist entries carry a one-line reason each.

No hook is installed on the development machine. The scan is a manual step in the process, and every
report includes its result.

---

## 11. Build phases

Each phase is one branch and one PR into `main` of the private repository.

| Phase | Content | Review effort |
|---|---|---|
| P0 | this plan | medium |
| P1 | skeleton: LICENSE, notices, `pyproject.toml`, `oah --version`, `oah doctor`, `.gitattributes`, `.gitignore`, `tools/privacy_scan.py`, test scaffold, CI workflow (approved) | medium |
| P2 | state and leases: paths, lock, atomic writes, lease store, `start/renew/status/finish/release/log` | xhigh (concurrency, Windows locking) |
| P3 | config, routing, engines: `recommend`, adapters, `call` with stage enforcement, runs dir, process-tree kill | high |
| P4 | packets, intent, preflight | high |
| P5 | gates: parser, unlazy delegation, `check` helpers, `finish` guard, Stop hook and `hooks install` | high |
| P6 | review loop: snapshot, canary, schema, receipts, rounds, `fix`, `review summary` | xhigh (trust boundary) |
| P7 | knowledge and agent files: `init`, `rules`, `tiers`, `wiki`, `graph`, `impact` | high |
| P8 | installers: `install.sh`, `install.ps1`, dry runs, install guides | xhigh (runs remote installers) |
| P9 | docs: README en/ko, concepts, worked example (`examples/hello-oah`), troubleshooting | medium |

**Process per PR** (the engine order the owner set):

1. **Claude implements**, tests first where practical. It runs the tests on Python 3.11 and the newest
   local Python, then the privacy scan.
2. **Codex reviews (R1).** It uses `codex exec --ephemeral -s read-only` on a sanitized snapshot of the
   branch head, with a canary and an output schema. The model is the owner's chosen reviewer model
   (currently `gpt-6.1-sol`); effort is from the table above. After P6, `oah review` dogfoods this.
3. **Codex fixes** the findings with `codex exec -s workspace-write` in a worktree of this repository.
   Claude commits the result, marked as a Codex correction. Then Codex re-reviews (R2, at most R3).
   Findings left after R3 go to the owner.
4. **Push and open the PR**, after the privacy scan is clean. CI runs on the PR. Claude merges once
   Codex review has converged, the scan is clean and CI passes, then reports the link.
5. **Report at each stop:** what was built, commit and PR links, test commands and results, the scan
   result, and open questions.

**Working rules.**
- This repository never uses the source harness's lock CLI.
- Once P2 lands, `oah` leases itself. State goes into this repository's own git common dir, which is
  inside the allowed write area.
- This repository's own development gate ledgers stay untracked, because their evidence can contain
  local paths.
  - Gates run from the separate development worktree, with `--root` / `--cwd` (when supplied) pointing
    there. `UNLAZY_APPROVAL_DIR` points to the main checkout's `.git/oah-dev/unlazy-approved`, outside
    that worktree's root even after canonical-path resolution, while keeping writes inside this
    repository's checkouts. Never run these gates from the main checkout itself: unlazy would refuse
    the store because it is inside that root.
  - The implementer's 2026-10-23 probe with unlazy 2.1.0 ran `gate-check.mjs --approve --cwd .`
    on a probe ledger from the development worktree with that store: approval was written and the
    gate returned `ALL MET (1 met)`, exit 0. P5 repeats this probe against its pinned unlazy commit
    and tests that running from the main checkout is refused.
  - Kit users keep unlazy's default store.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Windows cannot be tested on the development Mac | CI from P1 (needs OK); Windows-specific tests (paths, CRLF, cp949, `.cmd` shims) |
| Claude Code / Codex CLI flags change often | adapters isolated in `engines/`; fake-engine tests; `oah doctor` prints versions; the docs list tested versions |
| unlazy 2.1.0 is an unreleased source target | the docs pin a commit; built-in structural status as a fallback |
| graphify output format changes | the graph is an optional layer with tolerant parsing |
| Codex `workspace-write` makes `.git` read-only | `oah` writes state from the parent process; `OAH_STATE_DIR` override |
| Claude Code's optional sandbox may stop engines launched from inside it (Codex starting its own sandbox inside another; not verified yet) | verify in P3; the docs say to let `oah` run outside Claude's sandbox |
| Agent shell tools stop commands after about 10 minutes | `--background` plus `oah wait` (5.0) |
| Antivirus or indexers lock files on Windows | `os.replace` retry with backoff; short lock sections |
| Beginners' PATH problems | installers verify `oah` in a fresh process and print the exact fix |
| Scope creep (the source has 33 subcommands) | the v1 set in 5.13; everything else is marked "later" |

## 13. Decisions

**Owner decisions** (answered 2026-10-23):

1. **License.** MIT, with the copyright line "open-agent-harness contributors".
2. **Commit identity.** This repository commits with the owner's GitHub noreply address. The one-commit
   `main` was rewritten to it and force-pushed once, before any PR existed.
3. **CI.** The workflow lands in P1, and every PR runs one macOS and one Windows job.
4. **Merging.** Claude merges each PR once Codex review has converged, the privacy scan is clean and CI
   passes, then reports the link.
5. **This plan.** It still needs the owner's approval before P1 starts.

**Builder's calls** (change any of them):

*Runtime and layout*
- Python stdlib CLI.
- uv as the default installer.
- The command name `oah`, with the distribution named `open-agent-harness`.
- State in the git common dir.
- Per-task folders `.oah/tasks/<task>/`.
- Wiki in `docs/wiki/`.
- No Orca in v1.

*Workflow*
- Your interactive Claude Code session is the lead and does the initial implementation.
- `--background` and `oah wait` for long runs.
- Headless Claude defaults to `acceptEdits`, never `bypassPermissions`.
- Lease scope is advisory while working and checked at finish, review and fix.
- GitHub is optional, and preflight treats a repository with no remote as a WARN.

*Gates and review*
- unlazy, and therefore Node, is required for gates; a structural-only finish needs an explicit flag.
- `GATES.md` is git-ignored, with a committed path-free summary.
- The lead runs the gates.
- Engine attribution comes from commit trailers, with the `unknown` label.
- A lease-aware Stop hook that hands over to unlazy's.
- The `oah check` helpers.
- Review receipts kept in state, with `review summary` for PRs.

*Agent files*
- One rules block in `AGENTS.md`, with `CLAUDE.md` importing it.

*Docs and tests*
- All beginner docs in both English and Korean.
- Test temp files kept inside the checkout.

## 14. Evidence for the numbers in this plan

All commands are run from the source root, read-only.

| Number | Command |
|---|---|
| 286 files, 7.7 MB | `find . -path ./.git -prune -o -type f -print \| wc -l` · `du -sh .` |
| 10,635 lines, 33 subcommands | `wc -l bin/mah` · `grep -oE 'add_parser\(\s*"[a-z0-9_-]+"' bin/mah \| sort -u \| wc -l` |
| 39 helper modules (12 Orca, 11 OpenCode) | `ls bin/mah_*.py \| wc -l` · `ls bin/mah_orchestration*.py \| wc -l` · `ls bin/mah_opencode_*.py \| wc -l` |
| 69 contract tests | `find test -maxdepth 1 -type f ! -name '*.pyc' \| wc -l` |
| ~180 KB rule documents, ~130 KB rule fragments | `cat AGENTS.md TASK_ROUTING_POLICY.md ORCA_ADVERSARIAL_REVIEW.md README.md \| wc -c` (179,293) · `cat shared/*.md docs/*/*.md \| wc -c` (129,627) |
| 57 / 47 / 23 files with home path / product name / keychain mention | `grep -rlI --exclude-dir=__pycache__ --exclude-dir=.git '<pattern>' . \| wc -l` |
| 681 home-path lines in one fixture | `grep -c '<home path>' test/fixtures/<fixture>.jsonl` |
| round ceiling 3; stage table | `grep -nE '^REVIEW_ROUND_CEILING' bin/mah` · `config/routing.json` lines 5–9 |
