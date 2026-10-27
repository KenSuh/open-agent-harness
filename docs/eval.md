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

All named files resolve inside the case folder and have no linked ancestors, including symlinks or
Windows junctions. The same rule applies to `case.toml` and the `inputs/` root. The case digest walks
regular directories without following directory links and hashes every consumed input file.
A bundle may hold the whole history, including the fix, because
only the snapshot commit's tree reaches a workspace. A tar or zip archive must be the snapshot tree. When
`git archive` recorded a commit id in it, that id must equal `commit`. Archives may hold only regular files
and folders. Every archive member is validated before extraction. Git metadata names at any depth,
including case variants, macOS ignored format characters and Windows short-name, trailing-dot/space
and stream aliases, are refused.

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
command = ["{python}", "../inputs/run_baseline.py"]
fake = false                     # true marks a fake engine: results are control-flow-only
timeout_seconds = 1800           # optional; default [eval] timeout_seconds
config = "configs/improved.toml" # optional oah configuration, written to .oah/config.toml in the workspace
```

A variant is a named command, or an `oah` configuration that its command uses. All variants get identical
inputs. The program is resolved through PATH like every `oah` tool, and Windows batch files are never
launched. Put script arguments in the snapshot or case `inputs/`; existing external input files
are refused even when their contents are ordinary. Only the registered executable may be external.

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
Finding IDs must be unique within a variant result after normalization (a missing ID becomes
`F<position>`); duplicate IDs are refused during grading.

Variants receive an explicit runtime environment: `PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`, `COMSPEC`,
`HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TMP`, `TEMP`, `TMPDIR`, `LANG`, `LC_ALL`, `LC_CTYPE`
and `TZ` when present, plus the three `OAH_EVAL_*` workspace variables above. Other caller variables,
including oracle locations, API keys, Git context and `OAH_STATE_DIR`, are not forwarded. Configure
engines through their own local runtime setup.

## Preregistration

`oah eval prereg` writes a frozen JSON and `<file>.sha256`, and prints the hash. The JSON holds:

- the cases, each with its split, a SHA-256 of the case folder, the snapshot commit,
  `used_for_development` and the SHA-256 of its oracle;
- the variants, with their exact commands, the SHA-256 of every file a command names, and any config
  text;
- fingerprints of the supplied and resolved oracle paths, without recording their locations;
- the metrics and the grader rules.

The file is never rewritten. To change anything, preregister again to a new file. Keep the hash somewhere
the agents cannot change; `oah eval run --expect-sha256 HASH` checks against it.

`oah eval run` refuses (`OAH_EVAL_PREREG_MISMATCH`) when a case folder or a variant script changed after
preregistration. Every result records the preregistration hash.
Preregistration also builds and scans every workspace, without launching a variant. Known leaks are
refused before either preregistration file is written. Older preregistrations without path checks must
be recreated.

## Isolation

Before any variant starts, `oah eval run` builds a separate workspace for every variant of every case and
scans each one. It refuses with `OAH_EVAL_ISOLATION`, naming what it found, when a workspace holds:

- an object of the original history: any object of the bundle other than the snapshot tree's own trees
  and blobs, or the snapshot commit itself for archives;
- borrowed objects (`.git/objects/info/alternates`);
- embedded Git repository metadata (including nested clones and bare repositories), bundles, packs
  or loose Git objects in visible inputs or any retained raw Git blob, including overwritten configs
  and unreferenced blobs;
- a copy of any preregistered oracle, compared by SHA-256 with LF/CRLF and UTF-8 BOM normalization,
  in checked-out files **or any raw Git blob retained in the repository**, including unreferenced blobs;
- a copy of the snapshot file;
- a file or link matching a `forbidden` pattern;
- a link leaving its workspace input tree, or a directory link. Committed symlink modes are checked
  even when Windows checks them out as ordinary files. Case input roots, requirement and prompt
  must be regular directories/files.

The exact UTF-8 task prompt sent on stdin is scanned too, including its forbidden filename, and the
checked prompt is retained for launch. Every workspace uses the union of all preregistered oracle
hashes. Construction and scan Git subprocesses discard caller `GIT_*` variables so repository context
cannot redirect their operations.

One default-deny checker is shared by preregistration and run. Its explicit channels are every retained
raw Git blob, every working file (including repository metadata and output files), stdin prompt bytes,
each resolved command argument, each passed environment name/value, injected config text, and literal
files/configs named by command arguments or config string values. Every channel is compared against
all oracle hashes and supplied/resolved oracle path fingerprints. Literal path tokens in arguments,
prompt, config and working files are resolved with `realpath` before comparison; for `NAME=VALUE`,
both the whole token and `VALUE` are checked. An existing path outside the workspace refuses unless it
resolves to the registered variant executable. Unresolvable paths refuse. Allowed runtime environment
paths may be external, but resolved oracle aliases in those values still refuse. Binary channels retain
content/path fingerprint checks; retained blobs also receive history-container checks.

Config injection checks every repository ancestor, replaces snapshot links/reparse points at `.oah`
and `.oah/config.toml`, creates real directories and exclusively creates the file without following
links. It never writes through a snapshot link. The original blobs remain available to the checker, so
config replacement cannot hide oracle data or history containers. Fresh workspace initialization uses
no Git templates, preventing host template hooks from adding external file references.

Command file references use separate arguments or `--option=value`; relative command paths use the
workspace repository. Config file references must be absolute: engine config loaders can resolve
relative paths against different folders, so oah refuses that ambiguity. Referenced workspace files and
the resolved executable, including nested config dependencies, are hashed per case and checked again
at run. Values with a path separator, a suffix or a file/path/include/config option or key are conservatively treated as file references.
Only literal references and TOML/JSON configs can be checked. Dynamic paths, response files, inline
code/module launch switches (including `--eval`, `--command`, `-c`, `-e` and their `=` forms), attached
short options, unsupported config formats, missing/unreadable files, directory inputs and channels
larger than 64 MiB refuse with `OAH_EVAL_CHANNEL_UNCHECKED`.
Explicit `--output`, `--output-dir`, `--marker` and `--record-dir` destinations may be missing. Existing
empty directories inside the workspace are allowed; an existing external destination is refused. A
workspace destination file is checked and frozen like any other file.

The oracle comparison needs the preregistered oracle hash. An exploratory run records `oracle: not-checked`.

**Limit:** the scan checks the enumerated launch channels. It is not a sandbox. A variant that reads other
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

Usage completeness is tracked separately for each field. For example, if both cases record tokens but
only one records cost, token totals remain available and `cost_usd` is `unknown (1 of 2 recorded)`.
Unrecorded input tokens, output tokens and cost stay unknown; missing fields never contribute zero.
Unknown usage fields also appear individually in the report's limits.

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
| `OAH_EVAL_CHANNEL_UNCHECKED` | a launch channel cannot be checked; supply literal readable inputs and TOML/JSON configs, then preregister again |
| `OAH_EVAL_PREREG_REQUIRED` | a holdout case needs a preregistration |
| `OAH_EVAL_PREREG_HASH` | the preregistration changed after it was frozen, or there is no hash to check |
| `OAH_EVAL_PREREG_MISMATCH` | a case or variant file changed since preregistration |
| `OAH_EVAL_PREREG_EXISTS` | the preregistration file already exists |
| `OAH_EVAL_HOLDOUT_USED` | a case used for development cannot be holdout |
| `OAH_EVAL_ORACLE_MISSING` / `OAH_EVAL_ORACLE_IN_CASE` / `OAH_EVAL_ORACLE_CHANGED` | oracle missing, inside a case folder, or changed since preregistration |
| `OAH_EVAL_CASE_CHANGED` | the case folder changed between run and grade |
| `OAH_EVAL_VERDICT_INVALID` | a human verdict is unknown, duplicated or names no unmatched finding |
| `OAH_EVAL_FINDING_DUPLICATE` | finding IDs repeat after normalization; give each finding a unique ID and grade again |
| `OAH_EVAL_RUN_INVALID` / `OAH_EVAL_NOT_GRADED` | not a run folder / not graded yet |
| `OAH_EVAL_COMMAND_MISSING` (69) | a variant or test program is not installed |
| `OAH_EVAL_GIT` / `OAH_EVAL_IO` / `OAH_EVAL_NO_STATE` (69) | git failed, a file could not be written, or there is no place for run folders |

## Fake engines

Tests use fake variants, which never call a model. Their results are labelled `control-flow-only`. They
show that the runner, the grader and the report behave correctly. They say nothing about how well a real
engine finds defects.
