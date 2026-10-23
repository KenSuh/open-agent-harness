# open-agent-harness — Phase 0 plan

Status: **approved by the owner on 2026-10-23** · owner decisions in section 13 · amendments 1–5 (2026-10-23) in section 15 · intent revision 3 in section 1.

This plan turns a private, single-owner multi-agent harness (called "the source" below) into a
general-purpose kit that one person can install on macOS or Windows and use on their own repository.
Section 4 lists every source component and what happens to it. Sections 5–11 describe what gets built.
Section 13 separates what the owner decided from what the builder decided.

---

## 1. Intent

<!-- OAH_INTENT:BEGIN -->
## Intent
Intent-ID: open-agent-harness
Revision: 3
Status: accepted
### Problem
A beginner who starts programming with AI gets two strong tools, Claude Code and Codex CLI. They get none of the working environment that makes the two reliable together:
- shared leases;
- stage routing;
- evidence gates;
- bounded reviews with honest labels;
- a project memory;
- an impact map;
- skills and models that stay current.

The owner built that environment by hand for one machine; a beginner cannot.
### Desired outcome
On macOS or Windows, one install command and `oah init` build and run that environment for the beginner automatically, the same way the owner's harness works:
- Claude Code implements first.
- Codex reviews, corrects and re-reviews.
- The wiki, impact map, gates and hooks are created and kept current.
- Models and skills follow upstream updates.

Nothing paid or irreversible happens without the user's explicit confirmation.
### Success conditions
- AC1: On a fresh macOS 13+ or Windows 11 machine, one command from the README installs `oah`, and `oah --version` prints the version in a new terminal.
- AC2: In a new or existing project, `oah init` lists every missing tool with its official install command and installs only the tools the user confirms. It gets the user signed in to Claude Code and Codex through those tools' own login commands, and `oah` never sees a password, key or token.
- AC3: After `oah init`, the project has the rules block, tier agents, an Obsidian-compatible wiki, a fresh impact map, gate templates and the hooks the user accepted.
  - The defaults match the owner's workflow: Claude initial implementation, then Codex review, correction and re-review.
  - While work continues, the impact map and the wiki stay fresh automatically. They refresh on commit, merge and task steps, and their log, index and hygiene are maintained.
- AC4: Following the guided first task in `examples/hello-oah`, the beginner sees the whole loop finish: packet, preflight, lease, Claude implementation, gates, Codex review and fix, re-review, finish. Every step's result is explained in plain Korean or English.
- AC5: When a newer model or a validated skill update is available, the next task after oah's update check uses it without manual steps, and oah shows what changed, when and why. The model check runs before every launch; the skill check runs at least daily by default. A model or skill the user pinned is never changed silently.
- AC6: Every decision prompt says what will happen and what it costs. Nothing paid or irreversible happens without the user's explicit confirmation: purchases, account changes, pushes, merges and global file edits.
- AC7: The repository holds no private data from the owner's harness and no credentials of any kind (API keys, tokens, passwords), enforced by the scans of A17. Every bundled third-party item shows its source, version and license.
### Constraints
- macOS 13+ and Windows 11 natively (Windows 10 22H2 best effort), and Python 3.11+ with the standard library only (section 3).
- The owner's private harness is never a runtime dependency. `oah` follows it through fresh ports (A16).
- Third-party pieces are linked, depended on, or vendored only where their license permits it (A13).
### Non-goals
- Teams, servers, a web UI or remote telemetry.
- Linux as a supported OS. It may work, but it is not tested.
### Open questions
- None.
<!-- OAH_INTENT:END -->

**Revisions.** Revision 1 is the section 1 text the owner approved with this plan (PR #1). Revision 2
(2026-10-23) follows the owner's product vision, quoted verbatim: «우리 하네스를 그대로 초보 사용자가 본인 처음 AI를
통해 프로그래밍 할 때 자동으로 본인 하네스 환경이 우리 환경 처럼 구축되고 진행되도록 하는 깃 오픈 소스를 만들고 싶은거야».
AC3 and AC7 carry the two additions recommended with the acceptance. Revision 3 (2026-10-23) qualifies
AC5 by the update check, as the owner decided after review.

**GitHub is optional.** The worked example runs fully local in a fresh `git init` repository. A push and a
PR are the optional last step: `oah` prints the `gh pr create` command when `gh` and a remote exist,
and `oah review summary` prints the PR body.

**Release path.** v1 is built in this private repository. Going public is the owner's decision after a
final scan. The install docs cover a clone (works now) and a pinned-release bootstrap without git
(after it is public, 6.5). Publishing to PyPI is a later owner decision.

**Who it serves.** One person with one repository, beginners first. It needs at least one of Claude Code
or Codex, ideally both. It is not for teams or servers.

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
- Beginner onboarding that builds this environment automatically (6.5).
- Skills and models that are selected, updated and used automatically (A9, A10, A14, A15).

**Non-goals for v1.**
- Linux as a supported OS. It will probably work, but it is not tested.
- WSL as a requirement.
- Team or server mode, a web UI, or remote telemetry. Usage records stay local (A9).
- Orca by default. An optional Orca adapter is planned (row 8).
- Re-implementing what Claude Code or Codex already do, such as sandboxes, sign-in and subagents.

**Anti-goals (failure even if it "works").**
- A beginner has to read a long manual before the first useful run.
- The kit installs, changes or deletes anything global without asking: shell profiles, global agent
  settings, hooks.
- Private data from the source leaks into this repository.
- Third-party code is vendored where its license does not permit it, or without its LICENSE, a pinned upstream
  commit and a notice (A13).
- A credential of any kind enters the repository, its history, releases, issues or CI logs (A17).
- The kit works in bash but breaks in PowerShell, or only works on the author's Mac.
- Trust theater: a label or receipt that claims more than its mechanism proves.

## 3. Constraints

- **Platforms.** macOS 13+ and Windows 11 (Windows 10 22H2 best-effort, because Codex itself only
  recommends Windows 11). Native PowerShell. The Windows installer must run on the built-in
  Windows PowerShell 5.1.
- **Runtime.** Python ≥ 3.11, standard library only (`tomllib` needs 3.11). There are no third-party
  runtime dependencies. Node.js is needed only for unlazy's checker.
- **Beginner first.** There is one install command per OS. Every error says what to run next. Every
  optional feature is opt-in. Kit-cache skill updates are part of the guided defaults and may be disabled (A15).
- **Privacy.** The repository holds no personal paths, names, emails, product names, keys or vault material
  (section 10).
- **Licensing.** The kit is MIT, with the copyright held by "open-agent-harness contributors".
  Third-party pieces are linked or depended on. A skill is vendored only when its license permits
  redistribution: unchanged, with its LICENSE, a pinned upstream commit and a notice (section 9, A13).
- **Secrets.** No credential enters the repository. Keys are read only at runtime, from the environment or
  the OS keychain (A17).
- **CI cost.** The tests run on pull requests only, with one macOS job and one Windows job (section 8). A
  separate Linux secret-scan job runs on every pull request and push (A17).

---

## 4. Source inventory and classification

The source has 286 files and 7.7 MB. Its main CLI is one 10,635-line Python file with 33 subcommands and
39 helper modules; 12 of the modules are Orca-only and 11 are OpenCode-only. There are also 69 contract
tests, about 180 KB of top-level rule documents and about 130 KB of rule fragments. The measuring
commands are in section 14.

Verdicts: **keep** (port the idea, rewritten portable), **simplify** (port a smaller version that gives a
beginner the same outcome), **optional** (ship it, off by default, where it needs an account, a paid service or
another app), **later** (after v0.1.0), **drop** (private or owner-specific content, or something that cannot
work for a stranger).

**Re-judged on 2026-10-23 (amendment 3).** Every row was checked against one test: does a beginner get the
owner's working environment automatically? Only private or owner-specific content (names, paths, products,
accounts, vault material) and things that cannot work for a stranger stay out. Weight alone is no longer a
reason to drop a row; it ships with good defaults instead. Rows 2, 3, 7, 8, 9, 11, 17, 18, 21, 27–35 and 37
changed or gained a reason below.

