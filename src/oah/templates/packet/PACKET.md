<!-- OAH_INTENT:BEGIN -->
## Intent
Intent-ID: {{TASK}}
Revision: 1
Status: draft            # becomes "accepted" when you approve it
### Problem
<what is wrong or missing today>
### Desired outcome
<what is true when this task is done>
### Success conditions
<!-- Outcomes only, one bullet each. Leases, commits, review rounds and the return format are
process steps: they belong outside this section. -->
- AC1: <checkable condition>
### Constraints
<!-- "No network" forbids installs, downloads, fetches and outside web access.
It does not turn off `oah impact` or a configured re-ranker; name them here to turn them off. -->
### Non-goals
### Open questions
<!-- OAH_INTENT:END -->
## Scope        (paths this task may write)
<!-- One bullet per path, such as "- src/app.py". Use "- repo" for the whole repository.
An empty Scope is reported (OAH_SCOPE_EMPTY). -->
## Plan         (stage → engine → tier)
- initial implementation → claude
- review → codex
- correction → codex
## Acceptance   (GATES.md in this folder)
<!-- One gate per success condition in GATES.md next to this file: "- [ ] G1: <result>" with
CHECK:, EXPECT: and EVIDENCE: lines below it. -->
## Return       (INTENT <id>@r<n> <digest> + one "AC<n>: met|unmet|deferred — evidence" line each;
                rerank=used|unused(<reason>) when impact was used; the DB-impact line for DB changes)
End every return with these plain lines (no bullets, no code formatting):

    INTENT {{TASK}}@r<revision> <digest>
    AC1: met|unmet|deferred — <evidence>

- `oah intent show {{TASK}}` prints the exact INTENT line.
- Write one AC line per success condition, and none for conditions that are not listed.
- When `oah impact` was used, add `rerank=used` or `rerank=unused(<reason>)`.
- When the task touches database tables, routines or GRANTs, add
  `db-impact: <query> graph=<commit sha> digest=<digest>`, or `db-impact: unavailable(<reason>)`
  when no database plugin is configured.
