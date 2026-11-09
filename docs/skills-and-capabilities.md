# Skills and capabilities

A design says what to build. Before a worker starts, `oah` does three more things with it: it tracks the nine
design stages, it finds and reviews specialist skills for each stage, and it hands the reviewed skill texts and
the tools they name to the worker. Everything on this page runs locally and gives the same result for the same
inputs, except the one search step that says it sends something.

[한국어](ko/skills-and-capabilities.md)

## The nine design stages

A design keeps a `coverage` part beside its other parts. For each stage it records whether the stage applies,
the state of each check, the skills linked to it and the gaps left open.

| Stage | Required checks | Can be marked not applicable |
|---|---|---|
| `purpose` | users, outcome, first-scope, terms, open-decisions | no |
| `behavior` | normal-flow, failure-flow, cancel-flow, resume-flow, user-result | no |
| `data` | entities, ownership, lifetime, integrity | no |
| `access` | subjects, rules, cross-owner-counterexample, enforcement-point | yes |
| `server` | responsibilities, api-contract, secrets, external-effects | yes |
| `payment` | state-transitions, failure-refund-cancel, duplicate-out-of-order-missing, server-confirmation, reconciliation-owner | yes |
| `quality` | platforms, cost-performance, observability, deploy-rollback, recovery-scenario, and accessibility for a web, mobile or desktop interface | no |
| `materials` | stack-options, sources-licenses, adoption, unverified, first-scope-done, module-boundaries | no |
| `confirm` | intent-alignment, first-implementation-scope, completion-criteria, packet-ready | no |

- `oah design coverage P D` shows each stage's status, what needs a re-check and the stages to ask about next.
  It reads only. `--accepted` or `--revision N` reads another revision. `--check` exits 1 with
  `OAH_COVERAGE_INCOMPLETE` until the design is complete.
- `oah design coverage edit P D --from EDITS.json` builds a new coverage part from a list of operations and
  prints it together with the exact `oah design save` command that stores it. It writes nothing itself. The
  operations are `set-applicability`, `set-check`, `defer-stage`, `confirm-stage`, `set-tech`, `link-skill`,
  `unlink-skill`, `add-gap` and `clear-recheck`. The file names the revision and digest it was written against,
  so an edit built on an older revision is refused.
- When a decision changes, only the stages it reaches need a re-check. No flag is stored for this: the status is
  worked out again on every read.

**One source for the coverage part.** The readable section of the part is generated from its JSON block, and a
hand edit to that section is refused with `OAH_COVERAGE_EDITED`. What it costs you: you change coverage with
`coverage edit` and `oah design save`, not in a text editor.

**A design is complete only when all nine stages are.** A stage that applies needs every required check answered
and then your confirmation (`confirm-stage`). A proposal or a deferral counts only when its impact is minor. A
stage that does not apply is marked so, with a reason. One answered check never completes the design. What it
costs you: more questions, and one confirmation per stage, before `--check` passes.

## Finding and reviewing skills

A need is a stage check that a specialist skill could help with. `oah skills find --design P/D` looks for skills
for a design's needs. `--design-stage`, `--check` and `--tech LABEL=VALUE` narrow it.

- Providers are `seed` (a small built-in catalog), `installed` (skills already on this machine) and `skills-sh`
  (the public skills.sh search). Only `skills-sh` sends anything, and it shows a notice the first time.
- `oah skills review` pins one skill folder to a commit and records its license, front matter, supported hosts
  and a screening of instruction-like lines. It installs nothing. Choose the folder with `--hit QUERY_ID:N` (a
  result of a stored search), `--seed ID`, or `--github OWNER/REPO --path PATH`. `--update REVIEW_ID` checks a
  reviewed source again.
- `oah skills candidates --design P/D` lists stored reviews and search hits against the current decisions, and
  `oah skills read --review ID` prints a reviewed text framed as data. Neither sends anything.
- Dispatched workers cannot search, review or read reviews (`OAH_SKILLS_WORKER_REFUSED`). Only the lead does.
- A search needs no task. With `--design`, the report's `operation` names the design revision and digest it read,
  even an unaccepted draft. Without it, the operation names no design. Neither can deliver anything to a worker:
  delivery comes only from a task's own dispatch.

