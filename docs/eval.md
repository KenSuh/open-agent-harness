# Evaluating the loop on real cases: `oah eval`

`oah eval` replays real cases reproducibly. It answers one narrow question: on these cases, what did
each variant find, miss and fix, and at what cost? It is an evaluation tool. It is not a release gate,
and its reports never claim that one variant is better in general.

Nothing here runs unless you use `oah eval`. Cases are yours and stay on your machine; the repository
ships only one synthetic fixture (`tests/fixtures/eval/`).

## The four steps

```text
oah eval prereg plan.toml --oracle oracles/case-a.toml --out prereg.json   # freeze the comparison
oah eval run prereg.json                                                 # run every variant
oah eval grade <run folder> --oracle oracles/case-a.toml [--verdicts v.toml]
oah eval report <run folder> [--json]
```

`oah eval run plan.toml` also works without a preregistration for `dev` and `control` cases. Such a run is
labelled exploratory, and holdout cases are refused.

## A case

A case is the code before a fix plus the requirement at that time. It is one folder:

```text
case-a/
  case.toml
  snapshot.bundle      # or a tar/zip archive made with `git archive`
  requirement.md
  task.md              # the task prompt, the same for every variant
  inputs/              # extra material every variant gets (may hold just a note)
```

```toml
id = "case-a"
split = "dev"                       # dev | control | holdout
provenance = "Where the case came from, de-identified."
requirement = "requirement.md"
prompt = "task.md"
forbidden = ["*.orig", "fix.patch"] # optional patterns refused anywhere in the workspace

[snapshot]
file = "snapshot.bundle"
format = "bundle"                   # bundle | tar | zip
commit = "<the full commit id before the fix>"
```

All paths stay inside the case folder. A bundle may hold the whole history, including the fix, because
only the snapshot commit's tree reaches a workspace. A tar or zip archive must be the snapshot tree. When
`git archive` recorded a commit id in it, that id must equal `commit`. Archives may hold only regular files
and folders.

### Splits

- **dev:** cases you look at while changing the implementation.
- **control:** cases you run for comparison but never tune on.
- **holdout:** cases that run only from a matching preregistration. A case you used to change the
  implementation must be relabelled `dev`. Set `used_for_development = true` in the plan and
  preregistration refuses it as holdout, and so does `oah eval run`.

## The oracle (expected answers)

The oracle is a TOML file **outside every case folder**. Only `oah eval prereg`, which hashes it, and
`oah eval grade` read it. No agent is given its path.

```toml
case = "case-a"

[[defect]]
id = "D1"
summary = "what the defect is"
files = ["src/index.txt"]  # a finding must name one of these files ...
keywords = ["stale"]       # ... and contain every keyword, ignoring case (give files, keywords or both)

[[test]]
id = "T1"
command = ["{python}", "check.py"]  # must fail on the starting snapshot; passing afterwards = fixed
timeout_seconds = 60
```

`{python}` is the interpreter running `oah`.

## The plan

```toml
[[case]]
path = "cases/case-a"            # relative to the plan file
used_for_development = false

[[variant]]
name = "baseline"
command = ["{python}", "{plan_dir}/variants/run_baseline.py"]
fake = false                     # true marks a fake engine: results are control-flow-only
timeout_seconds = 1800           # optional; default [eval] timeout_seconds
config = "configs/improved.toml" # optional oah configuration, written to .oah/config.toml in the workspace
```

A variant is a named command, or an `oah` configuration that its command uses. All variants get identical
inputs. The program is resolved through PATH like every `oah` tool, and Windows batch files are never
launched.

### What a variant sees

- **Working folder:** a fresh repository with one commit of the snapshot tree. It holds no original
  history, no later objects and no remote. The commit has a fixed identity and date, so every variant of
  a case starts from the same commit id.
- **stdin:** the task prompt.
- **`OAH_EVAL_INPUTS`:** a copy of `inputs/`.
- **`OAH_EVAL_REQUIREMENT`:** a copy of the requirement file.
- **`OAH_EVAL_OUTPUT`:** an empty folder. The variant may write `result.json` there:

```json
{"findings": [{"id": "F1", "file": "src/index.txt", "summary": "..."}],
 "usage": {"input_tokens": 1200, "output_tokens": 300, "cost_usd": 0.02},
 "rounds": 2, "corrections": 1, "engine": "fake"}
```

Every field is optional. A finding can also carry `files`, `title`, `detail` and `evidence`. Usage, rounds
and corrections are reported only when the variant records them; otherwise they are `unknown`.
`"engine": "fake"` (or `fake = true` in the plan) labels the result `control-flow-only`.

## Preregistration

`oah eval prereg` writes a frozen JSON and `<file>.sha256`, and prints the hash. The JSON holds:

- the cases, each with its split, a SHA-256 of the case folder, the snapshot commit,
  `used_for_development` and the SHA-256 of its oracle;
- the variants, with their exact commands, the SHA-256 of every file a command names, and any config
  text;
- the metrics and the grader rules.