| # | Source component | What it does there | Verdict | What the kit does instead |
|---|---|---|---|---|
| 1 | Lease core: `start`, `claim`, `finish`, `release`, `status`, `log`, `context-save/show` over JSON ledgers in a per-user state root | Shared locks and task notes so parallel agents don't collide; an event log | **keep** | `oah start/renew/status/finish/release/log`. One lease record per task, state under the git common dir, real path-prefix overlap, a portable lock (5.4) |
| 2 | Hardened state publication: inode exchange through `renameatx_np`/`renameat2`, hardlink first-publish, `unlinkat` quarantine, `O_NOFOLLOW` | Defends ledgers against symlink and hardlink races | **simplify (portable)** | Temp file + fsync + `os.replace`, with a Windows retry (5.4), provides atomic, durable publication in a user-controlled state folder assumed trusted. It does not defend against a local attacker who can redirect parent paths or race links |
| 3 | Git gate `mah git` (three allowlists; workers may not push or merge) | A mechanical stop on agents pushing | **keep, as opt-in hooks** | The rules block says workers never push. An opt-in `pre-push` hook refuses while an unfinished lease exists and runs gitleaks when it is installed (P5, A17) |
| 4 | Two lease roles (implementer vs. landing session) and the single-merge/push choreography (`READY_FOR_*` states) | Only one session lands | **simplify** | One rule: workers never push. You (the lead) push after `oah finish`. PR + CI decide |
| 5 | Worker call `mah call` (Claude or Codex) with an injected preamble | Runs another engine non-interactively with the protocol attached | **keep** | `oah call <engine> --stage <s>`: prompt on stdin, executable resolved per OS, process-tree kill on timeout (5.5) |
| 6 | Stage routing (`initial→claude`, `review/correction→codex`) plus kind/intensity/reasoning/risk scoring → profile → model/effort | Engine by stage, model and effort by difficulty | **keep, smaller** | `[stages]` and `[engines.*.tiers]` in `.oah/config.toml`, and `oah recommend` applied automatically by `call` and `packet new` (A10). No model ids are baked in (5.5) |
| 7 | Model catalog with TTL refresh (refuses to run when stale) | Keeps model lists current | **keep (A14)** | A model catalog per engine, read before every launch and refreshed when the CLI version changes or its TTL passes. Tiers follow the configured quality order and CLI default rules; changes are logged and explicit tier pins win (A14) |
| 8 | Orca dispatch: launch half of the dispatcher, launch matrix, heartbeat, orchestration gates and land queue (12 modules) | Supervised workers inside the Orca app | **optional** | An optional Orca adapter for `oah call` when Orca is installed (lane P3o). The default stays the plain CLIs |
| 9 | Tier subagents (5 Claude agent files, tier table, tier sync, PreToolUse tier guard) | Model + effort per subagent by difficulty | **keep; tier guard later** | `oah init` writes `.claude/agents/oah-{low,medium,high,max}.md`; `oah tiers sync/check`. The opt-in tier guard is deferred until after v0.1.0 |
| 10 | Rule-block sync (shared rules source, marker blocks in CLAUDE.md/AGENTS.md, AGENTS.md byte budget) | The same rules in every engine's instruction file | **keep** | `oah rules sync/check`: one block in `AGENTS.md`, and `CLAUDE.md` imports it with `@AGENTS.md`. Global files are never touched (5.11) |
| 11 | Session continuity (entry-prompt generator, SessionStart hooks) | A new session resumes where the last one stopped | **keep** | `oah handoff`, plus an opt-in SessionStart hook that prints active leases, the last log entries and the latest handoff, and nothing when `OAH_WORKER=1` (P7) |
| 12 | Dispatch packets (`mah packet`, worker brief template) | One file that holds intent, scope, preflight, gates and return format | **keep** | `oah packet new/check` → `.oah/tasks/<task>/PACKET.md` (5.6) |
| 13 | Original-intent block (`MAH_INTENT`: id, revision, status, six sections, sha256 digest, per-criterion `AC<n>: met/unmet/deferred` lines) | The goal stays byte-identical across implement, review and fix | **keep** | `OAH_INTENT` block, the same digest rule (CRLF→LF), `oah intent show/check` (5.6) |
| 14 | Preflight script: stale base, active locks, recent commits, dirty tree, migration-number check, impact on touched files; ABORT / PROCEED_WITH_WARNINGS / PROCEED | Stops work on a stale or contested base | **keep, port to Python** | `oah preflight`: generic checks; project-specific checks such as migration numbers become config commands (5.7) |
| 15 | Gate ledgers through unlazy: `finish` refuses unmet gates, isolated re-run, Stop-hook routing, static ledger lint | "Done" means evidence, not a claim | **keep** | unlazy runs the checks. `oah` has a read-only status parser, the `finish` guard and a lease-aware Stop hook (5.8) |
| 16 | Verify-what-changed rule: re-run only the gates bound to changed files; heavy gates once on the final head | Cheap review rounds without losing rigor | **keep** | Rule text in the rules block, plus `oah impact --changed-since` |
| 17 | Impact map: lexical `git grep`, gate-CHECK bindings (pin/exec), a product database backend, an auto-fresh worktree, an external re-ranking layer | "If I change X, what must I re-check?" | **keep (A11)** | `oah impact`, persisted and kept fresh. `oah init` builds it, it refreshes incrementally on commit, merge and task steps, and it records a freshness identity. It names exact gates (`gates#G3`), lexical and wiki references, co-change history and optional graph neighbours. A default SQL plugin gives a DB impact graph. An optional re-ranker may re-order results, never removes deterministic rows, and reports `used` or `unused(reason)` (5.10) |
| 18 | Edit-time impact hint hook | Shows bindings before editing risky files | **keep, opt-in hook (A11)** | An opt-in Claude Code hook prints the impact bindings before a risky file is edited (P7) |
| 19 | graphify wrapper (report/query/path/explain/update, with update blocked where unsafe) | Safe access to the knowledge graph | **keep, thin** | `oah graph …` when graphify is installed |
| 20 | Wiki audit, inspired by `netwaif/knot` (broken wikilinks, unindexed, missing source, no frontmatter, orphans, duplicates, stale) | Wiki hygiene | **keep** | `oah wiki lint`, our own code (knot has no license, so nothing is copied) |
| 21 | Wiki conventions: catalog index, newest-first log with an insertion marker, a source line on every note, wikilinks | Long-term project memory agents can trust | **keep (A12)** | `oah init` scaffolds an Obsidian-compatible `docs/wiki/`; the lead or guided defaults run `oah wiki maintain` before the final commit, gates and review, and `finish` only verifies maintenance (A12) |
| 22 | Review loop: R1–R3 round ceiling counted at call time, series key, per-series lock, explicit extra-round override | Bounded review cycles | **keep** | `oah review` with `max_rounds = 3` and `--extra-round` (5.9) |
| 23 | Review artifact schema and validation (verdict/findings consistency, base/head echo) | A machine-checkable review result | **keep, trimmed** | A JSON schema passed to `codex exec --output-schema`; one receipt per round |
| 24 | Canary token (random per call, written into the reviewer's tree, never in the prompt, must appear in the evidence) | Proves access to the snapshot's token file only | **keep** | Same mechanism (5.9) |
| 25 | Sanitized review snapshot (fresh clone at the exact head, agent instruction files removed, reviewer config and rules ignored, minimal environment) | Reduces automatic instruction loading from repo files | **keep** | Portable Python version that also strips `CLAUDE.md` and `.claude/` (5.9) |
| 26 | "Same engine is not independent" rule | Honest trust labels | **keep, as a label** | Receipt field `independent: true/false` and a visible warning |
| 27 | Reviewer binary pinning (hash + macOS code signature + login-mode requirement) | Trust the reviewer binary | **simplify** | Record the CLI path, version and sha256; pinning is optional. Re-judged: a code-signature check exists only on macOS, and the login-mode requirement is an account choice |
| 28 | Five-lane cooperative check (goal, QA, quality, security, context) | Self-check axes on the final head | **simplify** | The five axes (goal, tests, quality, security, context) become the review rubric and a checklist in `oah review summary` |
| 29 | Landing audit: review bundles bound to CI check-runs, fast-path vs. critical audits, landing-closure receipts, `verify-landing` | Final audit before merge | **keep, smaller** | `oah land --check` verifies that the converged review receipt, the CI check runs (through `gh`, when present), the scan and the gates all bind to the exact head. `--merge` lands only after the user confirms (P6) |
| 30 | Skill machinery: suggest/pack/route/audit/telemetry/lock, auto-maintenance, vendored third-party skills with local patches | Manage and inject skills | **keep, generic (A9, A13, A15)** | Skill selection per brief, bundled skills with provenance where the license allows, and automatic updates. The source's own machinery is not copied; the selector is new code |
| 31 | External AI re-ranking service layer (difficulty shadow judge, model-upgrade shadow, footers) | Optional AI re-ranking | **optional (A9, A10)** | A re-ranker plugin interface. Its first provider, TypeSafe Jev, runs only when explicitly enabled and its key is present, and its difficulty judgements are advisory (A9) |
| 32 | Experimental third engine and free-model variants (OpenCode, Spark, shadow runs) | Experiments | **later, optional** | An optional OpenCode engine adapter after v0.1.0. It is not part of the owner's default routing. Provider-specific free-model variants stay out |
| 33 | Retired engines (Antigravity, Grok, Kimi) | Old routing targets | **drop** | — (retired in the source too) |
| 34 | Web policy: route all web work to Aside and block built-in web tools | The owner's tool choice | **optional** | When Aside is installed, `oah init` offers the web rule (route web work to Aside). Otherwise the engines' built-in web tools stay |
| 35 | Owner operations scripts (pre-push checks with fixed paths, CI and lane watchers, two historical audit lanes, session context) | Daily operations | **simplify** | The pre-push secret scan ships as an opt-in hook (A17). The rest uses the owner's fixed paths and lanes |
| 36 | 69 contract tests | Guard the source | **drop** | New tests written from scratch (section 8) |
| 37 | Long rule documents (shared rules about 94 KB, routing policy about 47 KB, review procedure, rule fragments) | Operating manual | **keep, rewritten** | Rewritten as beginner docs in English and Korean (P9) |

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
  - It runs `oah` through its shell tool: `preflight`, `start`, `wiki maintain`, `gates check`, `review`,
    `fix`, `finish`. Tracked maintenance precedes the final commit, gates and review (5.10).
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
      GATES.summary.md   #   path-free summary written by `oah gates check` before commit and review
    templates/           # packet, gates and wiki-note templates the commands copy from
  docs/wiki/
    index.md             # catalog by category
    log.md               # newest first, under <!-- OAH:LOG_INSERT_BELOW -->
    log_archive_NN.md    # older whole log entries, moved by `oah wiki rollover`, never edited
    decisions/           # decision records (ADR style)
    raw/                 # optional: imported source documents, append-only
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
    models.py  secrets.py  skillsel.py  rerank/  setup/  handoff.py  land.py   # amendment 3
    templates/                     # rules block (en, ko), packet, gates, wiki, agents, config
  install/install.sh  install/install.ps1
  docs/  PLAN.md  install-macos.md  install-windows.md  concepts.md  example.md  (+ ko/)
  examples/hello-oah/              # tiny Python project for the worked example
  skills/                          # vendored skills, unchanged, each with its LICENSE and upstream pin (A13)
  mcp/                             # MCP launch templates for Claude and Codex, ${ENV_VAR} placeholders only (A13)
  docs/UPSTREAM_SYNC.md            # ports from the owner's harness, reviewed commit by commit (A16)
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
  note. While the repository is private, users install from a clone. Once public, the bootstrap installs
  from a verified pinned release archive without git (6.5), and later from PyPI. The name
  `open-agent-harness` is free on PyPI. `oah` is taken there
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
  on Windows. Critical sections are time-bounded; A15 updates hold the lock throughout validation
  and publication to serialize with lease acquisition.
- **Atomic writes.** Write a temp file in the same directory, flush, fsync, then `os.replace`. On Windows,
  retry a few times on `PermissionError` (antivirus and indexers hold files briefly).
- **Lease record.**
  `{id, task, agent, scope[], note, worktree, branch, head_at_start, packet?, intent{source,id,revision,digest}?, gates[], created_at, expires_at}`.
- **Overlap.** `repo` overlaps everything. Path scopes overlap when one normalized, case-folded,
  repo-relative path is a prefix of the other. The source compared exact strings only, so `src` and
  `src/a.py` did not collide there.
- **Scope is advisory while working and checked at the gates.** Nothing can intercept an editor's writes.
  So `oah` compares the actual changes (base..head plus the working tree) with the lease scope at
  `finish`, at `review` and after `fix`, and lists any files outside the scope. `finish` refuses until
  the scope is widened (`oah renew --scope …`) or `--allow-out-of-scope` is given. Both are recorded.
  Wiki maintenance outputs from `oah wiki maintain` are exempt from these scope checks: only
  `docs/wiki/log.md`, `docs/wiki/index.md` and newly created `docs/wiki/log_archive_NN.md`.
  Each exemption is recorded with the task and output digests in the events; other wiki edits need scope.
- **TTL.** The default is 4 h. `oah renew` extends it. `status` shows expired leases as STALE without
  deleting them. `start` prunes them.
- **Commands.**
  - `start` refuses on an overlap and names the holder.
  - `finish` checks the declared gates and wiki maintenance (5.10), records a runtime summary and
    releases. It writes nothing tracked.
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
quality_order = ["opus", "sonnet", "haiku"]  # strongest first; CLI aliases, editable
[engines.claude.tiers]
low    = { model = "", effort = "low",    timeout_min = 15 }
medium = { model = "", effort = "medium", timeout_min = 30 }
high   = { model = "", effort = "high",   timeout_min = 60 }
max    = { model = "", effort = "max",    timeout_min = 90 }

[engines.codex]
command = ["codex"]
quality_order = []  # default: effective configured model; user may name an ordered list
[engines.codex.tiers]   # model = "" uses A14, then the effective-config fallback below
low    = { model = "", effort = "low",    timeout_min = 15 }
medium = { model = "", effort = "medium", timeout_min = 30 }
high   = { model = "", effort = "high",   timeout_min = 60 }
max    = { model = "", effort = "highest", timeout_min = 90 }  # the highest effort the model supports
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
- **Effort comes from what the model supports, with no fixed cap.**
  - A non-empty tier model is an explicit pin and always wins. Otherwise use A14's automatic
    catalog mapping, then the engine's effective configuration for the launch directory, including
    the selected profile. For Codex, the adapter
    passes that concrete model with `-m` and records it. If no concrete default can be resolved
    (including on a fresh installation), it refuses before launch with `OAH_MODEL_UNRESOLVED` and
    tells the user to set the tier's `model` explicitly in `.oah/config.toml`.
  - `oah` reads that model's `supported_reasoning_levels` from `$CODEX_HOME/models_cache.json`.
  - With usable metadata, a literal tier effort must be on that list, otherwise
    `OAH_EFFORT_UNSUPPORTED`.
  - `highest` resolves to the highest listed reasoning effort. It skips modes that change behaviour,
    such as `ultra`, which adds task delegation. For `gpt-6.1-sol` on 2026-10-23 that is `max`.
  - The source's old "max → xhigh" cap was measured stale: the Codex CLI (and Orca 1.4.237) accept
    `max` for `gpt-6.1-sol`.
  - If the cache is missing or has no usable entry for that model, a literal effort is passed
    unchanged to the checks below. `highest` refuses before launch with `OAH_EFFORT_METADATA_MISSING`
    when no ordinary reasoning levels can be read; the message tells the user to refresh Codex's
    model metadata or set an explicit model and literal tier effort. It never guesses a maximum.
  - When a launcher cannot run the requested effort, `oah` stops with `OAH_SEPARATE_SESSION_REQUIRED`
    and prints the exact command for a separate session.
- **Launch configuration is checked separately from inference effort.**
  - `requested_effort` is the tier value (including `highest`); `launch_effort` is the literal value
    passed to Codex after resolving `highest`. `header_effort` is the header's `reasoning effort: <e>`,
    which reports `config.model_reasoning_effort`, not proof of the inference effort actually used.
  - `oah` compares `header_effort` with `launch_effort`. A lower configuration fails with
    `OAH_EFFORT_DOWNGRADED`; any other mismatch fails with `OAH_EFFORT_MISMATCH`. A failed review
    round does not count. An unreadable header is recorded as `unverified`, never as a match.
  - The adapter uses normalization rules verified for the recorded CLI version and model to map
    both `launch_effort` and `header_effort` to inference efforts. For example, `ultra` may normalize
    to another inference effort; the adapter must not compare those labels as an ordinal scale.
    `resolved_inference_effort` stores the header-derived value and `normalization_basis` identifies
    the rule used. A value lower than the normalized launch effort fails with
    `OAH_EFFORT_DOWNGRADED`.
  - Unknown normalization records `resolved_inference_effort: unverified`. A matching header proves
    only the launched configuration; a known mapping describes expected inference behavior, not
    runtime observation. A review counts as effort-verified only with a matching header and known
    normalization. `oah` never lowers an effort silently.
  - For Claude, P3 verifies whether the inference effort can be observed. Until then it is recorded
    as unverified.
- **Runs.** Each call writes `prompt.md`, `stdout.log`, `stderr.log` and `result.json` (exit code,
  duration, CLI version, resolved model, `requested_effort`, `launch_effort`, `header_effort`,
  header-check status, `resolved_inference_effort` and `normalization_basis`; unavailable values are
  `unverified`). The preamble is a short template:
  task, stage, scope, rules (stay in scope, never push, write evidence into the ledger), the packet,
  and the return format.
- **Dispatched workers are marked.**
  - Every worker `oah` launches gets `OAH_WORKER=1`, `OAH_RUN_ID` and `OAH_TASK` in its environment.
  - The first line of its preamble says it is a dispatched worker.
  - Workers skip any session-entry procedure (5.11, 5.12).

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
## Return       (INTENT <id>@r<n> <digest> + one "AC<n>: met|unmet|deferred — evidence" line each;
                rerank=used|unused(<reason>) when impact was used; the DB-impact line for DB changes)
