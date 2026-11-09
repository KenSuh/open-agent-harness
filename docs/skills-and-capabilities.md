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
  `OAH_COVERAGE_INCOMPLETE` until the design is complete. `--next` names one stage to work on next (see
  [Jev in these steps](#jev-in-these-steps)).
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
- `oah skills review --links P/D` checks again every reviewed skill that the design's current coverage links, one
  fetch each, up to 16. A link whose upstream did not move prints nothing. A moved upstream is reviewed at its new
  commit and printed as a proposal; the link keeps its pin until you link the new review and accept the design. A
  check that fails says `failed, not checked` and the command exits 69.
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

**A refresh proposes; it never moves a pin.** What it costs you: after an upstream skill changes, its new text
reaches workers only after you read the proposed review, link it and accept the design again.

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

**One prepared identity.** Local admission checks `[skills] exclude`, each need's host and scope, mandatory
selections, current capability approval and schema, relay eligibility and expiry, the task and lease generation,
engine and stage. An excluded mandatory skill refuses; an omitted optional skill records its exact reason.
Search, review, refresh and skill advice require this live admission, including disabled, cached and retry paths.
A direct API call without admission refuses before transport. `PreparedE` binds these facts to the qualified
target profile and final render digests. Every launch and native settlement checks the current facts again under
the final guard. This includes a run prepared without managed content: adding its first task skill selection
requires preparation again. A changed or expired identity refuses and preserves the earlier record. Corrections
report `OAH_FIX_STATE_CHANGED` through their existing correction interface. An unchanged native
selection settles once; an identical retry returns that same receipt. Corrections freeze the verified needs,
selections and authorized relays and save only their closed digest/count/enum projection; worker text is resolved
in memory. What it costs you: queued work, a correction or a settlement must be prepared again after a relevant
local change or expiry; a direct API call cannot skip admission.

**One request boundary.** `skills find --task` checks the same existing required capabilities, target capacity
and access as a dispatch. Managed skill ordering also rechecks that admission immediately before its request.
Taskless exploration and design inspection keep their own bounded lead admission. Every managed search,
collector fetch and advice request writes a bounded attempt summary before sending. What it costs you: an
unavailable required capability or a changed local input needs an explicit correction and a new preparation
before another request; failed work that used requests or tokens stays inspectable before retry.

**Ordinary work keeps its existing path.** With no managed delivery, discovery, capability inputs or permitted,
configured task advice, the same prompt, judges and reservation sequence run. Global rerank and judgement
enable flags alone require no managed target profile. What it costs you: nothing; a run that cannot reach
managed work behaves exactly as before.

A required skill that cannot be delivered initially refuses the call with a
typed code: `OAH_SKILL_MANDATORY_MISSING`, `OAH_SKILL_REFERENCE_UNAVAILABLE` (`inaccessible` or
`unsupported-engine`, `profile` or `access`) or `OAH_SKILL_NOT_DELIVERABLE` with its reason (`excluded`, `scope`,
`user-only`, `license`, `host`, `marker` or `pin-mismatch`). There is no retry and no fallback,
and no run folder is created. An optional skill that cannot be delivered is skipped and listed. What it costs
you: a missing review or a moved pin blocks the dispatch until you review again or unlink the skill.

**One qualified target route.** Required managed delivery needs an exact, current `TargetBudgetProfileV1` for the
host, CLI, model, effort and route. A configured token count or model catalog is insufficient. The packaged
qualification registry currently contains **no real qualified profiles**, so managed routes refuse before advice
with `OAH_SKILLS_BUDGET_EXCEEDED(target-capacity-unknown)`. Test qualification fixtures do not qualify a real host.
A profile must pin evidence and expiry, finite input capacity, output/host/existing-context reserves, and render
and tool-schema overheads. Before advice, oah bounds the complete reachable brief, deferred design ReadPlan
payloads, required references and every planned read/tool envelope against the remaining input capacity. It
checks the exact final render locally again. What it costs you: required managed content works on a qualified
route; elsewhere use inline or smaller material outside managed delivery, or split the task; unknown capacity
is visible before any request.

**Independent byte budgets.** Serialized inline skill texts may take up to 196,608 bytes; the whole reachable
brief, including full referenced payload renders and read envelopes, may take up to 524,288 bytes. Each required
line/byte slice contributes its own read envelope to both that total and the token upper bound; 29,000 short
lines read in slices of at most 2,000 lines require 15 envelopes. Deferred design
content also keeps its independent 262,144-byte limit. There are at
most 128 payloads, counting every skill body and auxiliary file. Every inline file is included once, including
packs with more than 26 auxiliary files, using numeric labels. Over a budget, the call refuses with
`OAH_SKILLS_BUDGET_EXCEEDED` and measured totals. References bypass neither the total render limit nor target
capacity. What it costs you: a genuinely oversized pack must be split or optional material dropped; a valid
27-file pack receives every file. A later local change can still refuse after earlier requests were made.

**Large files by reference.** When the skill texts do not fit the skill budget, oah takes a skill's auxiliary
files out of the brief, largest first, and names each one instead: "Read `<path>` in full with your Read tool
before you start", with its line count and digest. The skill text itself always stays inline. Reference admission
also requires the actual executable, CLI version, tools and read/access policy to pass the shared task-context
qualification on every platform, and rechecks their identity at launch. This works only for
a Claude process run (`oah call claude` in the foreground, in the background or as an Orca worker) whose working
directory holds an installed copy of the skill with the pinned bytes, for example under `.claude/skills/<name>/`.
For that run oah asks Claude for `--output-format stream-json --verbose` (no other argument changes), pairs each
Read in the stream with the file's bytes, and writes `skills-receiving.json` in the run folder:
`reference_delivery: complete` when every required file was read whole, `incomplete` otherwise. An unread file
leaves the run's result as it is, adds a warning, and the record says the delivery is incomplete. A stream without
a result event fails the run with `OAH_ENGINE_FAILED`, and reads that cannot be recorded fail it with
`OAH_SKILL_RECEIVING_FAILED`. Codex, a native subagent and `oah fix` corrections show oah no reads, and native
Windows has no qualified read profile yet, so there a required file that does not fit inline refuses with
`OAH_SKILL_REFERENCE_UNAVAILABLE(unsupported-engine)`; a copy outside the working directory refuses with
`OAH_SKILL_REFERENCE_UNAVAILABLE(inaccessible)`. Just before the engine starts, oah hashes each referenced file
again. What it costs you: such a skill has to be installed in the repository and runs only on Claude process runs
outside native Windows, and the model spends context reading each file whole.

**The stream is checked while it runs.** On that same run oah reads Claude's stream as it arrives. The first line
must be Claude Code's init event for this run: a session id, the run's working directory, the Claude Code version
oah prepared and the same permission mode. When that line is missing, does not match, is not JSON, is larger than
256 KiB or has not arrived after 10 seconds (or the run's timeout, when that is shorter), oah stops the run's whole
process tree at once; the run fails with `OAH_ENGINE_FAILED` and does not count. After the init, a line that is
not JSON, a second init that names another session and an event of another session stop the run too, while a line
larger than 256 KiB is skipped unread. oah also pairs each MCP tool use (`mcp__<server>__<tool>`) with its own
result and writes `capabilities-receiving.json` in the run folder: per use, its capability ID, `planned` or
`unplanned`, the result `ok`, `error`, `denied` or `unobserved`, and digests of the session, the use, the arguments
and the result, never their text. It reads at most 16 MiB of events, keeps at most 64 uses waiting for a result
and records at most 1,024 uses. A record that cannot be written fails the run with
`OAH_CAPABILITY_RECEIVING_FAILED`. What it costs you: a Claude session that takes more than 10 seconds to start,
for example while a slow MCP server connects, is stopped, and a stop cannot undo what the child did before its
first line.

**Skills meant for explicit use stay with you.** A skill that only a person may invoke is never delivered. A
required one refuses with `OAH_SKILL_NOT_DELIVERABLE(user-only)`. What it costs you: you run such a skill
yourself.

**Installed copies are named, not guessed.** oah reads installed skills from the project, its own kit cache, your
user folders and the Claude plugins you installed, in that order, and the first copy of a name wins. When a skill
chosen for the brief also exists elsewhere with different contents, the brief's notes name every copy with its
origin and digest. Codex's plugin cache (`plugins/cache` under `CODEX_HOME`, else `~/.codex`) is read only with
`[skills] codex_plugin_cache = true`. Its copies come last and are shown as `plugin-cache (not proof of enabled)`,
because the cache also keeps plugins you turned off and old versions. What it costs you: a skill that lives only in
Codex's plugin cache is not used until you turn the setting on, and even then oah cannot tell whether Codex has that
plugin enabled.

## Skills for a task and its subtasks

Each `oah call` also works out the task's skill needs for its dispatch stage. Without a design, that is one need
for the task's intent; with a design, one need for each stage check the accepted coverage asks a skill for. A need
names the packet bytes that state the requirement (a range and a digest, never the text), the subtask, the public
technology words, the platform, a digest of your local constraints and the discovery settings. Any change makes it
another need.

- `oah skills find --task T` searches for a started task's needs under that task's own admission, within
  `max_queries`. `--subtask ID`, `--tech LABEL=VALUE`, `--constraint TEXT` (a version, for example) and
  `--required` describe one need instead; with a design, `--design-stage` and `--check` name its check.
  `--dispatch-stage` picks `initial` (the default), `correction` or `review`. `--out FILE` saves the needs file,
  and `--needs FILE` searches again for a saved file's needs.
- `oah skills select --needs FILE [--need ID] --review ID ...` binds reviewed skills to one need of a task without
  a design. Only the lead selects. A selection is stored once under `<state>/skills/task-selections/` and applies
  only under the lease it was made under.
- `oah call ... --skill-needs FILE` and `oah skills plan ... --skill-needs FILE` use that file's needs. oah checks
  each one again against the current packet, scope, kind, risk, platform and settings, and refuses a need that no
  longer matches with `OAH_SKILL_NEEDS_INVALID(stale)` (the other reasons are `malformed`, `foreign`, `scope` and
  `design-bound`).
- A selected skill is delivered like a linked one; its line in the brief reads `For task need …`. A required need
  without a selection refuses with `OAH_SKILL_MANDATORY_MISSING` before anything is spent. Just before the engine
  starts, oah checks that the selection is still the lead's current one, or refuses with
  `OAH_SKILL_NOT_DELIVERABLE(changed)`.
- After admission, the call itself looks for each need that names a technology or a design check and stores a
  task discovery record; `skills.json` lists them under `task_discovery`. A reviewed result is only a
  proposal until you select it, or, with a design, link it in the coverage and accept the design. A line starting
  with `OAH_SKILL_DISCOVERY` says when a proposal waits, when a technology decision changed under a linked skill,
  or when the search failed. A technology value only you use is never sent; its record shows the
  `public-term-required` gap. A need that names no technology and no design check is not stored at all.

**A task with a design follows its design.** `oah skills select` refuses for it with
`OAH_SKILL_NEEDS_INVALID(design-bound)`, and a required need that the accepted coverage does not link refuses the
call. What it costs you: a new skill for such a task needs a coverage link and one more design acceptance.

**Selections belong to one need and one lease.** Two subtasks with the same technology have different needs and
selections, and another version constraint is another need. After the task is started again, its old selections no
longer apply. What it costs you: you select again for a changed need or a new lease.

**The call may search.** A call whose needs carry public technology words sends up to `max_queries` searches to
skills.sh after admission, each with a 15-second limit and never under a launch lock. Answers and an unchanged
need's discovery are reused for `cache_hours`. `[skills.discovery] enabled = false` turns it off. What it costs you:
the first such call each day can wait for the search. `oah fix` corrections never search and do not carry task
selections.

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
  --relay-result HASH` delivers it in a `## Capabilities` block after the skill texts. When the call takes
  `--skill-needs FILE`, give `plan` the same file, so a skill selected for a task need gets its requests too.
- Pass one stored result per need. The order of `--relay-result` flags never changes the plan or the refusal, and
  two different results for one need refuse with `OAH_CAPABILITY_RELAY_REQUIRED(conflicting)`.
- On a run whose stream oah checks live (see **The stream is checked while it runs** above),
  `capabilities-receiving.json` records each MCP tool use. A tool the plan selected is `planned` and any other tool
  `unplanned`; oah records an unplanned tool and does not block it.
- `[capabilities]` in `.oah/config.toml` holds `classes = {server = "class"}`, which tags a server with one of
  oah's capability classes, `requires = {"<id>" = ["<id>", ...]}`, which adds dependencies between
  capabilities, and `docs = {"<id>" = "SOURCE:PATH"}`, which links one exact capability to a public documentation
  file that Jev may read (see [Jev in these steps](#jev-in-these-steps)). A plugin's server also needs its plugin
  enabled. Dependencies resolve in at most three rounds, and a cycle refuses with the chain it found.

**A configured name is not a connection.** A required need is met only by a fresh relay. A tool that is only named
in configuration refuses with `OAH_CAPABILITY_UNSUPPORTED(unknown-schema)` or `(pending-approval)`. What it costs
you: for a required tool, the lead fetches the data and records it before the dispatch.

**A relay result is data, and it is short-lived.** It must be valid UTF-8, at most 65,536 bytes, free of oah
markers and of anything that looks like a secret, and it expires 300 seconds after its request. It is bound to the
exact task, design, needs and inventories it was made for. A review run stays isolated and never takes one
(`OAH_CAPABILITY_UNSUPPORTED(review-isolated)`). What it costs you: you relay close to the dispatch, and again
after anything changes.

**Capability needs are checked before anything is asked.** A required capability nobody can provide, an unmet
dependency, and a missing, expired, failed, stale or conflicting relay refuse together with the skill admission,
before any skill search, Jev question, budget gate or run folder, for an ordinary call and a native offer alike.
After the searches and Jev advice, the plan is built and checked once more with its optional class needs. What it
costs you: a refused call shows no skill discovery for that attempt, and when you relayed one tool twice you name
the result to deliver, since oah never picks one. A relay that expires between the two checks still refuses, after
the searches and questions already sent.

**oah never installs, enables or connects a tool.** A missing plugin or server refuses with
`OAH_CAPABILITY_DEPENDENCY_UNSATISFIED` or `OAH_CAPABILITY_UNAVAILABLE`. What it costs you: you install or
enable it yourself and plan again.

## Jev in these steps

Jev never decides anything alone on this page. Coverage, discovery, admission and capability routing follow fixed
rules; Jev may only choose among options those rules already allow, and oah applies an answer only after the judge
checks it again against the same inputs. Jev off, an uncertain or stale answer and a failed request all keep the
fixed order. Jev is on when `[rerank] enabled`, `[rerank.judgement] enabled` and a key are all set, and text you
wrote leaves only with its own permission: `send_design_text` for design text and `send_task_text` for the task's
packet intent, both off by default. A native offer asks no judge at all and records `jev_status: unused(native)`.
The skill re-ranker described in [Skill selection](concepts.md#skill-selection) reorders installed skills for the
brief; it never changes which skill texts are delivered.

**Jev may only reorder the optional skills of one check.** When an ordinary run delivers two or more optional
skills for the same design check and Jev is on, oah asks Jev once for that check whether each skill's text gives
concrete guidance for it, and puts those skills in that order. Required skills keep their place, no skill is added
or dropped, and every text stays the same.
`skills.json` records the outcome as `skill_fit` beside the bundle. Every Jev-off state (`unused(judgement-off)`,
`unused(not-enabled)`, `unused(no key)`, `unused(disabled-by-brief)` for `--no-rerank`) and every answer oah cannot
apply, such as an uncertain one, keeps the bundle exactly as admitted. TypeSafe checks the answered ids strictly,
so a reply that leaves a skill out reads `unused(error:bad-response)`; `unused(error:dropped)` is the code for a
judge provider that returns a partial ranking. Only the first 4,000 bytes of each reviewed `SKILL.md`, read from
its verified public snapshot, and the fixed stage and check names are sent, never your design or task text. What
it costs you: one judge request per such check on each run, and `oah skills plan`, which asks no judge, shows the
order before advice.

**Jev may choose which search runs first.** When a need in `oah skills find` has two or more public technology
words, such as `--tech auth=passkey --tech platform=supabase`, oah asks Jev which word's query covers the need, and
runs that query first so the request budget reaches it. Only the provider name and the public words leave, as
fixed labels; the queries stay oah's own templates. The report's `search_path` lists the options, the one run
first and `jev`. One public word, Jev off and every answer oah cannot apply keep template order. What it costs
you: one judge request per such need on each `find`. Discovery inside `oah call` keeps template order.

**Jev may suggest the next design stage.** `oah design coverage P D --next` names the first open stage of the
current revision. When two or more stages are open and `send_design_text = true`, oah asks Jev which of them
covers the unresolved decision, sending the stage names and the start of the design's own text (at most 4,096
bytes), and names that stage instead. The readout prints `next: <stage> (jev=..., applied=yes|no)`. One open stage
asks nothing; with none open, the readout says `complete` or `blocked`. What it costs you: one judge request per
`--next`. It is a suggestion; nothing is stored and the coverage stays as it is.

**Jev may choose a capability for each part of a task.** In an ordinary `oah call` (not a native offer, not a
review), each task need without a design covers one span of the packet's intent section. With
`send_task_text = true`, oah asks Jev, for that span alone, which of oah's capability classes it needs
(`capability-needs/v1`). A class read as yes becomes an optional need. Its candidates are the qualified entries of
that class, which in this release means the ones the lead's inventory (`--parent-inventory`) reports with current
evidence and read-only effects, in inventory order; the first eight are options and the rest are deferred. When
every option has documentation linked in `[capabilities] docs`, oah asks Jev which option's documented function
fits the span (`capability-select/v1`) and lists that entry; otherwise the first stands. The chosen entry appears
in the `## Capabilities` block as an optional route, and oah never calls it. `capabilities.json` keeps each class
judgement in `class_advice_refs` and each choice in `entry_advice`, with its outcome: `selected`, `no-match`,
`uncertain`, `off`, `error`, `stale`, `single-option`, `no-candidate` or `no-public-candidate-evidence`. A class
need never refuses a run, never asks for a relay and never changes the preparation id, so a stored relay result
stays valid. `oah capabilities plan` asks nothing and records `jev: unused(plan-pure)`. What it costs you: on each
run, one judge request per task need and one per class with two or more documented options, and each request
carries that span of your packet text.

**Jev compares entries only by documentation you link.** `docs = {"mcp:docs/lookup" = "docs:document-text"}`
names, for one exact capability ID, a source registered with `oah sources` and a file of its verified snapshot
(`document-text` for a document page, or a path such as `README.md` in a repository). oah sends the first 4,000
bytes of that file, read again from the snapshot. An entry is never matched by its name, and a server that is only
named in configuration never becomes an option. What it costs you: you register a source and link every entry Jev
should compare; one missing link keeps the first entry for that class.

## Limits in this release

- Reference reads are paired after the run, from the whole stream. A child that starts working before it reads the
  file is not stopped; oah records the unread file instead of failing the run. Reads by the child's own subagents
  do not count.
- The live check is observation, not a barrier: a child can act before its first line, and oah stops it only
  after that. It runs only where oah itself turned on stream-json for skill references. Runs with a design
  transport, Codex runs, native subagents and `oah fix` corrections are not read live.
- `oah fix` correction runs carry verified skill texts in memory and save a closed E projection in `skills.json`.
  Launch rechecks the current copies and frozen authorized relays. The command accepts no new relay inputs; a
  required relay must already belong to the verified ordinary preparation and still be current.
- Routes are planned before the run starts. The init event names tools only, so no tool use is checked against a
  schema (`schema_digest` stays null), and oah never reports the `direct` route, which would need a host that
  waits before any work until oah approves.
- Real target qualification is still pending. The empty packaged registry refuses managed delivery until a
  measured profile with evidence and expiry is reviewed; catalog context sizes cannot substitute for it.
- Task selections reach every `oah call` run and native offer; `oah fix` carries verified ordinary selections as
  frozen inputs. A need that oah derives, or that `oah skills find --task` writes, covers the packet's whole intent
  section. A lead may narrow it to a byte range in the needs file, and oah checks that range against the packet
  again; the command offers no way to choose one.
- A native subagent's reads are seen only through `oah skills read`; the host's own file reads are not observed.
  `oah call settle` rechecks capability relay identity and expiry before first settlement and receipt reuse.
- Capability classes come only from Jev and from the tools a skill names. A dispatch's stage and kind add no
  class, packet lines that name a capability ID are not read, and an entry chosen for a class does not bring its
  own dependencies.
- A class need that finds no candidate leaves no plan when nothing else is planned, so `capabilities.json` is not
  written for that run.
- Jev chooses the search order only in `oah skills find`. `oah design coverage --next` only suggests a stage;
  design start and resume do not ask it in this release.
