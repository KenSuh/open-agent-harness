# Design sources

A design's **materials** are the libraries, services and documents it builds on. `oah sources` follows the
public upstreams those materials come from, keeps a verified snapshot of each version it saw, and lets a
design record which snapshot each of its decisions was made against. Later checks then tell you when an
upstream moved on, when the design's requirements changed under a decision, and what changed between two
snapshots.

Sources live in this clone's oah state folder (inside the repository's git folder, or under
`OAH_STATE_DIR`). Nothing there is committed. A design itself changes only through `oah design save`.

## Commands

| Command | What it does |
|---|---|
| `oah sources register <url>` | Follows a GitHub repository (`--track release` or `commit`) or an https documentation page (`--track doc`). `--id`, `--tag-prefix`, `--prerelease`, `--release-order`, `--ref`, `--paths` and `--manifest` narrow what counts; `--for-design PROJECT/DESIGN` links it to a design. |
| `oah sources remove <id>` | Stops following a source. Its stored snapshots and observations stay. |
| `oah sources list` / `show <id>` | Shows each source's freshness, last attempt, last confirmation and verified snapshot. Reads only. |
| `oah sources check [<id>...]` | Checks the named sources, or all of them, now. |
| `oah sources check --scheduled` | One bounded, fair pass over the persistent queue; this is what the daily OS entry runs. |
| `oah sources diff <id> --from S1 --to S2` | Compares two stored snapshots of one source without the network. |
| `oah sources proposals [--design PROJECT/DESIGN]` | Lists source changes a design could review. Reads only. |
| `oah sources dismiss <key> --reason "..."` | Hides one proposal, with your reason. |
| `oah sources materials render --design PROJECT/DESIGN --input FILE` | Builds a design's materials part from a selection file. Prints it, or writes one new file with `--out`. |
| `oah sources materials check --design PROJECT/DESIGN` | Says whether each material decision still matches its inputs. `--revision N` checks an older revision. |
| `oah sources schedule install\|remove\|status` | Adds, removes or inspects the daily launchd agent (macOS) or Task Scheduler task (Windows), with your consent. |

`--json` prints the full report on every reading command. Exit codes are `0` for success, `1` for a refusal,
a partial pass, a source that could not be verified or a decision that needs attention, and `69` when the
state, the configuration or the OS scheduler cannot be used (for example `OAH_SOURCES_STATE_CORRUPT`,
`OAH_SCHEDULE_TOOL_MISSING`, `OAH_SCHEDULE_UNSUPPORTED`).

## What a check records

- **Snapshots are immutable.** A snapshot is identified by the digest of exactly what was fetched. oah never
  rewrites or prunes one, so an old decision can always be compared with what it relied on.
- **Times stay separate.** Each source keeps when it was last *attempted*, when an observation was last
  *confirmed*, and when its snapshot was *verified*. A failed attempt never moves the other two.
- **Freshness** is `fresh`, `stale`, `unverified`, `unavailable` or `unknown`. A source is fresh for
  `[sources] fresh_hours` after its last confirmation.
- **Licenses are read in full.** oah keeps the actual license text and any nested license files, and judges
  eligibility against `[sources] license_allow`. A license it cannot recognise stays unknown; it is never
  guessed.
- **Requests are public and credential-free.** oah sends no cookie or authorization header and refuses to
  run through an HTTP proxy, because a proxy would choose the destination it cannot vet.

A check reports one result per source: `unchanged`, `recovered`, `changed`, `unverified`, `blocked`,
`failed`, `superseded`, `skipped-fresh`, `skipped-rate-limited`, `over-budget`, `disabled` or
`state-corrupt`.

## Comparing two snapshots

`oah sources diff` reads only stored snapshots. It never fetches, never updates a confirmation time and
never writes a design. If this clone no longer holds one of the snapshots, the report is `unavailable` and
names it.

The report says whether it is `complete`, `partial` or `unavailable`. It lists every changed path (the
text view shows the first 200; `--json` lists all), the license and release changes, and up to 50 text diffs
of at most 200 lines each, 64 KiB in total. Text from an upstream is labelled as external data, and control
and formatting characters in it are shown escaped (`‮`), so it cannot rewrite your terminal.
`OAH_SOURCES_SOURCE_MISMATCH` means the ID now names a different upstream than the snapshots came from.

## Materials in a design

`oah sources materials render` turns a selection file into the design's `materials` part. You write the
decisions; oah fills in every fact from the verified snapshot:

```json
{
  "lang": "en",
  "decision_inputs": {"calendar": {"need": "Show a teacher's free slots", "platform": ["web"]}},
  "decisions": [{"id": "calendar", "status": "selected", "chosen": "calendar-lib",
                 "reason": "Maintained and MIT licensed"}],
  "materials": [{"id": "calendar-lib", "source": "calendar-lib", "choice": "selected", "decision": "calendar"}]
}
```

- License, release, snapshot and check times come from the stored snapshot. If the file supplies them, oah
  ignores them and says so (`OAH_MATERIALS_INPUT_IGNORED`).