```

- `oah packet check` verifies the required sections, unique AC ids, `Status: accepted` before
  `start --packet`, and that the ledger parses.
- **Digest.** sha256 over the bytes from BEGIN to END, after CRLF→LF and with one trailing LF. Changing
  the intent means a new revision, never an in-place edit.
- **Binding.** The lease records `{source, id, revision, digest}`. Review prompts carry the intent
  verbatim, and receipts record the digest. A mismatch is reported.
- **Intent lineage (A7).** One section carries the goal through every stage.
  - **One source.** The packet's `OAH_INTENT` section is the only statement of the goal for every stage:
    packet authoring, implementation, review and correction. That holds for the lead, workers and
    subagents alike.
    - Review and correction briefs carry the same section byte for byte.
    - Round instructions live outside the section.
    - Later rounds reuse the intent source bound at the first dispatch. A brief whose source differs
      from the binding refuses with `OAH_INTENT_SOURCE_MISMATCH`; changing the goal means a new
      revision (A19).
  - **Revisions, not new IDs.** A changed goal raises `Revision` of the same `Intent-ID`, and needs
    `Status: accepted` again. A round never mints a new ID.
  - **Outcomes only.** Success conditions describe outcomes. Process steps (lease, commit, review
    rounds, return format) live outside the section. `oah packet check` warns
    (`OAH_INTENT_PROCESS_STEP`) when a condition reads like one.
  - **Bound in the same start command.**
    - `oah start --packet <PACKET.md>` binds the lease to `{source, id, revision, digest}`.
    - `oah status` and `oah intent show` use the first matching predicate below:
      - `unbound`: the lease has no intent binding;
      - `missing`: a binding exists, but its source does not exist;
      - `current`: the source is readable and contains exactly one well-formed `OAH_INTENT`
        marker pair and section, with `Status: accepted`, the bound ID and revision, and the
        bound digest using the normalization above;
      - `stale`: a binding exists and its source exists, but any `current` check fails. This includes
        unreadable content, missing, malformed or duplicate markers, invalid fields, a non-accepted
        status, or a changed identity or digest. Invalid content can never be `current`.
    - `oah call`, `oah review` and `oah fix` refuse to dispatch for a task whose intent is not
      `current` (`OAH_INTENT_NOT_CURRENT`).
    - A worktree selector resolves to exactly one repository before that check. An ambiguous or
      unresolvable selector refuses before launch with `OAH_WORKTREE_UNRESOLVED` and a runnable retry
      command (A19).
  - **Returns.** Every return, from the lead, a worker or a subagent, carries `INTENT <id>@r<n> <digest>`
    and one `AC<n>: met|unmet|deferred — <evidence>` line per criterion. `oah finish` and `oah review`
    check those lines structurally against the binding: exactly one matching `INTENT` line and one
    valid status/evidence line per bound AC. Missing, malformed, mismatched or duplicate lines and
    invented ACs refuse with `OAH_INTENT_RETURN_INVALID`.
  - **One shared footer.** A single canonical intent footer states these rules.
    - `oah call`, `oah review` and `oah fix` append it to every worker brief.
    - The tier agent files carry it inside their managed block, replaced on every sync (A5).
  - **A draft cannot bind.** `oah packet new` scaffolds `Status: draft`, and `oah start --packet` refuses
    with `OAH_INTENT_NOT_ACCEPTED` until the status says `accepted`.
- **Not ported.** The source's POSIX owner and link-count checks on the packet file. The kit checks:
  regular file, not a symlink, under 1 MiB, unchanged between stat and read.
- **An empty Scope is never silent.**
  - `oah packet check` warns loudly (`OAH_SCOPE_EMPTY`) when `## Scope` lists no paths.
  - `oah start --packet` refuses until Scope has paths, or until `--scope repo` is given explicitly.
  - Anything that selects by the file kinds in Scope prints the same warning and never silently
    selects nothing. That covers skills, tools, preflight checks, gate templates and impact queries.
- **"No network" is narrow.**
  - A packet constraint such as "no network" forbids installs, fetches and external web access.
  - It does not turn off `oah impact` or a consent-enabled re-ranker with a key. The brief must name
    either to disable it; provider consent and key checks still apply (5.10).

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
  dirs. Before any gate command runs, verify the discovered copy's commit and scripts digest against
  P5's contract-tested pin. A mismatch refuses without executing it or falling through to another copy;
  the message identifies the mismatch, gives `oah init --with-unlazy --force` to install the pinned
  project copy, and tells the user to clear an incompatible `OAH_UNLAZY_DIR` override if present.
  **Running** a CHECK always goes through unlazy's own approval flow; `oah` never executes CHECK lines itself.
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
  - `oah gates check` writes a path-free `GATES.summary.md` before the final commit and review;
    `oah finish` only verifies it matches current gate records. It contains only validated `G<n>` ids,
    fixed status enums and sha256 digests of titles, CHECK, EXPECT, evidence and input/carry records.
    No raw titles, commands, expectations, paths, task labels or other free text are exported; the
    writer rejects fields outside this allowlist. Full text stays in the ignored ledger or runtime state.
  - `[gates] commit_ledgers = true` commits the full ledger instead.
- **Pinned contract.** P5 pins an unlazy commit and scripts digest and writes down its CLI contract:
  arguments, output and exit codes. `oah init --with-unlazy` installs that contract-tested commit and
  verifies both identities; it never installs an unversioned upstream copy. unlazy is an operational
  dependency excluded from A15 updates. A new commit is adopted
  only by an explicit change passing the adapter contract and gate-invalidation tests against that
  candidate commit; stub tests alone do not authorize a new pin.
- **Stop hook (opt-in).** `oah hooks install` adds a Claude Code Stop hook that runs `oah hook stop`.
  When an active lease governs the session's worktree, the hook blocks while that task's ledger is
  unmet. A progress guard releases after N blocks with no progress, so it cannot wedge. When no lease
  governs, the hook hands over to unlazy's own stop hook only after the same pin verification. The hook writes to
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
  - **`oah wiki maintain --task T`** performs tracked log, index and size-based rollover maintenance
    before the final commit, gates and review. The lead runs it, or guided defaults run it at those
    task steps, including after corrections. It uses the exclusive wiki lock and journal/recovery
    protocol below. A task ID makes log/index insertion idempotent, including after rollover;
    retry verifies existing entries and completes missing work without duplicates. Lock contention
    waits with bounded retry, then refuses with a retry message; a pending journal requires explicit
    recovery. Successful output paths and digests are recorded in events as the exempt maintenance
    set in 5.4. `finish` only verifies the task entry and maintenance record against current wiki
    bytes, and runs read-only lint/hygiene. Missing or stale maintenance refuses with a message to run
    `oah wiki maintain --task T` before commit, gates and review again. Only
    `--allow-missing-wiki-maintenance` overrides this refusal, and the override is recorded in events.
  - `oah wiki lint` is read-only. It reports broken wikilinks, unindexed notes, missing sources, missing
    frontmatter, orphans, duplicate titles and stale notes, as text or JSON.
  - **Auto-clean (A8): candidates only.** No wiki command deletes, merges or moves a note on its own.
    - **`oah wiki lint` counts only real problems.**
      - It resolves links whose targets exist outside the wiki, under the configured `[wiki] link_roots`,
        such as agent memory folders.
      - It ignores links inside code spans and code blocks.
      - It counts archive indexes as index sources.
      - It judges staleness by the note's last commit date (`git log -1`), not by file mtime.
    - **`oah wiki lint --index`** reports banners, over-long lines and truly unindexed notes in
      `index.md`. Outside fenced code, a banner is a trimmed line matching `^#{1,6} +.*$` or
      `^(?:[-=*_] *){3,}$`; an over-long line exceeds `[wiki] index_line_limit` Unicode code points
      (default 120, excluding CR/LF). Unindexed notes use the normal lint's index-source rules.
      It writes candidate lists outside the wiki, under the state folder.
    - **`oah wiki rollover`** moves the oldest whole entries of `log.md` into the next numbered
      `log_archive_NN.md`.
      - Defaults are exactly 512,000 bytes to trigger (strictly greater) and 409,600 bytes as the
        target maximum for the retained entry bytes. The trigger counts the whole file, including
        metadata. Headers and the generated archive index are excluded from the target and entry
        stream; a metadata-only excess with entry bytes already at target is a no-op.
      - Only the trigger and target are `[wiki]` config values with these defaults. Archive names
        stay `log_archive_NN.md`, as required by scope exemptions, lint and recovery (A18).
      - `oah wiki rollover` prints its plan and writes nothing unless `--apply` is given. `--apply`
        needs a lease whose scope covers `log.md` and the new archive, or refuses with
        `OAH_WIKI_LEASE_REQUIRED`. `oah wiki maintain` applies rollover under the wiki lock without a
        lease, because every task appends to the log (A18).
      - Entries begin at unindented `## YYYY-MM-DD <title>` lines outside fenced code, below the
        single `<!-- OAH:LOG_INSERT_BELOW -->` marker in each log or archive. Headers and the
        generated table between `<!-- OAH:ARCHIVE_INDEX:BEGIN -->` and
        `<!-- OAH:ARCHIVE_INDEX:END -->` stay above that marker. Each entry includes every byte up
        to the next entry or EOF, including blank lines. `wiki log` emits this format. Nonempty
        content between the marker and first entry, invalid dates or ambiguous boundaries refuse
        with `OAH_WIKI_LOG_INVALID`; blank prefix bytes there remain with their file. The refusal
        happens before any write, and its message names the case, such as a missing marker, an
        undated first entry heading or an unclosed fence (A18).
      - Entries stay newest first within each file. Move the smallest oldest suffix that leaves
        retained entry bytes at or below target, keeping at least the newest entry. A newest entry
        larger than target stays whole and is reported as oversized; an oversized older entry moves
        whole. If no entry can move, report oversized without creating an archive; otherwise one
        run creates one archive, even when it exceeds target.
      - Archive numbers start at 01 and increase by one above the highest existing number, with at
        least two digits and no reuse. The preservation stream concatenates entry bytes from
        `log.md`, then archives in descending numeric order, newest first within each. It must be
        byte-identical before and after rollover, with no newline normalization. Generated archive
        headers and the marked archive-index table are metadata, excluded from that stream.
      - All wiki commands, including rollover and recovery, hold one exclusive wiki lock. Under it,
        snapshot the log and archive set, stage the archive and replacement log, and durably write
        a state-folder journal containing paths, original and replacement bytes/digests, and phase.
        Revalidate the source bytes and archive set immediately before publication; a difference
        refuses with `OAH_WIKI_CONCURRENT_EDIT` before publishing either file.
      - Publish the archive first without replacing an existing path, then atomically replace the
        log (including its generated archive index). Flush files and directories at each step;
        remove the journal only after both are durable. Individual writes use atomic replacement.
      - Before removing the journal, re-read both files. If both hold exactly the published bytes,
        remove the journal. Otherwise leave every wiki file as it is and record the live bytes in
        the journal with reason `changed-after-publication`; commands refuse with
        `OAH_WIKI_RECOVERY_PENDING`. For this reason, `oah wiki recover` refuses with
        `OAH_WIKI_RECOVERY_CONFLICT` and names the files that differ and the journal (A18).
      - Before any wiki read or write, check for a pending journal under the lock. Commands other
        than `oah wiki recover` refuse with `OAH_WIKI_RECOVERY_PENDING`, leave wiki files and the
        journal unchanged, and direct the user to run `oah wiki recover` explicitly. That command
        recovers under the same lock: an unchanged original log with an absent or matching new
        archive is completed forward; matching replacement log
        and archive is already complete. Recovery is idempotent. Any other bytes refuse with
        `OAH_WIKI_RECOVERY_CONFLICT`, preserve all files and journal, and name the files that differ
        and the journal. Editors outside the lock protocol must be quiescent during publication;
        edits detected before publication or during recovery are refused as above.
      - For every `OAH_WIKI_RECOVERY_CONFLICT`, the user reconciles the files by hand, then runs
        `oah wiki recover --resolved` under the same wiki lock. It checks that the log and all
        archives parse and that no entry appears twice. If any check fails, it refuses with
        `OAH_WIKI_RECOVERY_CONFLICT` and changes nothing. If all checks pass, it first lists
        every entry present in the journal's original snapshot or captured live bytes but absent
        from the reconciled files, comparing exact entry bytes. Missing entries never block
        resolution. It then moves the journal byte-for-byte to a timestamped resolved record in
        `resolved-records/` under the state folder and records the resolution time and record path
        in events. `--resolved` never deletes journal bytes (A18).
    - **`oah wiki hygiene`** prints one line of `count(delta)` values for `broken_links`, `unindexed`,
      `missing_sources`, `missing_frontmatter`, `orphans`, `duplicate_titles`, `stale`, `banners` and
      `overlong_lines`, using the normal and index lint definitions. After a successful scan it
      atomically stores counts and effective lint configuration in the state folder. The first run,
      or a configuration change, prints `delta=n/a` for every counter and establishes a baseline;
      otherwise each signed delta is the current count minus the previous successful run's count.
      A failed scan leaves the baseline unchanged.
    - **An opt-in commit check, `oah hook pre-commit`,** blocks a commit that:
      - shortens `log.md` without adding the archive files;
      - edits or deletes an existing archive;
      - edits or deletes a file under `raw/`;
      - adds non-UTF-8 or surrogate bytes to a wiki file.

      A commit that adds NEW broken links is warned first, and blocked once
      `[wiki] new_broken_links = "block"` is set.
    - **`oah init`** git-ignores editor UI state such as graph zoom, for example
      `.obsidian/workspace*.json`.
- **Skill maintenance (A15).** Automatic checks refresh only the kit cache in the state folder, outside
  the working tree. Tracked project skill folders are pins; changing them requires an explicit
  `oah skills update --project` in its own leased task with its own scope, without the wiki maintenance
  exemption. Selection prefers the project copy, then the kit cache, then user and plugin copies.
- **Graph (optional).** `oah graph update|query|path|explain|report` is a thin pass-through to
  graphify, run when it is installed (`uv tool install graphifyy`). `oah init --with-graph` writes
  `.graphifyignore` (wiki log, build outputs, `.oah/tasks/*/GATES.md` evidence).
- **Impact map.** `oah impact <path>… [--changed-since REV] [--strong-only] [--json] [--ref REV]` uses
  deterministic layers only. Results rank by binding (pin > exec > link > mention).
  Maps are stored per worktree in the state folder. Freshness binds the worktree identity and an
  effective input manifest: HEAD SHA, a digest of tracked index state, path/byte digests of dirty and
  untracked inputs the map reads, all ledger bytes (including ignored ledgers), config bytes and the
  tool version. Any identity or manifest change makes the map stale; record the result digest too.
  Compare the manifest before and after building; changed inputs require a retry or stale refusal.
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
  ending with `total/shown/overflow`. Exit codes: 0 ok, 2 usage, 69 environment.
- **Re-ranker (optional plugin, later).**
  - A configured re-ranker may re-order impact results. It never removes deterministic pin or exec rows.
  - A provider runs only when explicitly enabled by the user AND its key is present. Guided `oah init`
    asks once, explains potential charges and the candidate/evidence data sent, and records the
    choice; the user may instead enable it in config. A key alone never enables it. A brief can also
    disable it (`--no-rerank`, or the packet says so).
  - Every impact output, and every return that used impact, reports `rerank=used` or
    `rerank=unused(<reason>)`. Reasons include `not-enabled`, `no key`, `disabled-by-brief` and
    `error:<code>`.
