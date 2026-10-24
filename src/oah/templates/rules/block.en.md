## open-agent-harness rules

Claude Code and Codex CLI both follow these rules in this repository.

**Dispatched workers:** when `OAH_WORKER=1` is set, `oah` launched you for one task. Skip any session-entry procedure (status tours, handoff reading, greetings) and do only the task in your prompt. Initial and correction workers use the dispatcher's leased worktree; reviews stay read-only and may use another tree.

1. **Stage order.** Claude Code does the initial implementation. Codex CLI then reviews, corrects and re-reviews in bounded rounds (`oah review`, `oah fix`). The initial implementer returns review findings instead of fixing them.
2. **Leases.** Dispatched workers use the dispatcher's lease and never start, renew or finish a lease; stay inside its scope and return changes and evidence to the dispatcher. For the dispatcher: before editing, run `oah start --task <task> --agent <you> --scope <path>` (repeat `--scope`); widen scope with `oah renew --scope …` and close with `oah finish --summary "…"`. If another lease overlaps, stop and report.
3. **Packets and intent.** `.oah/tasks/<task>/PACKET.md` holds the task's intent, scope, plan and acceptance. Never edit the intent to fit your work; a changed goal raises its Revision. Return the `INTENT` line and one `AC<n>: met|unmet|deferred` line per success condition.
4. **Preflight.** Run `oah preflight` before you start. Stop on ABORT.
5. **Gates prove "done".** Every acceptance condition is a gate in `GATES.md` with CHECK, EXPECT and EVIDENCE. Run the check and record its real output. A ticked box without evidence is worse than an open one. Every number in a report needs its own check.
6. **Verify what changed.** Between review rounds, re-run only the gates bound to the changed files (`oah impact --changed-since <rev>`) plus quick structural checks. Run heavy gates once, on the final head.
7. **Honest labels.** Report failures, skipped steps and deferred gates exactly as they are. A review by the engine that wrote the code is not independent; say so.
8. **Workers never push.** Do not push, merge, force-push, rewrite shared history or delete branches. The lead pushes after `oah finish`.
9. **No keys.** Never put an API key, token, password, cookie or account identifier in any file, commit message, test fixture or log. Keys are read at runtime from environment variables or the OS keychain. Docs and templates use `${ENV_VAR}` placeholders.
10. **Keep the wiki current.** Record decisions and lasting findings in `docs/wiki/` with `oah wiki new` and `oah wiki log`, each with a source. `oah wiki lint` lists problems; nobody deletes, merges or moves notes on its say-so.
11. **When blocked** on a decision only the owner can make, stop and ask. Do not guess.