The file is never rewritten. To change anything, preregister again to a new file. Keep the hash somewhere
the agents cannot change; `oah eval run --expect-sha256 HASH` checks against it.

`oah eval run` refuses (`OAH_EVAL_PREREG_MISMATCH`) when a case folder or a variant script changed after
preregistration. Every result records the preregistration hash.

## Isolation

Before any variant starts, `oah eval run` builds a separate workspace for every variant of every case and
scans each one. It refuses with `OAH_EVAL_ISOLATION`, naming what it found, when a workspace holds:

- an object of the original history: any object of the bundle other than the snapshot tree's own trees
  and blobs, or the snapshot commit itself for archives;
- borrowed objects (`.git/objects/info/alternates`);
- a copy of the oracle, compared by SHA-256 with LF and CRLF line endings;
- a copy of the snapshot file;
- a file matching a `forbidden` pattern.

The oracle comparison needs the preregistered oracle hash. An exploratory run records `oracle: not-checked`.

**Limit:** the scan checks what each workspace contains. It is not a sandbox. A variant that reads other
paths on the machine, including the oracle's real location, is not stopped. Keep oracles where the agents'
tools are not pointed at them.

## Grading

`oah eval grade` applies the oracle's rules to each variant:

- **Matched:** an expected defect that at least one finding matches by file and keywords.
- **Missed:** an expected defect that no finding matches.
- **Unmatched findings:** listed as `needs-human-judgement`. They are **never** counted as false
  positives automatically.
- **Tests:** each test must fail on a fresh starting snapshot. If it passes there, it is reported as
  `oracle-invalid`. A test that passes on a copy of the variant's final workspace counts as `fixed`.

A human verdict file marks unmatched findings:

```toml
[[verdict]]
case = "case-a"
variant = "baseline"
finding = "F2"
verdict = "false-positive"   # false-positive | valid | duplicate | out-of-scope
note = "why"
```

## The report

`oah eval report` aggregates by split and variant. It shows:

- the counts: matched, missed and unmatched;
- the human verdicts;
- the tests fixed;
- non-zero exits and timeouts;
- rework (rounds and corrections, when recorded);
- wall time and usage.

A total that is only partly recorded reads `unknown (k of n recorded)`. The report then prints its limits:

- the number of cases run;
- the control-flow-only variants;
- every unknown metric;
- the findings awaiting human judgement;
- whether the run was preregistered;
- the scope of the isolation scan.

It never prints a superiority claim.

## Configuration

`.oah/config.toml` may set:

```toml
[eval]
timeout_seconds = 1800        # per variant, when the plan sets none
test_timeout_seconds = 300    # per oracle test, when the oracle sets none
```

Run folders go to `<oah state>/eval/runs/<run id>/`, which is `OAH_STATE_DIR` or `<git common dir>/oah`.
`--runs-dir` overrides this. Each run folder holds:

- `run.json`;
- for each case and variant, `cases/<case>/<variant>/` with `result.json`, `stdout.txt`, `stderr.txt`,
  the final `workspace/` and its `output/`;
- after grading, `grade.json`.

## Codes

Refusals exit 1. A missing tool or an unwritable folder exits 69.

| Code | Meaning |
|---|---|
| `OAH_EVAL_CASE_INVALID` / `OAH_EVAL_PLAN_INVALID` / `OAH_EVAL_ORACLE_INVALID` | a file is malformed; the message names the field |
| `OAH_EVAL_SNAPSHOT` | the snapshot file is unreadable, lacks the commit, or holds unsafe archive members |
| `OAH_EVAL_ISOLATION` | a workspace is not isolated; no variant was started |
| `OAH_EVAL_PREREG_REQUIRED` | a holdout case needs a preregistration |
| `OAH_EVAL_PREREG_HASH` | the preregistration changed after it was frozen, or there is no hash to check |
| `OAH_EVAL_PREREG_MISMATCH` | a case or variant file changed since preregistration |
| `OAH_EVAL_PREREG_EXISTS` | the preregistration file already exists |
| `OAH_EVAL_HOLDOUT_USED` | a case used for development cannot be holdout |
| `OAH_EVAL_ORACLE_MISSING` / `OAH_EVAL_ORACLE_IN_CASE` / `OAH_EVAL_ORACLE_CHANGED` | oracle missing, inside a case folder, or changed since preregistration |
| `OAH_EVAL_CASE_CHANGED` | the case folder changed between run and grade |
| `OAH_EVAL_VERDICT_INVALID` | a human verdict is unknown, duplicated or names no unmatched finding |
| `OAH_EVAL_RUN_INVALID` / `OAH_EVAL_NOT_GRADED` | not a run folder / not graded yet |
| `OAH_EVAL_COMMAND_MISSING` (69) | a variant or test program is not installed |
| `OAH_EVAL_GIT` / `OAH_EVAL_IO` / `OAH_EVAL_NO_STATE` (69) | git failed, a file could not be written, or there is no place for run folders |

## Fake engines

Tests use fake variants, which never call a model. Their results are labelled `control-flow-only`. They
show that the runner, the grader and the report behave correctly. They say nothing about how well a real
engine finds defects.