- **DB backend plugin (later; the rule already applies).**
  - Project-specific backends, for example a database-object map, plug in as commands that print
    JSON for entities such as `db_table:<name>`, `db_routine:<name>` and `db_grant:<name>`.
  - Any task or review that touches DB tables, routines or GRANTs must query that plugin.
  - Its return must include the query plus the graph identity: commit SHA and output digest.
  - A project without the plugin returns `db-impact: unavailable (no plugin configured)` instead of
    skipping silently.

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
  - Rules and tier sync never write global files. Automatic skill updates write only the state-folder
    kit cache; tracked project skill pins require `oah skills update --project` in a separate leased
    task with its own scope. Global skill updates require `oah skills update --global` with confirmation (A15).
- **The block itself is under 6 KB.** It covers the stage order, leases, packets and intent, preflight,
  gates and the verify-what-changed rule, the review loop and its honest labels, "workers never
  push", keeping the wiki current, and "dispatched workers (`OAH_WORKER=1`) skip any session-entry
  procedure and do only the task in their prompt". It also states A15's cache-only automatic updates,
  project skill pins and separate leased project updates, and 5.8's unlazy pin verification.
- `oah init --with-agents` writes `.claude/agents/oah-{low,medium,high,max}.md` (frontmatter: `name`,
  `description`, `model`, `effort` from the config). The body says: stay in scope, cite evidence, no
  destructive git, and return honestly when blocked.
- `oah tiers sync|check` owns the frontmatter `model` and `effort` lines, plus a managed rules block in
  the body (`<!-- OAH:TIER-RULES:BEGIN v1 -->` … `<!-- OAH:TIER-RULES:END -->`).
  - Sync **replaces** the block with the canonical text every time, so a wording change reaches
    existing files.
  - The block holds the rules and the shared intent footer (A7).
  - It inserts the block when it is missing and refuses broken markers.
  - `check` reports drift when either the frontmatter or the block differs from canonical.

### 5.12 Hooks — all opt-in, project-local, removable

| Hook | Command | Phase |
|---|---|---|
| Claude Code Stop: block while the governing lease's gates are unmet | `oah hook stop` | P5 |
| Claude Code SessionStart: active leases, last log entries, latest handoff; prints nothing when `OAH_WORKER=1` | `oah hook session-start` | P7 (row 11) |
| git pre-push: refuse while an unfinished lease exists on this worktree; gitleaks over the pushed commits when installed | `oah hook pre-push` | P5 (row 3, A17) |
| Claude Code PreToolUse: impact hint before editing gates, workflows, migrations | `oah hook impact-hint` | P7 (A11) |
| git pre-commit: wiki commit check (archives, raw sources, encoding, new broken links) | `oah hook pre-commit` | P7 |
| git post-commit and post-merge: refresh the impact map incrementally | `oah hook post-commit`, `oah hook post-merge` | P5 installer, P7 command (A11) |

`oah hooks install|uninstall|status` shows the exact change before it writes. It writes atomically and
leaves a backup.
`oah hook <name>` runs the module `oah.hook_<name>`. P5 owns the installer, and each phase adds its hook
modules without editing it. `oah init` offers every hook with the owner's defaults, and the user confirms
each one (6.5).

### 5.13 v1 command set

| Group | Commands |
|---|---|
| Setup | `doctor`, `init` (guided, 6.5), `models` (A14), `skills list/install/update` (A13, A15) |
| Task | `packet new/check`, `intent show/check`, `preflight`, `start`, `renew`, `status`, `finish`, `release`, `log`, `handoff` (row 11) |
| Engines | `recommend`, `call` |
| Gates | `gates new/status/check/lint`, `check exists/contains/absent/json` |
| Review | `review`, `fix`, `review summary`, `land --check/--merge` (row 29) |
| Knowledge | `wiki new/log/maintain/lint/rollover/recover/hygiene` (`lint --index` included), `graph …`, `impact`, `impact build/refresh` (A11) |
| Agent files | `rules sync/check`, `tiers sync/check` |
| Hooks | `hooks install/uninstall/status`, `hook stop/pre-commit/pre-push/post-commit/post-merge/session-start/impact-hint` |

`oah doctor` checks git, Python, Node (only when gates are used), uv, claude, codex (`codex login status`)
and the recommended and optional tools from one catalog (A13). It prints the official install command for this OS for each missing one, and
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
| unlazy (required for gates) | running gate checks, Stop hook | `oah init --with-unlazy` (P5 pin, verified commit and scripts digest) | same | https://github.com/Leonxlnx/unlazy |
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

### 6.5 Beginner onboarding (amendment 3)

This is the whole path a beginner follows, in plain Korean or English at every step. Nothing paid or
irreversible happens without the user's explicit confirmation.

1. **One command per OS, in two distribution forms.**
   - **Private repository:** the README command runs from a clone: `bash install/install.sh` on
     macOS, or `powershell -ExecutionPolicy Bypass -File .\install\install.ps1` on Windows. Obtaining
     git first is a documented prerequisite: `xcode-select --install` on macOS, or
     `winget install --id Git.Git -e` on Windows; then clone the repository.
   - **Public repository:** the README gives `curl <pinned-release-bootstrap-url> | bash` on macOS,
     or `irm <pinned-release-bootstrap-url> | iex` on Windows. Without requiring git, the bootstrap
     downloads a pinned release archive, verifies its SHA-256 against the release's expected digest
     before extraction, installs uv and `oah`, and offers git installation with consent. The release
     URLs and digest are placeholders until the owner publishes; the private form is a preview path,
     and the public form supplies AC1's fresh-machine path.
   - Both forms obtain confirmation for installs and global changes (AC6).
   - It checks that `oah` resolves in a fresh shell.
   - It hands over to `oah init`.
2. **`oah init` is a guided setup.**
   - **Tools.** It checks every tool in the catalog (`oah.setup`) and shows each missing tool's official
     install command and URL. It installs only the tools the user confirms. `--yes` confirms them all; nothing
     else does.
   - **Sign-ins.** It walks the user through the Claude Code and Codex sign-ins. It names the command to run
     in their own terminal, then verifies the result with that tool's own status command. `oah` never reads,
     stores or asks for a password, key or token.
   - **The environment.** It creates the project environment with defaults that match the owner's workflow:
     - stages Claude → Codex → Codex;
     - tier agents whose difficulty-based models follow updates (A10, A14);
     - the rules block (5.11);
     - the Obsidian wiki (A12);
     - the first impact map (A11);
     - gate templates and, with consent, unlazy at P5's contract-tested commit, verified by commit and
       scripts digest before any gate command (5.8);
     - automatically selected skills and kit-cache updates, checked daily by default; tracked project
       skills stay pinned until an explicit `oah skills update --project` in its own scoped, leased task (A9, A15);
     - `.gitignore` entries for editor state and local secret files (A8d, A17);
     - a starter GitHub Actions workflow for the project, shown first and written only with consent
       (A21);
     - optional companion apps, each offered with what it adds: Orca to run agents side by side in
       worktrees, and Aside as a web agent through MCP (A20);
     - each opt-in hook the user accepts (5.12).
   - A second run refreshes a stale impact map and otherwise changes nothing (section 7).
3. **A first guided task.** `examples/hello-oah` walks the whole loop end to end: packet, preflight,
   lease, Claude implementation, gates, Codex review and fix, re-review, finish, and the optional PR.
4. **Plain language.** Docs and messages exist in English and Korean (P9), and every error says what to
   run next.

The work is split across lanes: P8 (the installers and the tool catalog), P8c (the companion apps, A20),
P7c (the CI starter, A21), P7b (`oah init`) and P9 (the example and the docs).

## 7. `oah init`

Amendment 3 makes `oah init` the guided setup of 6.5. It runs the tool checks, the consent-based installs and
the sign-in walkthrough, then creates the environment and persisted first impact map below. Each `--with-*` option becomes
a question in the guided flow, and the flag answers it without asking.


```text
oah init [--lang en|ko] [--with-agents] [--with-hooks] [--with-unlazy] [--with-graph] [--dry-run] [--force]
```

- **Plans before writing.** It prints the list of files it will create or change, then writes only
  missing files. Existing files are never overwritten without `--force`; managed blocks are the
  exception, and `rules sync` updates those in place.
- **Always creates** `.oah/config.toml` (with the defaults from 5.5 and the detected default branch),
  `.oah/templates/`, the managed block in `AGENTS.md`, the `@AGENTS.md` line in `CLAUDE.md`, and
  `docs/wiki/` (index, log with marker, decisions template).
- **Impact map.** After scaffolding, init builds the first persisted per-worktree map using A11's
  effective input manifest. A build failure warns and records `impact=unavailable` without failing
  the rest of init. It prints the build retry command; it never labels an unavailable map fresh.
- **Dry run.** `--dry-run` lists the map build along with planned installs and file changes, and
  runs none of them.
- **Options.**
  - `--with-agents`: the tier agent files.
  - `--with-graph`: `.graphifyignore`, plus a hint to run `oah graph update`.
  - `--with-unlazy`: installs the P5 contract-tested unlazy commit as a project copy after a prompt,
    verifying the commit and scripts digest (5.8); `--force` explicitly replaces an incompatible copy.
    No unversioned install command is used.
  - `--with-hooks`: the Stop hook, after showing the diff.
- **Skill updates.** Guided setup enables cache-only automatic updates with `update_interval_days = 1`
  by default (A15). Tracked project skills remain pins; only `oah skills update --project`, as its own
  leased task with its own scope, changes them. Global updates require `--global` with confirmation.
- **Idempotent.** A second run refreshes a stale or unavailable map using the same failure handling;
  with a fresh map and unchanged inputs it changes nothing and says so.

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
- A separate workflow, `secrets.yml`, runs on every pull request and push, docs-only changes included
  (A17). It uses one ubuntu-latest job: gitleaks 8.30.1, pinned by SHA-256, scans the full history,
  and `tools/privacy_scan.py --gitleaks require` scans the tree and commit identities. Any finding fails
  it.

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
| unlazy (Leonxlnx/unlazy) | MIT | operational dependency installed by `oah init` at P5's contract-tested pin, verified by commit and scripts digest (5.8); vendored under A13 |
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

**Amendment 3 (A13).** A skill is vendored only where its verified upstream license permits it. Section 15
lists every item with its verdict. Each vendored skill ships unchanged under `skills/<name>/`, with its
LICENSE, upstream URL and pinned commit. Its `THIRD_PARTY_NOTICES.md` entry is added in the same PR.

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
It also runs in CI on every pull request and push, and over the full history before a release tag. Any
finding fails the build, and no allowlist entry ever covers a real-looking credential (A17).

---

## 11. Build phases

Each phase is one branch and one PR into `main` of the private repository.

| Phase | Content | Review effort |
|---|---|---|
| P0 | this plan | medium |
| P1 | skeleton: LICENSE, notices, `pyproject.toml`, `oah --version`, `oah doctor`, `.gitattributes`, `.gitignore`, `tools/privacy_scan.py`, test scaffold, CI workflow (approved) | medium |
| P2 | state and leases: paths, lock, atomic writes, lease store, `start/renew/status/finish/release/log` | highest = `max` (concurrency, Windows locking) |
| P3 | config, routing, engines: `recommend` applied automatically (A10), adapters, `call` with stage enforcement, runs dir, process-tree kill, skill selection and the re-ranker interface (A9), the model catalog (A14), the key lookup (A17), an optional Orca adapter | high |
| P4 | packets, intent, preflight; skills and difficulty in `packet new` (A9, A10); impact refresh (A11) | high |
| P5 | gates: parser, unlazy delegation, `check` helpers, `finish` guard, Stop hook and `hooks install`, git hooks (pre-push, post-commit, post-merge) | high |
| P6 | review loop: snapshot, canary, schema, receipts, rounds, `fix`, `review summary`, impact refresh (A11), `land --check` (row 29) | highest = `max` (trust boundary) |
| P7 | knowledge and agent files: guided `init` (6.5), `rules`, `tiers`, the Obsidian `wiki` (A12), `graph`, the persisted `impact` map with the SQL plugin (A11), the toolkit and skill updates (A13, A15), `handoff` and SessionStart (row 11) | high |
| P8 | installers: `install.sh`, `install.ps1`, dry runs, install guides, the tool catalog and guided installs (6.5), `doctor` tool checks (A13) | highest = `max` (runs remote installers) |
| P9 | docs: README en/ko, concepts, worked example (`examples/hello-oah`), troubleshooting, the Toolkit page, the upgrade path (A16), where users keep their keys (A17) | medium |

**Lanes (amendment 3).** To finish quickly, phases are built in parallel lanes. Each lane is one branch, one
worktree, one Claude implementer and one Codex review series, and lands as one PR in dependency order (the
lane table is in section 15). A lane is rebased onto `main` only after what it needs has landed.

**Process per PR** (the engine order the owner set):

1. **Claude implements**, tests first where practical. It runs the tests on Python 3.11 and the newest
   local Python, then the privacy scan.
2. **Codex reviews (R1).** It uses `codex exec --ephemeral -s read-only` on a sanitized snapshot of the
   branch head, with a canary and an output schema. The model is the owner's chosen reviewer model
   (currently `gpt-6.1-sol`). Effort is from the table above. "highest" is the top effort the model
   lists as supported (5.5), so the toughest setting is `max`. The runner resolves the model and
   metadata before launch, checks the header-reported configuration against `launch_effort`, and
   records the normalized `resolved_inference_effort` separately (5.5). A mismatch fails the round;
   an unreadable header or unknown normalization leaves effort unverified. Neither counts as an
   effort-verified review. After P6, `oah review` dogfoods this.
3. **Codex fixes** the findings with `codex exec -s workspace-write` in a worktree of this repository.
   Claude commits the result, marked as a Codex correction. Then Codex re-reviews.
   - **Standing fix-round rule** (owner, 2026-10-23, every phase): continue automatically to the next
     correction and re-review when every new finding is P0 or P1, small, inside the phase scope, and of a
     class no earlier round tried to fix.
   - Ask the owner only when a finding repeats a class an earlier round tried to fix, the fix needs a
     design or stored-format change, or the next round would be R9 or later.
   - When only P2 or P3 findings remain, run the checklist, merge, and record them as follow-ups.
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
5. **This plan.** The owner approved it on 2026-10-23; P1 may start.
6. **Intent revision 2** (2026-10-23): the beginner-first product vision in section 1. It is accepted with
   the AC3 and AC7 additions.
