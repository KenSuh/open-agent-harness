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
typed code: `OAH_SKILL_MANDATORY_MISSING`, `OAH_SKILL_REFERENCE_UNAVAILABLE` (`inaccessible` or
`unsupported-engine`) or `OAH_SKILL_NOT_DELIVERABLE` with its reason (`user-only`, `license`, `host`, `marker` or `pin-mismatch`). There is no retry and no fallback,
and no run folder is created. An optional skill that cannot be delivered is skipped and listed. What it costs
you: a missing review or a moved pin blocks the dispatch until you review again or unlink the skill.

**Fixed byte budgets.** Skill texts may take up to 196,608 bytes and the whole brief up to 524,288 bytes, with at
most 128 items. Over a budget, the call refuses with `OAH_SKILLS_BUDGET_EXCEEDED` and lists each skill's size, so
you can split the task or unlink optional skills. What it costs you: a large skill set has to be split across
tasks.

**Large files by reference.** When the skill texts do not fit the skill budget, oah takes a skill's auxiliary
files out of the brief, largest first, and names each one instead: "Read `<path>` in full with your Read tool
before you start", with its line count and digest. The skill text itself always stays inline. This works only for
a Claude process run (`oah call claude` in the foreground, in the background or as an Orca worker) whose working
directory holds an installed copy of the skill with the pinned bytes, for example under `.claude/skills/<name>/`.
For that run oah asks Claude for `--output-format stream-json --verbose` (no other argument changes), pairs each
Read in the stream with the file's bytes, and writes `skills-receiving.json` in the run folder:
`reference_delivery: complete` when every required file was read whole, `incomplete` otherwise. An unread file
leaves the run's result as it is, adds a warning, and the record says the delivery is incomplete. A stream without
a result event fails the run with `OAH_ENGINE_FAILED`, and reads that cannot be recorded fail it with
`OAH_SKILL_RECEIVING_FAILED`. Codex, a native subagent and `oah fix` corrections show oah no reads, so there a
required file that does not fit inline refuses with `OAH_SKILL_REFERENCE_UNAVAILABLE(unsupported-engine)`; a copy
outside the working directory refuses with `OAH_SKILL_REFERENCE_UNAVAILABLE(inaccessible)`. Just before the
engine starts, oah hashes each referenced file again. What it costs you: such a skill has to be installed in the
repository and runs only on Claude process runs, and the model spends context reading each file whole.

**Skills meant for explicit use stay with you.** A skill that only a person may invoke is never delivered. A
required one refuses with `OAH_SKILL_NOT_DELIVERABLE(user-only)`. What it costs you: you run such a skill
yourself.

## Native subagents

`oah call ENGINE --stage S --task T --launcher native` hands the brief to the AI you work with instead of starting a
separate process. That AI, the lead, gives it to a subagent of its own: Claude Code's Agent tool, or a Codex native
subagent.

- It runs the same checks and skill admission as any `oah call`, then writes one run folder with the brief,
  `skills.json` and `native-offer.json`, and prints the exact message with its byte count and SHA-256. For Claude it
  prints the three Agent tool parameters: `subagent_type` (the `oah-<tier>` agent that `oah init` writes),
  `description` and `prompt`. For Codex it prints the message only.
- It asks no paid judge, reserves nothing, counts no round and starts no engine. The run stays `prepared`, and
  neither `oah wait` nor an Orca worker starts it.
- The brief's first line tells the subagent that it is a native subagent, not a separate oah process, and asks it
  to end its reply with an `oah-canary:` line. The subagent may run `oah skills read --run R --item N` to read a
  skill text again, and that read is recorded.
- When the subagent returns, the lead saves its reply to a file and runs `oah call settle --run R --offer-digest H
  --host-child-id ID --outcome succeeded|failed --result-file FILE [--canary VALUE]`. Settle checks the task, intent,
  packet, scope, lease, design, skill texts and host target again. Anything that changed refuses with
  `OAH_NATIVE_OFFER_STALE` and keeps the old offer as evidence. An offer can be settled for 24 hours.
