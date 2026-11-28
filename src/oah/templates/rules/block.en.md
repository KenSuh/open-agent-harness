## open-agent-harness rules

**Workers (`OAH_WORKER=1`).** Skip session-entry. Do only the dispatched task. Initial and correction workers use the dispatcher's leased worktree. Reviews are read-only and may use another tree.

**Lead drafts.** Run `oah recommend --kind design --task <task> --request-file <request>` before drafting. Delegate to its `oah-<tier>`. Use `--kind packet` for packets.

**Guided design (Claude Code lead, Python CLI).** To start or resume, read the project's `grilling` skill. Use `oah design show PROJECT DESIGN` and `oah design coverage PROJECT DESIGN --next`. Ask one open question with a recommendation and wait. On request, record answers and open items via `oah design save` with the current revision and digest. Preview `coverage edit` before saving.

- `deferred`: `reason`, `impact`, `resolve_by`, empty `answer_refs`.
- Optional stage: `set-applicability`, `value` = `not-applicable`, `reason`.

Pass `oah design coverage PROJECT DESIGN --revision N --check`. Use `oah design accept` after separate user approval of N and DIGEST. On an implementation request, run `oah design check PROJECT DESIGN --revision N --digest DIGEST --accepted`, then `oah packet new TASK --design PROJECT/DESIGN --design-revision N`. Verify the packet links the same accepted N and DIGEST. If approval moved, pause for the user's choice of checked revision.

1. Claude Code does the initial implementation. Codex CLI then reviews, corrects and re-reviews in bounded rounds (`oah review`, `oah fix`). The implementer returns review findings to the lead.
2. Workers use the dispatcher's lease and never start, renew or finish it. Edit in scope and return changes and evidence. Before editing, dispatchers use `oah start --task <task> --agent <you> --scope <path>` (repeat `--scope`), `oah renew --scope …` to widen and `oah finish --summary "…"` to close. Stop and report overlaps.
3. Keep approved intent. `.oah/tasks/<task>/PACKET.md` holds intent, scope, plan and acceptance. Goal changes need a new intent Revision. Return `INTENT` and one `AC<n>: met|unmet|deferred` per condition.
4. Run `oah preflight` before starting. Stop on ABORT.
5. Each acceptance condition needs a `GATES.md` CHECK, EXPECT and observed EVIDENCE. Run checks. Verify reported numbers.
6. Verify what changed. Between rounds, run bound gates (`oah impact --changed-since <rev>`) and quick structural checks. Run heavy gates once on the final head.
7. Report failures, skips and deferrals. Same-engine reviews are not independent.
8. Workers never push, merge, force-push, rewrite shared history or delete branches. The lead pushes after `oah finish`.
9. No keys, tokens, passwords, cookies or account IDs in files, commits, fixtures or logs. Read secrets from the environment or OS keychain. Use `${ENV_VAR}` in docs and templates.
10. Record decisions and findings with sources in `docs/wiki/` via `oah wiki new` and `oah wiki log`. `oah wiki lint` reports only. Stop and ask for decisions only the owner can make.