7. **Amendment 3** (2026-10-23): A9–A17 and the onboarding flow (section 15), from the owner's notes.
8. **Fix rounds:** the standing rule in section 11 applies to every phase.
9. **Parallel lanes:** independent phases run side by side, each with its own Claude session and Codex
   review, and land in dependency order.
10. **Release:** a v0.1.0 checklist ends the build (section 15). Making the repository public, or
    inviting people, stays the owner's decision.
11. **Credentials (A17):** a hard rule for every phase and PR.

**Builder's calls** (change any of them):

*Runtime and layout*
- Python stdlib CLI.
- uv as the default installer.
- The command name `oah`, with the distribution named `open-agent-harness`.
- State in the git common dir.
- Per-task folders `.oah/tasks/<task>/`.
- Wiki in `docs/wiki/`.
- Orca only as an optional adapter (row 8).

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

*Amendment 3*
- The A13 verdicts follow the verified upstream licenses (section 15). For MCP servers, "template" means
  a launch template with `${ENV_VAR}` placeholders; the server itself installs from its registry at run time.
- The secret scan runs as one Linux job, separate from the macOS and Windows test jobs.
- P3, P4 and P7 are split into lanes (section 15) so that independent parts land early.

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

## 15. Amendments 1–5 (2026-10-23)

These are fixes the owner made to their own harness on 2026-10-23, carried into this plan at the owner's
request in five batches: A1–A6, A7–A8, amendment 3 (A9–A17 and the onboarding flow), amendment 4
(A18–A19), then amendment 5 (A20–A21). Each item names the sections it changed and the phases
whose checklists carry it.

| # | Rule | Sections | Phases |
|---|---|---|---|
| A1 | Effort comes from what the model supports, with no fixed cap. `highest` means the top supported reasoning effort, which is `max` for `gpt-6.1-sol`. The model must resolve before launch; `highest` requires usable model metadata. The header verifies launch configuration; versioned normalization separately records expected inference effort. Lower effort fails the run, and unverified effort is labelled honestly. Effort is never downgraded silently. | 5.5, 11 | P2 review, P3, P6, this repo's runners |
| A2 | "No network" does not turn off `oah impact` or a consent-enabled re-ranker. A provider requires explicit enablement and a key (A9); the brief can disable it. Every result reports `used` or `unused(reason)`, including `unused(not-enabled)` or `unused(no key)`. | 4 (row 17), 5.6, 5.10 | P4, P7 |
| A3 | A task or review that touches DB tables, routines or GRANTs queries the DB backend plugin, and returns the query plus the graph identity (commit SHA, digest). Without the plugin it says so. | 5.6, 5.10 | P4, P6, P7 |
| A4 | An empty `## Scope` warns loudly (`OAH_SCOPE_EMPTY`). A selection that depends on Scope's file kinds never silently selects nothing. | 5.6 | P4, P7 |
| A5 | `oah tiers sync` replaces the managed rules block with the canonical text, not only when it is missing. | 5.11 | P7 |
| A6 | Dispatched workers carry `OAH_WORKER=1`. Generated hooks and the rules block tell them to skip any session-entry procedure. | 5.5, 5.11, 5.12 | P3, P7, session-start hook (later) |
| A7 | Intent lineage. One intent section for every stage, worker and subagent. A changed goal raises the revision, never a new ID. Conditions are outcomes. The lease binds the section in the same start command, and status says `current`. Returns carry `INTENT` and per-AC lines. One shared footer goes to every brief and every tier agent file. A draft cannot bind. | 5.6, 5.11 | P3, P4, P6, P7 |
| A8 | Wiki auto-clean, candidates only. Lint counts real problems only. Size-based log rollover. An opt-in commit check. Editor UI state is not tracked. An index lint writes outside the wiki. A hygiene delta line. | 5.1, 5.10, 5.12 | P7 |

### Phase checklists

**P2**
- [ ] Review at the highest supported effort (`max`). The runner's header configuration check and inference normalization are verified (5.5). (A1)

**P3**
P3b owns the call adapter and brief construction. P4b owns all intent wiring into `start`, `status`,
`finish` and `call`, including the call refusal and shared-footer integration below.

- [ ] Codex tier efforts resolve from `supported_reasoning_levels`. `highest` skips behaviour-changing modes. With usable metadata, a literal effort that is not listed fails with `OAH_EFFORT_UNSUPPORTED`. (A1)
- [ ] A tier model pin wins, then A14's automatic catalog mapping, then effective Codex configuration (including the selected profile); the concrete model is passed with `-m` and recorded. A fresh installation without a resolvable default refuses with `OAH_MODEL_UNRESOLVED` and tells the user to set the tier model. (A1, A14)
- [ ] `highest` with a missing cache, absent model entry or no usable ordinary reasoning levels refuses with `OAH_EFFORT_METADATA_MISSING` and actionable guidance; a literal effort with missing metadata is passed unchanged. (A1)
- [ ] Fake-engine tests cover matching, lower, other mismatched and unreadable headers: lower means `OAH_EFFORT_DOWNGRADED`, other mismatch means `OAH_EFFORT_MISMATCH`, unreadable means `unverified`. The header verifies configuration only. (A1)
- [ ] Version/model-specific normalization tests include a behavior-changing alias such as `ultra` and an unknown mapping. Run records keep requested, launch, header-reported and resolved inference effort separately, with header-check status and normalization basis; unknown inference effort stays `unverified`. (A1)
- [ ] Workers are launched with `OAH_WORKER=1`, `OAH_RUN_ID` and `OAH_TASK`, and with the worker line first in the preamble. (A6)
- [ ] `oah call` refuses to dispatch when the task's intent is not `current` (`OAH_INTENT_NOT_CURRENT`). It appends the shared intent footer to every worker brief. (A7)

**P4**
P4a owns packet, intent and preflight core. P4b owns all intent wiring into `start`, `status`, `finish`
and `call`; P6 consumes that integration after P4b lands.

- [ ] `oah packet check` warns with `OAH_SCOPE_EMPTY`, and `oah start --packet` refuses an empty Scope unless `--scope repo` is given. (A4)
- [ ] The packet template says what "no network" forbids and what it does not. (A2)
- [ ] The Return section carries `rerank=used|unused(<reason>)` and the DB-impact line. (A2, A3)
- [ ] `oah packet new` scaffolds a draft intent. `oah start --packet` binds `{source, id, revision, digest}` in the same command and refuses a draft with `OAH_INTENT_NOT_ACCEPTED`. The lease record gains an optional `intent` field, so the schema stays at version 1. (A7)
- [ ] `oah status` and `oah intent show` apply the ordered predicates in 5.6. Tests cover absent bindings and sources, accepted matching identity/digest, changed status/identity/digest, unreadable sources and missing, malformed or duplicate markers; invalid content is never `current`. (A7)
- [ ] A changed goal raises the `Revision` of the same ID. A different `Intent-ID` for the same task is refused with `OAH_INTENT_ID_CHANGED`. (A7)
- [ ] `oah packet check` warns with `OAH_INTENT_PROCESS_STEP` when a success condition reads like a process step. (A7)
- [ ] `oah finish` checks the return's `INTENT` and `AC<n>` lines structurally against the binding as in 5.6; invalid returns refuse with `OAH_INTENT_RETURN_INVALID`. (A7)

**P6**
- [ ] A review round with lower header configuration or normalized inference effort fails with `OAH_EFFORT_DOWNGRADED`; any other header mismatch fails with `OAH_EFFORT_MISMATCH`. Failed rounds do not count; unreadable headers and unknown normalization never count as effort-verified reviews. (A1)
- [ ] For DB changes, the review prompt requires the DB-impact query and graph identity. The reviewer checks that they are present. (A3)
- [ ] Reviewers run as dispatched workers. (A6)
- [ ] Review and correction briefs carry the same intent section byte for byte, with round instructions outside it, plus the shared footer. Receipts record the digest. (A7)
- [ ] `oah review` and `oah fix` refuse before dispatch with `OAH_INTENT_NOT_CURRENT` for every non-current state. Tests exercise `unbound`, `missing` and `stale`, including invalid source content, for both commands, and allow `current`. (A7)
- [ ] `oah review` structurally checks the return's `INTENT` identity/revision/digest against the binding and requires exactly one valid status/evidence line for each bound AC. Tests reject missing, malformed, duplicate or mismatched `INTENT` lines and missing, malformed, duplicate or invented AC lines with `OAH_INTENT_RETURN_INVALID`, and accept a complete return. `oah finish` uses the same checks. (A7)

**P7**
- [ ] `oah impact` always prints `rerank=...`. A provider runs only when explicitly enabled and a key exists; the brief can disable it. Tests cover `unused(not-enabled)` with an existing key and `unused(no key)` when enabled without a key. (A2, A9)
- [ ] The DB backend plugin contract is in place, and DB entities require the query plus the graph identity. (A3)
- [ ] Every selection by Scope file kinds warns on an empty Scope and never selects nothing silently. (A4)
- [ ] `oah tiers sync` replaces the managed block. A test changes the canonical wording and sees it reach an existing file. (A5)
- [ ] The rules block tells dispatched workers to skip session entry. (A6)
- [ ] The tier agent files carry the shared intent footer inside their managed block, replaced on every sync. (A5, A7)
- [ ] `oah wiki lint` counts only real problems. It resolves out-of-wiki link roots, ignores links in code spans and blocks, counts archive indexes as index sources, and judges staleness by last commit date. (A8a)
- [ ] `oah wiki rollover` implements the exact thresholds, whole-entry parsing, oversized-entry rule, archive numbering and metadata-free concatenation order in 5.10. Tests cover threshold boundaries, invalid boundaries, fenced headings, multiple archives and oversized entries, comparing preservation streams byte for byte. (A8b)
- [ ] Rollover serializes wiki access, journals and flushes staged writes, and validates sources before archive-first publication. With a pending journal, commands other than `oah wiki recover` refuse with `OAH_WIKI_RECOVERY_PENDING` and direct the user to explicit recovery as in 5.10. Tests verify that read-only lint leaves wiki files and the journal byte-identical on refusal. Failure injection at every journal/publication step and repeated explicit recovery prove no lost or duplicated entries; concurrent-edit tests prove pre-publication refusal and recovery-conflict preservation. (A8b)
- [ ] `oah hook pre-commit` is opt-in. It blocks missing archives, archive edits, raw-source edits and non-UTF-8 or surrogate bytes. New broken links are warned first, then blocked. (A8c)
- [ ] `oah init` git-ignores editor UI state. (A8d)
- [ ] `oah wiki lint --index` uses the banner patterns and configurable 120-code-point line limit in 5.10, resolves unindexed notes using normal lint rules, and writes candidates outside the wiki. Tests cover pattern matches, fenced-code exclusions and line-limit boundaries. (A8e)
- [ ] `oah wiki hygiene` prints the nine named counters in 5.10 and persists counts/configuration atomically. Tests cover first-run and changed-configuration `delta=n/a`, signed subsequent deltas, and failed scans preserving the baseline. (A8f)
- [ ] No wiki command deletes, merges or moves notes on its own; they produce candidates only. (A8)

**Later: session-start hook**
- [ ] `oah hook session-start` prints nothing when `OAH_WORKER=1`. (A6)

### Candidates from the owner's later fixes (2026-10-23)

These lessons came after amendment 2. Each is a **candidate**: it becomes a checklist item when the named
phase is planned, and is dropped only with a recorded reason.

| # | Candidate | Phases |
|---|---|---|
| C1 | Only implementation and correction briefs carry the exact lease command (`oah start --packet <source>`, with the id, revision and digest it will bind). Review briefs never carry it. A test follows the delivered command and expects `oah intent show` to report `current`. | P3, P4, P6 |
| C2 | Briefs have one fixed layout: worker line, preamble, the intent section verbatim, round instructions, the verify footer (the verify-what-changed rule, 5.11), then the intent footer. A test that compares a brief with an older baseline strips exactly one intent footer at that position, and fails when the footer is missing or duplicated. | P3, P6 |
| C3 | A dispatch that reuses a stored brief or spec constructs a delivered brief containing the canonical intent footer. If it cannot deliver that brief, it refuses dispatch with `OAH_FOOTER_NOT_APPENDED`. | P3 |
| C4 | A footer counts as present only when its opening marker is a whole line at line start. A brief that merely mentions the marker still gets the footer. Tier sync uses the same rule for its managed block. | P3, P7 |
| C5 | No current document claims a fixed effort cap, such as "max is rejected". Docs written in P9, and later edits, follow the supported-effort rule (A1). | P9 |
| C6 | Any future skill-selection text describes the actual selection (by kind, stage, risk and availability) and never promises zero optional skills. | later |

