# Upstream sync with the owner's harness

`oah` follows the owner's private harness, called "the source harness" here, as it keeps improving (A16 in
`docs/PLAN.md`).
- **Read-only.** The source harness is read, never copied. Every port is written fresh as a generic feature.
- **Nothing private.** No names, paths, products, accounts or rule text enter this repository.
- **No runtime dependency.** The source harness is never a runtime dependency. `oah` users get its
  improvements as normal `oah` releases.

**Last reviewed commit:** `82492ea30` (2026-10-23). It covers the source harness's harness tree and shared
rules.

## How this table is kept

At the start of each phase, and at least once a day while work continues, the builder:
1. reads the source harness's new commits since the last reviewed one, read-only;
2. adds one row per change, using one of the verdicts below;
3. turns each port verdict into a plan amendment and its implementation, under the standing fix-round rule.

Port-candidate notes from the owner go into the same table.

**Verdicts:**
- **port:** a generic feature. The `oah` item and PR carry it.
- **skip:** private or owner-specific.
- **defer:** later, with a reason.

| Source change | Summary (generic) | Verdict | `oah` item | PR |
|---|---|---|---|---|
| baseline, before 2026-10-23 | Lease core, stage routing, packets and intent, preflight, gates, the review loop, the wiki and the impact map. The Phase 0 plan classified them row by row | port, as section 4 records | PLAN section 4 | #1 |
| owner's fixes, 2026-10-23 | Effort follows what the model supports; re-ranker used/unused reporting; DB-impact queries; empty-scope warnings; tier sync replaces its block; dispatched workers are marked | port | A1–A6 | #3 |
| owner's fixes, 2026-10-23 | Intent lineage across every stage; wiki auto-clean as candidates only | port | A7, A8 | #5 |
| `ac4a20339` | Verify that a skill-maintenance run's results really materialized | port, as the skill-load check after an update; init installs P5's contract-tested unlazy pin, discovery verifies commit and scripts digest before gate commands and refuses mismatches with an exact repair command; adopting a new pin requires candidate contract and gate-invalidation tests | A15, PLAN 5.8 | #9 |
| `07df09060`, `4c5e28612` | Launch the highest Codex effort through the worker launcher, and drop the stale separate-session rule | port, as the no-fixed-cap effort rule | A1, C5 | #3, #6 |
| `da793b672` | Keep the re-ranker on under "no network" briefs, and map DB touchpoints | port; a provider still requires explicit enablement after cost/data disclosure AND an available key | A2, A3, A9 | #3 |
| `b54d9fa5a` | Finish the highest-effort rule sweep; describe skill selection accurately | port | C5, C6 | #6 |
| `a80ffe4f5` | Carry the original intent into every stage, for workers and subagents | port | C1–C4 | #6 |
| `4dc132679`, `7a7d99d7c` | Load and harden the guard plugin of an experimental third engine | defer: an optional third-engine adapter after v0.1.0 | PLAN section 4, row 32 | — |
| owner's notes, 2026-10-23 | Skill automation, automatic difficulty, a fresh impact map, an Obsidian wiki, the toolkit, models and skills that follow updates, this sync, no credentials, beginner onboarding | port; per-worktree input-bound maps, wiki maintenance before commit/gates/review with verification-only finish, explicit model-pin precedence and a model check before every launch; validated cache-only skill updates checked daily by default, project copies pinned until explicit `oah skills update --project` in its own scoped, leased task, global updates only with `--global` and confirmation, and project/cache/user/plugin selection precedence; accepted intent Revision 3 qualifies AC5 by the update check; private clone and public pinned-release bootstrap forms | A9–A17, OB | #9 |
| `acf62770b`, `0b8b8230f`, `30331777e`, `08752b6af` | Byte-preserving wiki log rollover: refusals before any write, writer checks with rollback and a kept recovery copy, a dry run by default and a lease for `--apply` | port onto the existing 5.10 rollover with fixed `log_archive_NN.md` names for scope exemptions, lint and recovery. Before journal removal, both files must hold exactly the published bytes; otherwise wiki files stay unchanged and live bytes are recorded in the state-folder journal with reason `changed-after-publication`. Commands refuse with `OAH_WIKI_RECOVERY_PENDING`; recovery refuses with `OAH_WIKI_RECOVERY_CONFLICT` and names the files that differ and the journal. Every recovery conflict requires manual reconciliation followed by `oah wiki recover --resolved`, which validates parsing and absence of duplicate entries, then lists every entry from the journal's original snapshot or captured live bytes absent from the reconciled files by exact entry-byte comparison. Missing entries never block resolution. It moves the journal byte-for-byte to a timestamped resolved record in the state folder's `resolved-records/` folder and records the resolution time and record path in events; `--resolved` never deletes journal bytes; failed checks refuse with `OAH_WIKI_RECOVERY_CONFLICT` and change nothing, so nothing new is git-ignored | A18 (lane P7a) | pending |
| `abe7cd4c3`, `44b7abdee`, `6d5fe9941`, `2c08b583d` | The intent section reaches every brief path intact; later rounds keep the bound source; worktree selectors resolve before the intent check | port, engine-neutral. The byte-for-byte section and the shared footer were already in A7 and C1–C4 | A19 (lane P3b) | pending |
| owner's notes, 2026-10-23 (later) | Optional setup of the two companion apps the owner's workflow uses (a worktree agent IDE and a web agent through MCP); a CI starter for beginners' projects; reviews without a fixed round ceiling | port; official sources only, consent for every install and global write, no account data; the source's code still holds its old ceiling, and the owner's current practice wins; stable finding/class IDs, validated prior-round references and persisted correction-attempt outcomes supply recurrence history to every reviewer; missing or ambiguous evidence stops with `OAH_REVIEW_NEEDS_DECISION` | A20, A21, A22 | pending |
| `ce49211a1`, `4c284812e`, `f5c599e89`, `f8d10f4d4`, and a later rule correction | Impact analysis refreshes a copy of the latest default branch before each query; the refresh stays on, briefs exempt it from "no network", and review evidence stays pinned to the reviewed commit | port as a second, default-branch map next to the per-worktree map; offline results are labelled stale; landing fetches and reassesses impact against the fetched SHA immediately before push or merge and requires newly affected checks; stale landing impact refuses with `OAH_LAND_IMPACT_STALE`, and pending or failed newly affected checks refuse with `OAH_LAND_CHECKS_REQUIRED` | A23 (lane P7j) | pending |