`[skills.discovery]` in `.oah/config.toml` holds these keys.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` stops every search request. |
| `provider` | `"skills-sh"` | The search provider. |
| `cache_hours` | `24` | How long a successful search is reused. |
| `max_queries` | `4` | Search requests per `find`; the rest are deferred. |
| `license_allow` | `[]` | Licenses a review accepts. Empty means the `[sources]` list, which defaults to MIT. |

**Only public words leave the machine.** A search is built from oah's fixed stage, check and technology names.
An undecided technology, `none`, or a local value such as `other:…` sends nothing. You may add your own words with
`--public-query` only together with `--public-source`, the public https page that uses them, and `--need`. What
it costs you: a technology oah has no public name for is not searched until you give a public query.

**Outside text is data.** A skill's text never instructs `oah`, and a review only records what it found. What it
costs you: a skill you find does nothing until you review it and link it with `coverage edit`.

## Delivering skill texts to a worker

When `oah call` prepares a run, it admits the skills before anything is spent: before the budget check, the
judges, the round and the reservation. The links in the task's accepted coverage that name this dispatch stage
(`initial`, `correction` or `review`) become the run's skill bundle.

- Each item is the reviewed text or a matching installed copy, checked against the review's pin by SHA-256 with
  line endings normalized. A required auxiliary file is delivered with its skill.
- The brief carries one marked block, `<!-- OAH_SKILL_TEXTS:BEGIN -->` to `<!-- OAH_SKILL_TEXTS:END -->`, after
  the design block. `skills.json` in the run folder records the bundle.
- Just before the engine starts, oah hashes the installed copies again. A copy that changed in between refuses
  with `OAH_SKILL_NOT_DELIVERABLE(changed)`.
- `oah skills plan --task T --stage S [--engine claude|codex]` runs the same admission and prints what the next
  dispatch would carry, or why it would refuse. It writes nothing and sends nothing.
- `oah skills read --run RUN --item N` prints one bundle item and records the read. A worker may read only its own
  run's bundle.
- A task whose coverage links no skill gets exactly the brief it got before.

**One admission, before anything is spent.** A required skill that cannot be delivered refuses the call with a
typed code: `OAH_SKILL_MANDATORY_MISSING`, `OAH_SKILL_REFERENCE_UNAVAILABLE` or `OAH_SKILL_NOT_DELIVERABLE`
with its reason (`user-only`, `license`, `host`, `marker` or `pin-mismatch`). There is no retry and no fallback,
and no run folder is created. An optional skill that cannot be delivered is skipped and listed. What it costs
you: a missing review or a moved pin blocks the dispatch until you review again or unlink the skill.

**Fixed byte budgets.** Skill texts may take up to 196,608 bytes and the whole brief up to 524,288 bytes, with at
most 128 items. Over a budget, the call refuses with `OAH_SKILLS_BUDGET_EXCEEDED` and lists each skill's size, so
you can split the task or unlink optional skills. What it costs you: a large skill set has to be split across
tasks.

**Skills meant for explicit use stay with you.** A skill that only a person may invoke is never delivered. A
required one refuses with `OAH_SKILL_NOT_DELIVERABLE(user-only)`. What it costs you: you run such a skill
yourself.

## Capabilities a skill names

A delivered skill can name tools in its `allowed-tools` line, such as an MCP server's tool. `oah` turns each
named tool into a capability need and routes it.

- `oah capabilities show [--engine claude|codex]` lists what the child engine is configured with: names and
  flags such as enabled and approval, read from the Claude Code and Codex settings files. It never reports a
  connection and never copies a value such as a key or a URL.
- `oah capabilities plan --task T --stage S` routes the task's needs and writes nothing. A need takes one of
  these routes.

| Route | When |
|---|---|
| `direct-unverified` | The child is configured with the tool, but its schema, connection and approval are unknown. Good enough for an optional need only. |
| `parent-relay` | The lead has the tool. The lead runs it, records the result, and the worker gets the result as data. |
| `unsupported` | No usable route. A required need refuses. |

- A relay goes in three steps. `plan --parent-inventory FILE` prints a relay request for each required need the
  lead can meet. The lead runs the tool itself and stores the result with `oah capabilities relay --request FILE
  --capability ID --result-file FILE --invocation FILE`. Then `oah call ... --parent-inventory FILE
  --relay-result HASH` delivers it in a `## Capabilities` block after the skill texts.
- `[capabilities]` in `.oah/config.toml` holds `classes = {server = "class"}`, which tags a server with one of
  oah's capability classes, and `requires = {"<id>" = ["<id>", ...]}`, which adds dependencies between
  capabilities. A plugin's server also needs its plugin enabled. Dependencies resolve in at most three rounds,
  and a cycle refuses with the chain it found.

**A configured name is not a connection.** A required need is met only by a fresh relay. A tool that is only named
in configuration refuses with `OAH_CAPABILITY_UNSUPPORTED(unknown-schema)` or `(pending-approval)`. What it costs
you: for a required tool, the lead fetches the data and records it before the dispatch.

**A relay result is data, and it is short-lived.** It must be valid UTF-8, at most 65,536 bytes, free of oah
markers and of anything that looks like a secret, and it expires 300 seconds after its request. It is bound to the
exact task, design, needs and inventories it was made for. A review run stays isolated and never takes one
(`OAH_CAPABILITY_UNSUPPORTED(review-isolated)`). What it costs you: you relay close to the dispatch, and again
after anything changes.

**oah never installs, enables or connects a tool.** A missing plugin or server refuses with
`OAH_CAPABILITY_DEPENDENCY_UNSATISFIED` or `OAH_CAPABILITY_UNAVAILABLE`. What it costs you: you install or
enable it yourself and plan again.

## Jev in these steps

Jev chooses nothing on this page in this release. Coverage, discovery, delivery and capability routing follow
fixed rules, and the capability plan records `jev: unused(not-implemented)`. The skill re-ranker described in
[Skill selection](concepts.md#skill-selection) reorders installed skills for the brief; it never changes which
skill texts are delivered.

## Limits in this release

- Skill texts are always delivered inline. A reference mode that hands the worker paths instead does not exist yet.
- `oah fix` correction runs carry the skill texts but record no bundle in `skills.json` and do not hash installed
  copies again at launch. `oah fix` takes no relay inputs, so a correction whose skill needs a lead-only tool
  refuses.
- Routes are planned before the run starts. oah does not watch the child's tool startup, and it never reports the
  `direct` route, which would need that.
- The budgets are fixed byte counts. They do not measure the model's context window.