- Settle writes `native-settlement.json` and `skills-receiving.json`. Running it again with the same values returns
  the first receipt; other values refuse with `OAH_NATIVE_SETTLE_CONFLICT`.

**Offered is not delivered, and settled is not applied.** The host gives oah no signed receipt, so delivery is
recorded as `parent-attested` and the evidence as `cooperative`. A matching canary proves only that the token came
back. A skill text counts as read only when `oah skills read` recorded the read, and nothing marks a skill as
applied. A settlement is not counted as a review or a process success, finishes no task and releases no lease. What
it costs you: one more command, `oah call settle`, after each native subagent returns.

**Native is a flag, never a default.** Only `--launcher native` selects it; `[call] launcher = "native"` refuses with
`OAH_CONFIG_INVALID`. A native subagent shares the lead's environment, so oah's worker guards are cooperative: oah
cannot stop it from running a lead-only command. What it costs you: nothing for existing setups; you choose native
for each call.

**The returned result is data.** The result file is never run. It must be UTF-8, at most 65,536 bytes, without a
line starting with `<!-- oah`, or settle refuses with `OAH_NATIVE_RESULT_INVALID`. What it costs you: you save a
short reply, not a long log.

A Claude offer needs the tier agent file; without it the offer refuses with `OAH_SKILL_HOST_UNSUPPORTED`. A design
too large for the brief also refuses on this route, because a subagent's reads cannot be observed.

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

Jev chooses no skill and no tool on this page in this release. Coverage, discovery, admission and capability
routing follow fixed rules, and the capability plan records `jev: unused(not-implemented)`. A native offer asks no
judge at all and records `jev_status: unused(native)`. The skill re-ranker described in
[Skill selection](concepts.md#skill-selection) reorders installed skills for the brief; it never changes which
skill texts are delivered.

**Jev may only reorder the optional skills of one check.** When an ordinary run delivers two or more optional
skills for the same design check and Jev is on (`[rerank] enabled`, `[rerank.judgement] enabled` and a key), oah
asks Jev once for that check whether each skill's text gives concrete guidance for it, and puts those skills in
that order. Required skills keep their place, no skill is added or dropped, and every text stays the same.
`skills.json` records the outcome as `skill_fit` beside the bundle. Every Jev-off state (`unused(judgement-off)`,
`unused(not-enabled)`, `unused(no key)`, `unused(disabled-by-brief)` for `--no-rerank`) and every answer oah cannot
apply, such as an uncertain one, keeps the bundle exactly as admitted. TypeSafe checks the answered ids strictly,
so a reply that leaves a skill out reads `unused(error:bad-response)`; `unused(error:dropped)` is the code for a
judge provider that returns a partial ranking. Only the first 4,000 bytes of each reviewed `SKILL.md`, read from
its verified public snapshot, and the fixed stage and check names are sent, never your design or task text. What
it costs you: one judge request per such check on each run, and `oah skills plan`, which asks no judge, shows the
order before advice.

## Limits in this release

- A reference read is observed from the stream after the run. oah cannot stop a child that starts working before it
  reads the file; it records the unread file instead of failing the run. Reads by the child's own subagents do not
  count.
- `oah fix` correction runs carry the skill texts but record no bundle in `skills.json` and do not hash installed
  copies again at launch. `oah fix` takes no relay inputs, so a correction whose skill needs a lead-only tool
  refuses.
- Routes are planned before the run starts. oah does not watch the child's tool startup, and it never reports the
  `direct` route, which would need that.
- The budgets are fixed byte counts. They do not measure the model's context window.
- A native subagent's reads are seen only through `oah skills read`; the host's own file reads are not observed.
  `oah call settle` does not check capability relays again.
- Jev's capability-needs question, the only one that may read the task's packet intent (and only with
  `[rerank.judgement] send_task_text = true`, off by default), is not asked by any step yet. The capability plan
  keeps its fixed rules.