**Candidate checklist lines** (copy into each phase's checklist when that phase is planned):
- **P3:**
  - [ ] Implementation and correction briefs carry the exact lease command; review briefs do not. (C1)
  - [ ] Briefs use the fixed layout. (C2)
  - [ ] Reusing a stored brief or spec delivers a brief containing the canonical intent footer, or refuses dispatch with `OAH_FOOTER_NOT_APPENDED` if it cannot. (C3)
  - [ ] Footer presence means a marker line at line start. (C4)
- **P4:**
  - [ ] A test runs the delivered lease command and gets `current` from `oah intent show`. (C1)
- **P6:**
  - [ ] Review briefs never carry a lease command. (C1)
  - [ ] Baseline comparison strips exactly one intent footer at its fixed position and fails when it is missing or duplicated. (C2)
- **P7:**
  - [ ] Tier sync detects its managed block by a marker line at line start. (C4)
- **P9:**
  - [ ] No document claims a fixed effort cap. (C5)
- **Later:**
  - [ ] Skill-selection text describes the actual selection by kind, stage, risk and availability, and never promises zero optional skills. (C6)

### Amendment 3 (2026-10-23)

These items come from the owner's notes of 2026-10-23. Each names the sections it changes and the phases
that carry it. Section 1 (intent revision 3), section 4 (the re-judged rows), 6.5 (onboarding), section 11
(lanes and the fix-round rule) and section 13 (decisions) were updated with them.

| # | Rule | Sections | Phases |
|---|---|---|---|
| A9 | **Skill automation.** `oah` selects skills for each brief from the installed Claude and Codex skill directories (user, project and plugin skills) and the kit cache, with A15's precedence. It selects by task kind, stage, risk and the file kinds in Scope, and references or attaches each skill in the brief with a reason. An empty Scope warns (A4). A re-ranker plugin interface may re-order candidates; its first provider is TypeSafe Jev. A provider runs only when explicitly enabled AND its key is present; a key alone never enables it. Guided `oah init` asks once, states potential cost and the candidate/evidence data sent, and records the choice, or the user enables it in config. Every result records `used` or `unused(<reason>)`, including `unused(not-enabled)` and `unused(no key)`. Selection telemetry stays local. The selector is new code; none of the source's skill machinery is copied. | 4 (rows 30, 31), 5.5, 5.6, 5.10 | P3, P4, P7 |
| A10 | **Difficulty-based model and effort, applied automatically.** `oah packet new` and `oah call` score the difficulty axes (kind, intensity, reasoning, risk) by rules, and apply `oah recommend` without a manual flag. `--tier` overrides and is recorded. The re-ranker plugin may add an advisory AI judgement, recorded next to the rule result, which never lowers the tier. Run records keep the axes, the tier source, and the requested and effective model and effort. | 4 (rows 6, 31), 5.5, 5.6 | P3, P4 |
| A11 | **Impact map, generated and kept fresh.** `oah init` builds a persisted map per worktree in the state folder; failure warns and records `impact=unavailable` without failing the rest of init (7). Opt-in `post-commit` and `post-merge` hooks refresh it incrementally, and `preflight`, `packet new`, `review` and `finish` refresh it first. Freshness binds the worktree identity and effective input manifest: HEAD SHA, tracked index-state digest, digests of dirty and untracked inputs read by the map, ledger bytes including ignored ledgers, config bytes and tool version. Any change makes the map stale; a result digest is also recorded. A stale map is refreshed, or refused with `OAH_IMPACT_STALE`, never used silently. A default DB plugin extracts tables, views, routines, triggers, indexes and GRANTs from SQL migrations and links them to code references, so a project with SQL gets a DB impact graph without custom code. The re-ranker stays optional. The edit-time impact hint becomes an opt-in hook. | 4 (rows 16–18), 5.10, 5.12, 7 | P4, P5, P6, P7 |
| A12 | **Obsidian wiki, created and maintained automatically.** `oah init` creates an Obsidian-compatible `docs/wiki/`: an index, a newest-first log with its insertion marker, note templates, and a minimal `.obsidian/` configuration with workspace files git-ignored. The lead or guided defaults run `oah wiki maintain --task T` for tracked log, index and rollover work before the final commit, gates and review, including after corrections. The outputs in 5.4 are scope-exempt and recorded in events. Maintenance uses the wiki lock, bounded retry, task-idempotent insertion and explicit journal recovery (5.10). `finish` writes nothing tracked: it verifies task maintenance, refusing unless the recorded `--allow-missing-wiki-maintenance` override is given. Lint and hygiene at `finish` and pre-commit leave tracked files unchanged. graphify stays optional (`oah graph`), and nothing rebuilds the graph automatically. | 4 (row 21), 5.1, 5.4, 5.10, 5.12 | P7 |
| A13 | **Toolkit with provenance.** The kit bundles the skills, MCP server templates and CLI checks the owner uses, each with its source. It reverses row 30's "nothing vendored" only where the license allows. A skill is vendored only when its upstream license permits redistribution: unchanged, with its LICENSE, a pinned upstream commit and a `THIRD_PARTY_NOTICES.md` entry. A public item without a redistributable license is linked: install command and URL only. Owner-specific or private skills, account-bound connectors and product-specific tools are excluded. The kit ships `skills/`, `oah skills list/install/update`, `mcp/` templates with `${ENV_VAR}` placeholders only, `oah doctor` checks for the recommended CLIs with their official install commands, and a Toolkit page in English and Korean. unlazy installs only at P5's contract-tested pin and is verified by commit and scripts digest before gate commands (5.8). Automatic updates refresh the state-folder kit cache only; tracked project skill copies remain pins (A15). The inventory is below. | 2, 3, 4 (row 30), 5.2, 5.8, 9 | P7, P8, P9 |
| A14 | **Models follow updates.** `oah` ships an editable strongest-first `quality_order` per engine in config defaults. Claude defaults to `["opus", "sonnet", "haiku"]`, using CLI aliases that Claude Code resolves to its newest models; low uses the last available entry, medium the second (or first if only one), high and max the first. Codex starts from its effective configured model for all tiers, varying effort by A1. With no user quality list, a resolvable CLI-reported default replaces that automatic mapping; otherwise retain the effective configured model. A user quality list uses the same tier mapping as Claude. A newer Codex model is adopted only if the CLI model list marks it as default or the user quality list names it. CLI reports supply availability and supported reasoning levels, not an inferred quality ranking. Precedence is explicit tier model pin, automatic catalog mapping, then 5.5's effective-configuration resolver. A fresh install with no resolvable model still refuses with `OAH_MODEL_UNRESOLVED`. The model update check runs before every launch: read the catalog and check its freshness, refreshing on CLI version change, TTL expiry or `oah models --refresh`. The next launch after a successful check uses the eligible mapping, and A1 re-resolves effort for the selected model. Every resolved model or effort change is logged with old, new, when and why. A failed refresh reports the failure and keeps the last good mapping without downgrading; explicit pins always win. | 4 (row 7), 5.5 | P3 |
| A15 | **Skills update automatically and are used automatically.** Updatable vendored and linked skills record an allowlisted upstream and installed commit; explicit user pins never advance automatically. unlazy is an operational dependency: A15 never updates it, and only a change passing P5's contract and gate-invalidation tests against the candidate commit may adopt it (5.8). Automatic updates refresh only a kit cache in `oah`'s state folder, outside the working tree, serving skills the project has not vendored itself. Tracked project copies in `.claude/skills/` or `.agents/skills/` are pins: they change only through explicit `oah skills update --project`, run as its own leased task with its own scope. For the same skill ID, selection prefers the project copy, then the kit cache, then user and plugin copies; selection reads all copies without changing them. Guided setup enables automatic kit-cache updates by default and the user may disable them. `update_interval_days` defaults to 1 day: mutating `oah` commands run a due check before the next task when no lease is active in that repository, at most once per interval, so checks run at least daily by default while tasks continue. It holds the state lock from the idle check through update publication, so lease acquisition cannot race it; active leases defer the check. Optional launchd (macOS) or Task Scheduler (Windows) entries are installed and removed only with consent and invoke the same locked check. Global skill folders are updated only by an explicit `oah skills update --global` command with confirmation, never on a schedule. Before advancing an installed commit, check the license, record a diff summary with old/new commits, when and why, run the skill-load check and privacy scan, then publish atomically. Failed or suspicious updates roll back to the last good commit and are reported, never silent. A9 uses the selected installed version, so the next task after the update check uses an eligible validated update without manual steps; project and explicit user pins stay unchanged. | 4 (row 30), 5.10, 5.11, 6.5, 7 | P4, P7 |
| A16 | **Upstream sync with the owner's harness.** `docs/UPSTREAM_SYNC.md` records the last reviewed commit of the source harness, and a table of every reviewed change. Each row gives the commit, a summary and a verdict (port as a generic feature, skip as private or owner-specific, or defer). For ports it names the plan item and PR. At the start of each phase, and at least daily while work continues, the builder reads the new commits read-only, writes every port fresh, and turns port verdicts into amendments and implementation under the standing fix-round rule. The source harness is never a runtime dependency; `oah` users get updates as normal releases. | 1, 13 | every phase; P9 docs |
| A17 | **No credentials anywhere** (hard rule). No API key, token, password, cookie, OAuth secret or account identifier enters the repository, its history, releases, issues or CI logs.<br>• Keys are read only at runtime, from environment variables or the OS keychain, through one documented lookup (`oah.secrets`). A missing key leaves the feature off and reports `unused(no key)`, and `oah` never asks the user to paste a key into a project file.<br>• Templates use `${ENV_VAR}` placeholders only, and docs show placeholder names only.<br>• Tests generate fake keys at runtime, and committed fixtures contain none.<br>• gitleaks and `tools/privacy_scan.py` run in CI on every pull request and push, and over the full history before a release tag. Any finding fails the build, and no allowlist entry covers a real-looking credential.<br>• `oah init` git-ignores local secret files in user projects, and the docs say where users keep their keys.<br>• Anything copied from the owner's machine keeps names and sources only. | 2, 3, 8, 10 | every phase; CI now |
| OB | **Beginner onboarding** (6.5). A one-command installer per OS hands over to the guided `oah init`. It checks and installs tools from official sources with consent, walks through the sign-ins without handling secrets, and sets up the environment with the owner's defaults. Then a first guided task (`examples/hello-oah`) runs, with plain-language messages and docs in Korean and English. | 6.5, 7 | P7, P8, P9 |

#### Amendment 3 checklists

**P3** (lanes P3b and P3o)
- [ ] `oah call` scores the difficulty axes by rules when the packet has no tier, and applies `oah recommend` without a flag. `--tier` overrides and is recorded. (A10)
- [ ] Run records keep the axes, the tier and its source (`rules`, `packet` or `flag`), the advisory judgement (`used` with its value, or `unused(<reason>)`), and the requested and effective model and effort. An advisory judgement never lowers the tier. Tests cover no plugin, no key, a plugin error, and higher and lower advice. (A10)
- [ ] The skill selector reads the installed Claude and Codex skill directories (user, project and plugin skills) and kit cache read-only, using A15's precedence. It selects by kind, stage, risk and Scope file kinds, with a reason per skill, and warns on an empty Scope (A4). Briefs reference the selected skills by name and path, or attach their text when `[skills] mode = "attach"`. (A9)
- [ ] The re-ranker plugin interface only re-orders, and records `rerank=used` or `rerank=unused(<reason>)`. The TypeSafe Jev provider requires explicit enablement AND a key through `oah.secrets`. Tests cover an existing key without consent (`unused(not-enabled)`), enabled without a key (`unused(no key)`), a disabled brief and a consent-enabled call using a fake provider and no network. (A9)
- [ ] Skill-selection telemetry is a local JSON-lines file in the state folder and is never transmitted. Only a consent-enabled provider sends the data disclosed by A9. Selection text describes selection by kind, stage, risk and availability, and never promises zero optional skills. (A9, C6)
- [ ] `oah models [--refresh]` uses the editable per-engine quality lists and exact tier mapping in A14. Tests cover Claude aliases, Codex effective defaults, CLI-reported default adoption and user-listed models, explicit tier pins winning over catalog mappings, effective-config fallback and `OAH_MODEL_UNRESOLVED`. The model update check runs before every launch, checking catalog freshness and refreshing on CLI version change, TTL expiry or `oah models --refresh`; the next launch after a successful check uses the eligible mapping. A1 re-resolves supported effort. Changes log old, new, when and why; refresh failure warns and retains the last good mapping without downgrading. (A14)
- [ ] `oah.secrets.get(name)` is the one key lookup: environment variable first, then the OS keychain (macOS `security`, Windows Credential Manager). A missing key reports `unused(no key)`. Keys never reach logs, run records or briefs. (A17)
- [ ] An optional Orca adapter dispatches `oah call` workers through Orca when it is installed. (row 8, lane P3o)

**P4** (lane P4b)
- [ ] `oah packet new` scores the axes by rules, and records them, the recommended tier and the selected skills with reasons in `## Plan`. (A9, A10)
- [ ] `oah preflight` and `oah packet new` refresh the impact map first, or refuse with `OAH_IMPACT_STALE`. (A11)
- [ ] Briefs and packets use A15's selection precedence, so the next task after an update check uses an eligible validated cache update; project and explicit user pins stay unchanged. (A15)

**P5**
- [ ] `oah hooks install` offers opt-in `post-commit` and `post-merge` git hooks, with the same show-diff, backup and uninstall rules as the Stop hook. (A11)
- [ ] `oah hook pre-push` refuses while an unfinished lease exists on this worktree, and runs gitleaks over the commits being pushed when gitleaks is installed. (row 3, A17)
- [ ] `oah init --with-unlazy` installs P5's contract-tested commit and scripts digest. Every gate command verifies discovered environment/project/user copies against both identities before execution; mismatch tests verify refusal and the exact pinned-install command plus override-removal guidance (5.8). The pin is excluded from A15. Adopting a candidate commit requires the adapter contract and gate-invalidation tests against that commit, covering CLI arguments, status and evidence behavior. `gates check` writes the path-free summary before commit and review; `finish` verifies it without tracked writes. (A15, 5.8)

**P6**
- [ ] `oah review` refreshes the impact map first, or refuses with `OAH_IMPACT_STALE`. (A11)
- [ ] `oah land --check` verifies that the converged review receipt, the CI check runs (through `gh`, when present), the scan and the gates bind to the exact head. `--merge` lands only after the user confirms. (row 29)

**P7** (lanes P7a, P7i, P7t and P7b)
- [ ] `oah impact build/refresh` persist maps per worktree with A11's effective input manifest. Tests invalidate on HEAD, tracked index, dirty/untracked input, ignored ledger, config or tool-version changes, and isolate worktrees sharing HEAD. A stale map refreshes or refuses. The default SQL plugin produces `db_table`, `db_routine` and `db_grant` entities linked to code references. `oah init` builds the first map; failure warns with `impact=unavailable` and leaves the rest of init successful, dry-run only lists the build, and a second run refreshes a stale map or changes nothing. The edit-time impact hint is opt-in. (A11)
- [ ] `oah init` creates the Obsidian-compatible wiki: index, log with marker, templates, and a minimal `.obsidian/` with workspace files ignored. The lead or guided defaults run `wiki maintain --task T` before final commit, gates and review for log/index/rollover work. Tests cover scope-exempt output events, lock contention, idempotent retries including rollover/recovery, and finish refusing missing/stale maintenance unless the override is recorded. `finish` and pre-commit lint/hygiene write nothing tracked. Nothing rebuilds the graph automatically. (A12)
- [ ] `skills/` holds only skills whose verified license permits redistribution, each unchanged, with its LICENSE, upstream URL, pinned commit and a `THIRD_PARTY_NOTICES.md` entry. Linked skills install through `oah skills install`; unlazy installs at P5's contract-tested pin with commit and scripts-digest verification (5.8). Tracked project copies stay pinned under A15. `mcp/` templates use `${ENV_VAR}` placeholders only. (A13)
- [ ] Automatic kit-cache skill updates are on by default in guided setup and may be disabled. Only the state-folder kit cache outside the working tree is writable automatically, serving skills the project has not vendored; selection prefers the project copy, then kit cache, then user and plugin copies. Tracked `.claude/skills/` and `.agents/skills/` copies are pins and change only through explicit `oah skills update --project` in its own leased task with its own scope. Tests verify automatic checks leave tracked project folders unchanged and project updates enforce that separate lease and scope. Tests verify `update_interval_days = 1` by default, due checks before the next task at most once per interval through mutating commands while idle, and the state lock held through publication so a lease cannot start mid-update. Optional launchd/Task Scheduler entry install and removal require consent. Global updates require `skills update --global` with confirmation and never run on a schedule. Updates honor user pins, exclude unlazy, validate license/load/privacy, log a diff with old/new commits, when and why, and atomically publish or roll back and report failure. (A15)
- [ ] `oah init` is the guided setup. It checks tools through the catalog, installs only what the user confirms, and walks through the Claude and Codex sign-ins without handling secrets. It writes the rules block, tier agents, wiki, impact map, gate templates and the accepted hooks, with defaults that match the owner's workflow. (OB)
- [ ] Guided `oah init` asks once about provider enablement, disclosing potential cost and candidate/evidence data sent and recording the answer; a key alone and `--yes` tool-install consent do not enable it. Config enablement is also supported. (A9, AC6)
- [ ] `oah handoff`, and the opt-in SessionStart hook, which prints nothing when `OAH_WORKER=1`. (row 11)
- [ ] `oah init` adds `.gitignore` entries for local secret files: `.env`, `.env.*`, `*.pem`, `*.key`, `.oah/secrets*` and local MCP files. (A17)

**P8**
- [ ] One command per OS installs `oah` and hands over to the guided setup. The tool catalog in `oah.setup` lists each tool's check, its official install command per OS and its official URL, and nothing is installed without consent. (OB)
- [ ] The private README form runs from a clone and documents git as a prerequisite (`xcode-select --install` or `winget install --id Git.Git -e`). The public form uses `curl … | bash` or `irm … | iex`, downloads a pinned release archive without git, verifies SHA-256 before extraction, installs uv and `oah`, and offers git with consent. Release URL/digest placeholders remain until publication. Checks cover absent git, a rejected digest and `oah --version` in a new terminal. (OB, AC1)
- [ ] `oah doctor` checks the recommended CLIs from the same catalog and prints their official install commands. (A13)

**P9**
- [ ] The docs cover skill automation, difficulty, the impact map, the wiki, the toolkit with its sources, model and skill updates, the upgrade path (A16), and where users keep their keys (A17), in English and Korean. The Toolkit page lists every item with its source and license, and `examples/hello-oah` walks the whole loop. (A9–A17, OB)

**Release**
- [ ] All phases merged, with CI green on macOS and Windows.
- [ ] The privacy scan and gitleaks clean over the full history.
- [ ] The README status box updated from "developer preview".
- [ ] A `v0.1.0` tag with release notes.
- [ ] Making the repository public, or inviting people, prepared and left to the owner.

#### Lanes and dependencies

| Lane | Content | Needs |
|---|---|---|
| P3a | config loader, command discovery, package data | P2 |
| P3b | routing, engines, `call`, runs, briefs; A9, A10, A14, the A17 lookup | P3a, P4a |
| P3o | optional Orca adapter | P3b |
| P4a | packet, intent (with the shared footer), preflight core | P2 |
| P4b | all intent wiring into `start`, `status`, `finish` and `call`, including call refusal/footer integration; A9 and A10 at `packet new`; A11 refreshes | P3b, P4a, P5, P7i |
| P5 | gates, verified unlazy commit/scripts pin and contract, `check` helpers, `finish` guard, Stop and git hooks | P2 |
| P6 | review loop, landing check, A11 refresh; consumes P4b's current-intent binding | P3b, P4b, P5, P7i |
| P7a | wiki (A8, A12), rules block, graph | P2 |
| P7i | impact map (A11), SQL plugin | P2, then P5 for gate bindings |
| P7t | toolkit (A13), cache-only automatic skill updates and separately leased `--project` updates (A15); pinned unlazy installation | P3a, P5 for the unlazy pin and contract |
| P7b | guided `oah init` (OB), tiers, hook wiring, `handoff`, SessionStart | P3b, P4b, P5, P7a, P7i, P7t, P8, P7c, P8c |
| P7c | CI starter for the user's project (A21): `oah ci init` and its templates | P3a |
| P8 | installers, tool catalog, `doctor` tool checks | P3a |
| P8c | optional companion apps Orca and Aside (A20): setup steps, MCP registration, rules section, `doctor` entries | P8 for wiring; its own module can start earlier |
| P9 | docs, example, troubleshooting, Toolkit page | all |

Every lane registers commands through P3a's discovery and reads settings through `oah.config`. Lanes that
started before P3a landed rebase onto it before their review.

#### A13 toolkit inventory (2026-10-23)

This inventory was taken read-only on the owner's machine. Licenses were verified from the upstream LICENSE
files. Where the check was weaker (for example a local copy of upstream at a pinned commit), the lane that
vendors the item re-reads the LICENSE at the pinned commit first.

Each row is one of four verdicts:
- **vendor:** ship unchanged with LICENSE, pinned commit and notice. Automatic updates refresh only the
  state-folder kit cache for skills not vendored by the project. Tracked project copies stay pinned
  until `oah skills update --project` runs in its own scoped, leased task (A15).
- **template** (MCP servers): ship a launch template with `${ENV_VAR}` placeholders only.
- **link:** ship the official install command and URL only.
- **exclude:** not shipped.

Names that match a private term are masked. Values (env, headers, tokens) were never recorded.

##### Skills

| Kind | Name | Purpose | Upstream | Version/commit | License | Verdict |
|---|---|---|---|---|---|---|
| skill | computer-use | Drive the GUI of visible local app windows through the Orca CLI (accessibility tree, clicks, typing, screenshots). | <https://github.com/stablyai/orca/tree/a0cd1644f320f1327f7495650a816111b495f3cd/skills/computer-use> | a0cd1644f320f1327f7495650a816111b495f3cd | MIT | vendor |
| skill | orca-cli | Operate Orca worktrees, terminals, repos, embedded browser and handoffs through the orca CLI. | <https://github.com/stablyai/orca/tree/a0cd1644f320f1327f7495650a816111b495f3cd/skills/orca-cli> | a0cd1644f320f1327f7495650a816111b495f3cd | MIT | vendor |
| skill | orchestration | Coordinate supervised Orca worker agents: messages, task dispatch, DAGs, decision gates. | <https://github.com/stablyai/orca/tree/a0cd1644f320f1327f7495650a816111b495f3cd/skills/orchestration> | a0cd1644f320f1327f7495650a816111b495f3cd | MIT | vendor |
| skill | typesafe-ai | Guidance for building features from TypeSafe typed-judgment AI primitives (routing, ranking, verification). | <https://github.com/typesafe-ai/skills/tree/main/skills/typesafe-ai> | lock records no commit (folder hash 678c375b883794bf4292715a43acbba5c84681aa); upstream main HEAD at check 65a39f393687675ce170e6094757de20370365b9 | MIT | vendor |
| skill | unlazy | Completion discipline and operational gate checker; installed at P5's contract-tested pin, verified by commit and scripts digest before gate commands (5.8), excluded from A15 updates. | <https://github.com/Leonxlnx/unlazy> | 2.1.0 (package.json); lock hash 16671491f6679ad9378f52604d3bc2415b4120c7 equals upstream main HEAD at check | MIT | vendor |
| skill | vercel-react-best-practices | React and Next.js performance rules from Vercel Engineering. | <https://github.com/vercel-labs/agent-skills/tree/main/skills/react-best-practices> | skill metadata 1.0.0; lock records no commit; upstream main HEAD at check 063bee94c3f4df8453406c830b0a7df0f2860278 | NOASSERTION | link |
| skill | graphify | Turn code and documents into a persistent knowledge graph with query, path and explain tools. | <https://github.com/Graphify-Labs/graphify> | graphifyy 0.9.93 (PyPI); upstream commit not recorded | Apache-2.0 | vendor |
| skill | [private-term skill A] | excluded: matches a private term | - | - | - | exclude |
| skill | [private-term skill B] | excluded: matches a private term (account-synced copy) | - | - | - | exclude |
| skill | [owner-specific article skill] | excluded: owner-specific; matches a private term | - | - | - | exclude |
| skill | docx | Create, read and edit Word documents. | <https://github.com/anthropics/skills/tree/main/skills/docx> | account-synced copy; upstream main HEAD at check dbd4588f9e1033efb41dad4bef2f7947c8993d44 | LicenseRef-Anthropic-Proprietary | link |
| skill | pdf | Read, merge, split, fill and create PDF files. | <https://github.com/anthropics/skills/tree/main/skills/pdf> | account-synced copy; upstream main HEAD at check dbd4588f9e1033efb41dad4bef2f7947c8993d44 | LicenseRef-Anthropic-Proprietary | link |
| skill | pptx | Create, read and edit PowerPoint decks. | <https://github.com/anthropics/skills/tree/main/skills/pptx> | account-synced copy; upstream main HEAD at check dbd4588f9e1033efb41dad4bef2f7947c8993d44 | LicenseRef-Anthropic-Proprietary | link |
| skill | xlsx | Create, read and edit spreadsheets. | <https://github.com/anthropics/skills/tree/main/skills/xlsx> | account-synced copy; upstream main HEAD at check dbd4588f9e1033efb41dad4bef2f7947c8993d44 | LicenseRef-Anthropic-Proprietary | link |
| skill | skill-creator (Anthropic) | Create, edit and evaluate agent skills, including trigger-description tuning. | <https://github.com/anthropics/skills/tree/main/skills/skill-creator> | account-synced copy; upstream main HEAD at check dbd4588f9e1033efb41dad4bef2f7947c8993d44 | Apache-2.0 | vendor |
| skill | built-in-browser | Use the Claude desktop app's built-in browser pane. | - | account-synced | LicenseRef-Anthropic-Proprietary | exclude |
| skill | chrome-browser | Use the Claude in Chrome extension. | - | account-synced | LicenseRef-Anthropic-Proprietary | exclude |
| skill | computer-use (Anthropic) | Control the desktop through the Claude desktop app. | - | account-synced | LicenseRef-Anthropic-Proprietary | exclude |
| skill | docs | Claude Docs documents. | - | account-synced | NOASSERTION | exclude |
| skill | google-workspace | Edit Google Docs/Sheets/Slides through account connectors. | - | account-synced | NOASSERTION | exclude |
| skill | morning | Render a morning brief from account data. | - | account-synced | NOASSERTION | exclude |
| skill | import-memory | Import another assistant's memory export into Claude memory. | - | account-synced | NOASSERTION | exclude |
| skill | deep-research | Coordinate research subagents into a report. | - | account-synced | NOASSERTION | exclude |
| skill | Codex system skills (imagegen, openai-docs, review-agent, skill-creator, skill-installer) | Skills bundled with the Codex CLI. | - | Codex system-skills marker 3eadc798bef12c4a | Apache-2.0 | exclude |
| skill | Claude Code built-in skills (artifact-*, update-config, keybindings-help, code-review, simplify, loop, schedule, claude-api, init, security-review, run, plugin-authoring, workflow-authoring, dataviz, claude-in-chrome, fewer-permission-prompts) | Skills that ship inside Claude Code or claude.ai. | - | Claude Code 2.1.309 | LicenseRef-Anthropic-Commercial | exclude |

##### Plugins

| Kind | Name | Purpose | Upstream | Version/commit | License | Verdict |
|---|---|---|---|---|---|---|
| plugin | superpowers@claude-plugins-official | Workflow skills: brainstorming, plans, TDD, debugging, code review, worktrees, verification. | <https://github.com/obra/superpowers> | 7.0.0 @ bb92a77741419a4ab5f06e711a283343f1ada0c3 | MIT | vendor |
| plugin | mattpocock-skills | Engineering and productivity skills: TDD, diagnosis, domain modeling, code review, PR writing, grilling. | <https://github.com/mattpocock/skills> | 1.3.1 @ 49dd158d1076134a641b33efb035946536778336 | MIT | vendor |
| plugin | codex@openai-codex | Claude Code plugin that calls the Codex CLI for rescue, review and delegated work. | <https://github.com/openai/codex-plugin-cc> | 1.0.6 @ db52e28f4d9ded852ab3942cea316258ae4ef346 | Apache-2.0 | vendor |
| plugin | ponytail@ponytail | 'Lazy senior dev' mode: skills that push agents to write less code, plus audit/debt/review helpers. | <https://github.com/DietrichGebert/ponytail> | 5.3.2; marketplace revision 2ed6c52c9d7e5e56942508591085fd45dea277d3 | MIT | vendor |
| plugin | omo@sisyphuslabs (LazyCodex) | Codex harness plugin: agent roles, ultrawork/loop skills, LSP and code-graph tools, lifecycle hooks. | <https://github.com/code-yeongyu/oh-my-openagent> | 5.1.40 | LicenseRef-Sustainable-Use-1.0 | link |
| plugin | cowork-plugin-management | Create and customize Claude Cowork plugins. | <https://github.com/anthropics/knowledge-work-plugins/tree/main/cowork-plugin-management> | account-synced version 0040; upstream plugin-dir last commit e7a5e58b7d08dbadf66e4121b06c449e4faa2fb4 | Apache-2.0 | exclude |
| plugin | Codex openai-bundled plugins (browser, chrome, computer-use, unified-computer-use, codex-app-tools, code-review, visualize) | Plugins bundled with the Codex desktop app. | - | local bundled marketplace | NOASSERTION | exclude |
| plugin | Codex openai-primary-runtime plugins (documents, presentations, spreadsheets, pdf, template-creator) | Office-file runtimes bundled with the Codex app. | - | 26.1017.11052 (runtime cache) | NOASSERTION | exclude |
| plugin | Codex openai-curated connectors (github, notion) | Account-bound app connectors. | - | - | NOASSERTION | exclude |

##### MCP servers

| Kind | Name | Purpose | Upstream | Version/commit | License | Verdict |
|---|---|---|---|---|---|---|
| mcp | context7 | Fetch current library and framework documentation. | <https://github.com/upstash/context7> | launched as @upstash/context7-mcp@latest (npx cache holds 4.4.4 and 2.2.5); upstream master HEAD at check d980242a5b04be30ae8804124a8be259bcc2a227 | MIT | template |
| mcp | korean-law | Search and read Korean statutes and court decisions. | <https://github.com/chrisryugj/korean-law-mcp> | launched as korean-law-mcp@latest (npx cache 4.15.9); upstream main HEAD at check 0a8f08ef4f697e08047f76bfab8be435f5bdda5a | MIT | template |
| mcp | openai (mcp-openai) | Let an agent query OpenAI chat models as a tool. | <https://github.com/mzxrai/mcp-openai> | launched as @mzxrai/mcp-openai@latest (npx cache 0.1.1) | MIT | template |
| mcp | aside | Browser agent for web search, page reading and site tasks. | <https://aside.com/> | CLI 1.26.1010.1739 | LicenseRef-Proprietary | link |
| mcp | openaiDeveloperDocs | Hosted OpenAI developer-docs MCP endpoint. | <https://developers.openai.com/mcp> | hosted service | NOASSERTION | link |
| mcp | node_repl | Node REPL tool shipped inside the ChatGPT desktop app. | - | - | NOASSERTION | exclude |
| mcp | computer-use (Codex) | Codex Computer Use desktop-control client. | - | - | NOASSERTION | exclude |
| mcp | claude-in-chrome | Claude in Chrome browser-extension tools. | - | - | LicenseRef-Anthropic-Commercial | exclude |
| mcp | account-bound connectors | claude.ai connectors in these categories: mail, calendar, drive, office suite, docs, media generation. | - | - | - | exclude |

##### CLIs

| Kind | Name | Purpose | Upstream | Version/commit | License | Verdict |
|---|---|---|---|---|---|---|
| cli | git | Version control. | <https://github.com/git/git> | 2.50.1 (Apple Git-155) | GPL-2.0-only | link |
| cli | gh | GitHub CLI for PRs, issues and API calls. | <https://github.com/cli/cli> | 2.91.0 | MIT | link |
| cli | node | JavaScript runtime for MCP servers and hooks. | <https://github.com/nodejs/node> | v24.6.0 | MIT | link |
| cli | npm | Node package manager. | <https://github.com/npm/cli> | 11.5.1 | Artistic-2.0 | link |
| cli | npx | Run npm packages without installing (MCP launchers). | <https://github.com/npm/cli> | 11.5.1 | Artistic-2.0 | link |
| cli | pnpm | Fast Node package manager. | <https://github.com/pnpm/pnpm> | 10.32.1 | MIT | link |
| cli | uv | Python package and tool manager. | <https://github.com/astral-sh/uv> | 0.12.17 | MIT OR Apache-2.0 | link |
| cli | python3 | Python runtime. | <https://github.com/python/cpython> | 3.14.4 | PSF-2.0 | link |
| cli | claude | Claude Code CLI. | <https://github.com/anthropics/claude-code> | 2.1.309 | LicenseRef-Anthropic-Commercial | link |
| cli | codex | OpenAI Codex CLI. | <https://github.com/openai/codex> | codex-cli 0.163.0 | Apache-2.0 | link |
| cli | gitleaks | Secret scanner for git history and staged changes. | <https://github.com/gitleaks/gitleaks> | 8.30.1 | MIT | link |
| cli | graphify | Knowledge-graph CLI behind the graphify skill. | <https://github.com/Graphify-Labs/graphify> | 0.9.93 (PyPI graphifyy) | Apache-2.0 | link |
| cli | aside | Aside browser-agent CLI. | <https://aside.com/> | 1.26.1010.1739 | LicenseRef-Proprietary | link |
| cli | orca | Orca CLI for worktrees, terminals and agent orchestration. | <https://github.com/stablyai/orca> | 1.4.237 | MIT | link |
| cli | jq | JSON processor. | <https://github.com/jqlang/jq> | 1.8.1 | MIT | link |
| cli | rg | ripgrep fast search. | <https://github.com/BurntSushi/ripgrep> | 15.2.0 | Unlicense OR MIT | link |

unlazy's inventory commit is a source observation, not permission to install an untested checker. P5
contract-tests and records the operational commit and scripts digest; init installs that pin, discovery
refuses mismatches with the repair command in 5.8, and a new commit requires a P5 contract change.

Open questions from the inventory. None of them blocks: every affected item is linked, not vendored.
- `vercel-react-best-practices`: its README says MIT, but the upstream repository has no LICENSE file, so it
  is link only until upstream adds one.
- The LazyCodex plugin: its local metadata says MIT, but upstream's LICENSE is the Sustainable Use License
  1.0, so it is link only.
- graphify ships both an Apache-2.0 and an MIT license file; either permits vendoring its skill. The lane
  pins the upstream commit of the release it vendors.
- `typesafe-ai`: its local lock records no commit, so the lane pins the upstream commit it vendors.
- Aside is closed source: it is linked as optional, and the engines' built-in web tools remain the default
  (row 34).

### Amendment 4 (2026-10-23)

Two changes the owner made to their harness after amendment 3, ported under A16 (the `82492ea30` rows of
`docs/UPSTREAM_SYNC.md`). Sections 5.6 and 5.10 were updated with them.
Consistency fix: `oah impact` uses exit 2 for usage errors, matching the shared CLI and argparse convention.

| # | Rule | Sections | Phases |
|---|---|---|---|
| A18 | **Safer log rollover.** `oah wiki rollover` is a dry run that prints its plan. `--apply` writes and needs a lease whose scope covers the log and the new archive. `oah wiki maintain` applies rollover under the wiki lock without a lease. Before the journal is removed, both files must hold exactly the published bytes. Otherwise wiki files stay unchanged and live bytes are recorded in the journal with reason `changed-after-publication`; commands refuse with `OAH_WIKI_RECOVERY_PENDING`, and recovery refuses with `OAH_WIKI_RECOVERY_CONFLICT`, naming the files that differ and the journal. For every recovery conflict, the user reconciles files by hand, then runs `oah wiki recover --resolved`: it checks that the log and all archives parse without duplicate entries, then lists every entry from the journal's original snapshot or captured live bytes absent from the reconciled files, comparing exact entry bytes. Missing entries never block resolution. It moves the journal byte-for-byte to a timestamped resolved record in the state folder's `resolved-records/` folder and records the resolution time and record path in events; `--resolved` never deletes journal bytes. A failed check refuses with `OAH_WIKI_RECOVERY_CONFLICT` and changes nothing. Every `OAH_WIKI_LOG_INVALID` refusal names its case and happens before any write. Only the trigger and target are `[wiki]` config values; archive names stay `log_archive_NN.md` for scope exemptions, lint and recovery. | 5.10 | P7 (lane P7a) |
| A19 | **The bound intent cannot be swapped.** Later rounds reuse the intent source bound at the first dispatch, and a brief with a different source refuses with `OAH_INTENT_SOURCE_MISMATCH`. Worktree selectors resolve to exactly one repository before the intent check. An ambiguous or unresolvable selector refuses before launch with `OAH_WORKTREE_UNRESOLVED` and a runnable retry command. | 5.6 | P3 (lane P3b) |

#### Amendment 4 checklists

**P3** (lane P3b)
- [ ] A later round reuses the bound intent source, and a brief with a different source refuses with `OAH_INTENT_SOURCE_MISMATCH`. (A19)
- [ ] An ambiguous or unresolvable worktree selector refuses before launch with `OAH_WORKTREE_UNRESOLVED` and a runnable retry command. (A19)

**P7** (lane P7a)
- [ ] `oah wiki rollover` writes nothing without `--apply`, and `--apply` without a covering lease refuses with `OAH_WIKI_LEASE_REQUIRED`. (A18)
- [ ] The post-publication check removes the journal only when both files hold exactly the published bytes. (A18)
- [ ] Any post-publication difference, including a late append, leaves wiki files unchanged, records live bytes in the journal with reason `changed-after-publication` and makes commands refuse with `OAH_WIKI_RECOVERY_PENDING`. (A18)
- [ ] Recovery refuses a `changed-after-publication` journal or any other recovery conflict with `OAH_WIKI_RECOVERY_CONFLICT`, names the files that differ and the journal, and preserves all files and the journal. (A18)
- [ ] After manual reconciliation of either conflict path, `oah wiki recover --resolved` moves the journal byte-for-byte to a timestamped resolved record under the state folder's `resolved-records/` when the log and all archives parse and no entry appears twice; it never deletes journal bytes. (A18)
- [ ] Successful `oah wiki recover --resolved` records the resolution time and resolved-record path in events. (A18)
- [ ] Dropping an original entry or a captured late append during reconciliation lists each missing entry by exact entry-byte comparison, does not block `--resolved`, and leaves each dropped entry recoverable from the resolved record. (A18)
- [ ] If parsing fails or an entry appears twice, `oah wiki recover --resolved` refuses with `OAH_WIKI_RECOVERY_CONFLICT` and changes nothing. (A18)
- [ ] Each invalid-log refusal names its case before any write. (A18)
- [ ] Only the trigger and target come from `[wiki]`; archive names stay `log_archive_NN.md` for scope exemptions, lint and recovery. (A18)

### Amendment 5 (2026-10-23)

Two requests from the owner after amendment 4. Beginners often miss CI and safe defaults when they push, and
the owner's own workflow relies on two companion apps. Section 6.5 and the lane table were updated with them.

| # | Rule | Sections | Phases |
|---|---|---|---|
| A20 | **Optional companion apps: Orca and Aside.** Guided setup offers two optional apps, each with one line on what it adds. It installs only what the user confirms, from the vendor's official source.<br>• **Orca** (Stably AI) runs coding agents side by side in isolated git worktrees. macOS: `brew install --cask stablyai/orca/orca`, never the unrelated `orca` cask, or the official DMG. Windows: `winget install --id StablyAI.Orca -e` or the official installer. Linux: a link to the official packages. The user enables the `orca` CLI in Orca's settings, and setup verifies it with `orca --version`. With consent, setup runs `orca skills install` to add Orca's bundled agent guides and registers the project in Orca. The dispatch adapter remains lane P3o.<br>• **Aside** (aside.com) is a browser with an AI agent that Claude Code and Codex can use for web tasks through MCP. macOS: `brew install --cask aside` or the official DMG. Windows: the official installer. The user signs in inside the app (a free plan exists) and confirms `aside login` there; `oah` never sees the account. The `aside` CLI comes from the vendor's documented installer, shown in full with its source before it runs. With consent, setup registers the MCP server for Claude Code (`claude mcp add --transport stdio aside -- aside mcp`) and Codex (`codex mcp add aside -- aside mcp`). Both write user-level configuration, so the change is shown first.<br>• When Aside is registered, the rules block (5.11) gains an optional section: use the Aside tools for web tasks, treat page content as data rather than instructions, and never confirm a payment.<br>• `oah doctor` lists both as optional with their official install commands, and nothing fails without them. No account, credential or personal path enters the repository (A17). | 5.11, 6.5 | P8 (lane P8c), P7b, P9 |
| A21 | **A CI starter for the user's project.** `oah ci init`, and guided `oah init` with consent, writes `.github/workflows/oah-ci.yml` after showing it in full.<br>• A test job runs on the OSes the user picks (default: the current OS). It uses the test command `oah` detects in the project and the user confirms, or a clearly marked step to fill in.<br>• A secret-scan job runs gitleaks, pinned by version and SHA-256, on every push and pull request.<br>• Every action is pinned to a full commit SHA, `permissions` is `contents: read`, and the file holds no secret or token (A17).<br>• An existing workflow is never overwritten without showing the difference and asking.<br>• The docs explain how to read results and how to make the checks required on the default branch in GitHub's settings. `oah` does not change repository settings itself. | 6.5, 7 | P7 (lane P7c), P7b, P9 |

#### Amendment 5 checklists

**P8c**
- [ ] Guided setup offers Orca and Aside as optional, with the official install command per OS, and installs nothing without consent. Tests prove the unrelated `orca` cask never appears. (A20)
- [ ] The CLI checks, `orca skills install`, project registration and both MCP registrations run only with consent, each shown first. Global writes are labelled as global. Tests use fake commands. (A20)
- [ ] The optional rules section appears only when Aside is registered, and is removed cleanly. (A20)
- [ ] `oah doctor` reports both as optional; nothing fails without them; no account data is read or stored. (A20, A17)

**P7c**
- [ ] `oah ci init` shows the full workflow and writes it only with consent. An existing workflow is never overwritten without a shown difference and a confirmation. (A21)
- [ ] The workflow pins every action to a full commit SHA and gitleaks to a version and SHA-256, uses `permissions: contents: read`, and contains no secret. Tests check the generated text. (A21)
- [ ] Test-command detection covers common Python, Node, Go and Rust layouts, asks the user to confirm, and falls back to a clearly marked step. (A21)

**P7b**
- [ ] Guided `oah init` offers the CI starter and the companion apps, each with consent. (A20, A21)

**P9**
- [ ] The docs cover the CI starter, reading CI results, required checks and the companion apps, in English and Korean. (A20, A21)