- A source with no verified snapshot yet refuses. `--allow-unverified` records it as unverified instead.
- The part embeds **portable evidence**: the exact bytes each material relied on. A fresh clone with no
  sources state can still check it, and its original observation times survive later refreshes. The
  whole part must stay under 1 MiB, or render refuses with `OAH_MATERIALS_EVIDENCE_TOO_LARGE` before
  writing anything; narrow the selected paths or split the decisions across designs.
- Output goes to standard output. `--out FILE` creates one new file inside this repository and refuses an
  existing file, a link or a path outside the repository (`OAH_MATERIALS_OUTPUT_EXISTS`,
  `OAH_MATERIALS_OUTPUT_PATH_INVALID`). On Windows `--out` refuses with
  `OAH_MATERIALS_OUTPUT_UNVERIFIABLE`; redirect standard output instead.

Save the result as a new revision with
`oah design save <project> <design> --expect-revision N --expect-digest D --reason "..." --part materials=FILE`.

`oah sources materials check` gives each decision one status, most severe first:

| Status | Meaning |
|---|---|
| `invalid` | The part was edited by hand, its evidence does not verify, or it names another design or an unknown revision. |
| `unknown` | oah cannot read the design it belongs to. |
| `stale` | The accepted requirements behind the decision changed. |
| `needs-review` | The working (not yet accepted) requirements changed, or a source moved to a new snapshot or registration, or the license policy changed, and no resolution covers it. |
| `current` | The decision still matches the inputs it was made for. |

When a decision's inputs change, render refuses with `OAH_MATERIALS_REVIEW_REQUIRED` until the selection
records a resolution for it: `carry` keeps the same material for the new inputs, `reselected` records a new
choice. Each resolution names the exact old and new inputs, so a later check can tell it from an edit.

## Daily checks

`oah sources schedule install` adds a daily OS entry that runs `oah sources check --scheduled` in this
project, after you confirm (`--yes` agrees). Each pass is bounded to 30 minutes and works through a
persistent, fair queue. A pass that runs out of time keeps the queue, so the next pass continues where it
stopped instead of starting over, and sources that time out early never keep the later ones from being
attempted. `complete` means every source had an attempt in this cycle, not that every check succeeded.
A pass reports `complete`, `partial(over-budget)`, `partial(rate-limited)`, `partial(interrupted)`,
`not-due`, `busy` or `disabled`, and exits `1` when partial.

The OS wakes the entry once a day; `[sources] check_interval_hours` decides whether a pass is due.
`auto_check = false` turns every scheduled pass into a recorded no-op.

`oah sources schedule status` compares the definition oah would install with what the OS reports and names
each difference. The state is `scheduled`, `scheduled-different`, `disabled`, `unverifiable`, `file-only`,
`absent`, `unsupported` or `tool-missing`. It then shows three separate kinds of evidence:

- the last `oah sources check` of any kind, which does not show who started it;
- the last `--scheduled` pass, which a manual command can also produce;
- the OS scheduler's own record of running the entry, or `unknown` when the OS does not say.

Only the third shows that the OS ran it.

## Optional judgements

oah can ask a judge (TypeSafe Jev, see [keys](keys.md)) whether a source change matters to a design. Besides
`[rerank] enabled = true` and a key, this needs its own switch, which is off by default:

```toml
[rerank.judgement]
enabled = false           # true allows judgements on public source data
send_design_text = false  # true also allows excerpts of your design text
```

Even when enabled, oah sends only slices of verified public snapshots and fixed labels, plus design excerpts
only with `send_design_text = true`. Every outgoing string is screened for secrets and e-mail addresses, and
a hit sends nothing. A stored judgement applies only while its snapshot, design revision, license policy,
provider and model all still match. No `oah sources` command requests a judgement yet; these settings gate
the callers that will.

No judgement forces an answer. A question with several options also asks whether any option fits, so the
result is `chosen`, `no-match` or `uncertain`. Material classes are asked one at a time, so several classes
or none can apply. A yes/no answer of 0.7 or more counts as yes and 0.3 or less as no; these are suggested
starting points, not values tuned on real cases. A judgement is advice only: it never accepts, installs or
removes anything.

## Settings

All keys live under `[sources]` in `.oah/config.toml`; `OAH_CONFIG_INVALID` names a bad one.

| Key | Default | Meaning |
|---|---|---|
| `auto_check` | `true` | Whether scheduled passes check anything. |
| `check_interval_hours` | `24` | How long after a complete pass the next one is due (1 to 720). |
| `fresh_hours` | `24` | How long a confirmation keeps a source fresh (1 to 720). |
| `license_allow` | `["MIT"]` | Licenses a material may use: any of `MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`. |
| `max_document_bytes` | `1048576` | The largest documentation page fetched (1024 to 8388608). |
| `timeout_seconds` | `30` | The time allowed for one request (1 to 600). |

## Limits

- GitHub's unauthenticated rate limits apply. A rate-limited source waits for a later pass.
- Snapshots are never pruned, so the state folder grows with each changed snapshot.
- Release selection reads a bounded set of tags and releases. When it cannot see them all, the observation
  says `selection-incomplete` instead of claiming the newest. A selected release is the newest oah observed,
  not a promise that nothing newer exists.
- `--out` on Windows refuses, as above.
- `schedule status` reads `launchctl print` on macOS and the Task Scheduler on Windows. A fact it cannot read
  makes the state `unverifiable`; it is never assumed.
