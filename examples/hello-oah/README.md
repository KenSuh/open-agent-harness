# hello-oah: your first task with oah

Ask your AI to make a small change, then follow what it does with oah. Claude Code builds, Codex CLI reviews
and corrects, and the gates and wiki keep the evidence. You ask, answer when it stops for a decision, and
confirm before the change lands. It takes about 20 minutes.

[한국어](README.ko.md)

The project is tiny on purpose. `hello.py` prints `Hello, world!`, and `tests/test_hello.py` tests it. The task
teaches it to greet a name: `python hello.py Ada` should print `Hello, Ada!`.

Tests in this repository (`tests/test_example_hello.py`) run the real Python CHECK commands and exercise the
illustrative changes-requested path below with stand-ins for Claude Code, Codex and unlazy. The offline
walkthrough uses a local remote and stops before landing; it does not verify GitHub CI or live engine output.

## Walk through the conversation

Run the installer once and `oah init` once per project. Then open your AI in the project and speak in plain
words. The installed rules and each command’s next-step message guide it through the harness. You ask, answer
when it stops for a decision, and confirm before anything lands.

The steps below follow the same order as the command walkthrough. For this example, start by asking your
AI to prepare the practice copy; step 2 installs the project rules.

### 1. Make a practice copy

- **You say:** “Copy hello-oah into a separate project so I can try it.”
- **The AI does:** Copies the folder and prepares git and a remote. There is no `oah` command at this step.
- **You check or answer:** Choose a local practice remote or a GitHub repository. Local practice stops before
  landing; the GitHub route needs CI and the tools listed below.

### 2. Set up the project once

- **You do:** Run `oah init` in a terminal, answer its questions and confirm the plan. It installs the rules,
  wiki and templates and builds the first impact map.
- **The AI does:** After setup, adds the bytecode ignore entries shown below and commits the setup.
- **You check or answer:** Choose unlazy for the checks and the CI starter if you want to land on GitHub.
  Confirm the complete setup plan and finish any missing sign-ins. This small example leaves Jev off;
  without a key, rules decide the difficulty and model.

### 3. Describe the result

- **You say:** “Use oah to make hello.py greet a name. Ada should get ‘Hello, Ada!’; with no name, keep
  ‘Hello, world!’.”
- **The AI does:** Runs `oah packet new`, writes the goal, scope and success conditions, and prepares the checks
  with `oah gates new`. It validates them with `oah gates lint` and `oah packet check`.
- **You check or answer:** Confirm the intended behavior, the two checks and the scope of `hello.py` and `tests`.
  Answer if the AI needs a choice clarified.

### 4. Check the starting point

- **You say:** “Is the starting point ready?”
- **The AI does:** Runs `oah preflight` to check the starting point and refresh the impact map, then
  `oah start --packet` to reserve the paths the task changes. An overlapping lease is refused.
- **You check or answer:** Read any warning or refusal and settle a scope conflict before continuing.

### 5. Build the change

- **You say:** “Build it with that scope.”
- **The AI does:** Runs `oah call claude --stage initial` with the accepted task, model and skills. Claude Code
  changes the program and adds the test.
- **You check or answer:** Look at the result and the diff. A name should work and the original greeting should
  still work without one.

### 6. Ask for the evidence

- **You say:** “Did all the gates pass? Is the wiki up to date?”
- **The AI does:** Runs `oah gates check` to show the check commands, maintains the wiki with `oah wiki maintain`,
  then runs `oah gates check --approve` after approval and commits the result with its authoring engine recorded.
- **You check or answer:** Read and approve the check commands before they run. Then inspect the recorded
  results and the wiki entry, not just a claim that the task is done.

### 7. Review, correct and answer decisions

- **You say:** “Is the review done? Fix any blocking findings and review again.”
- **The AI does:** Runs `oah review`. If corrections are requested, it runs `oah fix`, maintains the wiki,
  rechecks the gates, commits the inspected correction, runs `oah fix --bind` and reviews again.
- **You check or answer:** Inspect corrections before they are committed. A repeated problem or a design choice
  stops the loop for your decision; answer so the AI can resume with `oah review --decision`. A converged review
  can still have follow-ups, and Codex reviewing its own correction is labelled non-independent.

### 8. Close the task

- **You say:** “Show me how the result meets the goal, then finish the task.”
- **The AI does:** Uses `oah intent show` to prepare the return, then runs `oah finish`. It checks scope, gates,
  wiki upkeep and the return before closing the lease.
- **You check or answer:** Check each success condition and any remaining follow-up. Finishing closes the task;
  landing comes next.

### 9. Confirm before landing

- **You say:** “Is this version ready to land?” Then, after inspecting the checks, “Land this version.”
- **The AI does:** Pushes the branch, prepares the pull request with `oah review summary`, and runs
  `oah land --check`. After your confirmation, `oah land --merge` rechecks and lands the reviewed commit.
- **You check or answer:** Inspect the exact version, review, gates, secret scan and CI results. Give the final
  confirmation only for the result you want to land; a refusal still needs resolving.

**Planned for v0.4.0:** before implementation, you will say what you want to build and oah will ask one design
question at a time with a recommendation. That guided design conversation is not available yet. Today you can
already discuss the goal with your AI and answer decisions when the review loop stops.

## What runs underneath

After you run `oah init` in a terminal, the installed rules and each command’s next-step message guide your AI
through the commands below. You can also run this complete walkthrough yourself to see each result. Every `sh`
block is kept in execution order; the review section shows which commands depend on its actual verdict.
When your AI makes these commits it records its own engine; the walkthrough uses `Agent-Engine: human` because
you run it by hand.

### Before you start

- `oah` is installed: `oah --version` prints a version. If not, follow
  [Install on macOS](../../docs/install-macos.md) or [Install on Windows](../../docs/install-windows.md).
- Claude Code and Codex CLI are installed and signed in. `oah doctor` checks both.
- For the gates: Node.js, and unlazy, which `oah init` offers to install.
- For landing: gitleaks, and for the GitHub route, gh signed in with `gh auth login`. Missing gitleaks stops
  the scan; missing gh leaves CI unverified and blocks landing by default.

Every command below runs in a terminal. macOS lines work in zsh or bash. Where Windows PowerShell needs a
different line, it is shown next to it.

### 1. Make your own copy

Copy this folder out of the `oah` checkout and make it a git repository of its own:

```sh
cp -R examples/hello-oah ~/hello-oah
cd ~/hello-oah
git init -b main
git add -A
git commit -m "start hello-oah" -m "Agent-Engine: human"
```

On Windows PowerShell, the first two lines are
`Copy-Item -Recurse examples\hello-oah $HOME\hello-oah` and `cd $HOME\hello-oah`.

For the landing walkthrough, create an empty GitHub repository and push the initial branch. From this
folder, with gh signed in, run (choose another repository name if `hello-oah` already exists in your account):

```sh
gh repo create hello-oah --private --source . --remote origin
git push -u origin main
```

Alternatively, to practise locally through step 8, use a bare repository on your own disk:

```sh
git init --bare -b main ~/hello-remote.git
git remote add origin ~/hello-remote.git
git push -u origin main
```

On Windows PowerShell, write `$HOME\hello-remote.git` instead of `~/hello-remote.git`. The remote must be a URL
or an absolute path: landing refuses a relative path such as `../hello-remote.git`.
The local alternative cannot complete step 9 with gh installed: gh cannot query GitHub CI for a disk path,
so landing refuses with `OAH_LAND_CI_PENDING`. Use the GitHub route for landing. Without gh, CI is
`unverified` and blocks by default with `OAH_LAND_CI_UNVERIFIED`; removing gh is not a CI verification procedure.

### 2. Set up oah in the project

```sh
oah init
```

`oah init` first asks you to select options: tier agents, hooks, unlazy, the knowledge
graph, the CI starter, the companion apps Orca and Aside, the TypeSafe Jev re-ranker and skill updates. For
this example, say yes to unlazy. For GitHub landing in step 9, also say yes to the CI starter and confirm
its Python test command; for local practice through step 8, say no to CI. Say no to the other options.
You can add them later. Then inspect and confirm the complete plan before changes are applied.
It installs nothing you did not confirm, and it checks your Claude Code and Codex
sign-ins with their own status commands.

It writes `.oah/config.toml`, the rules block in `AGENTS.md` (which `CLAUDE.md` imports), the wiki in
`docs/wiki/`, templates in `.oah/templates/` and `.gitignore` entries, then builds the first impact map. The
last line is `OAH_INIT_DONE`, or `OAH_INIT_INCOMPLETE` with what is still to do.

Before running gates, append these Python bytecode patterns to `.gitignore`. Init does not add them, and
the unit-test CHECK creates bytecode; non-ignored new files would invalidate the gate inputs.

```gitignore
__pycache__/
*.py[cod]
```

Commit the result:

```sh
git add -A
git commit -m "set up oah" -m "Agent-Engine: human"
git push
```

### 3. Describe the task

Work on a branch, then create the task's packet. The packet holds the intent: the problem, the outcome
you want and the success conditions.

```sh
git switch -c hello-name
oah packet new hello-name --scope hello.py --scope tests
```

```text
OAH_PACKET_CREATED .oah/tasks/hello-name/PACKET.md
plan: initial → claude → medium; review → codex → medium; correction → codex → medium
```

The plan line shows the engine and tier for each stage, scored from the task's difficulty. Open
`.oah/tasks/hello-name/PACKET.md` and fill in the intent:

```markdown
Status: accepted
### Problem
`python hello.py` always prints `Hello, world!`; it cannot greet a person by name.
### Desired outcome
`python hello.py Ada` prints `Hello, Ada!`, and without a name it still prints `Hello, world!`.
### Success conditions
- AC1: `python hello.py Ada` prints `Hello, Ada!`.
- AC2: the unit tests pass, including a test for a name.
```

`Status: accepted` replaces the whole `Status: draft …` line. Leave the comment lines and the other sections
as they are.

Now write the gates, the checks that prove each success condition:

```sh
oah gates new hello-name
```

Replace the examples in `.oah/tasks/hello-name/GATES.md` with:

```markdown
# Gates: hello-name

- [ ] G1: `python hello.py Ada` prints `Hello, Ada!`
  CHECK: python hello.py Ada
  EXPECT: Hello, Ada!
  EVIDENCE: pending

- [ ] G2: the unit tests pass
  CHECK: python -m unittest discover -s tests
  EXPECT: /^OK/m
  EVIDENCE: pending
```

`python` must start Python 3 in your terminal. If it does not, write the command that does. Check both files,
then commit the packet:

```sh
oah gates lint hello-name
oah packet check hello-name
git add -A
git commit -m "packet for hello-name" -m "Agent-Engine: human"
```

```text
OAH_PACKET_OK .oah/tasks/hello-name/PACKET.md intent=hello-name@r1 status=accepted acs=2 scope=2 warnings=0
```

`GATES.md` stays out of git (`oah init` ignores gate ledgers); the summary that `oah gates check` writes
later is committed instead.

### 4. Check the start and take the lease

```sh
oah preflight --task hello-name
oah start --task hello-name --agent claude --packet .oah/tasks/hello-name/PACKET.md
```

```text
OAH_PREFLIGHT verdict=PROCEED aborts=0 warnings=0
OAH_LEASE_STARTED task=hello-name agent=claude scope=hello.py,tests expires=… intent=hello-name@r1 sha256:…
```

Preflight checks that your branch is up to date, the tree is clean and no other lease holds these paths. The
lease reserves `hello.py` and `tests` for this task and binds the intent to it; an overlapping lease is refused.

### 5. Claude implements

```sh
oah call claude --stage initial --task hello-name
```

This runs Claude Code once, without a chat window, with a brief built from the packet: the intent, the
scope, the selected skills and the tier's model and effort. You can instead open Claude Code in this folder
and ask it to do the task in `.oah/tasks/hello-name/PACKET.md`; the rules block tells it how to work.

When it is done, look at the change with `git diff`. `greet` should take a name, `main` should pass the first
argument, and the tests should have a case for a name.

### 6. Prove it with the gates, then commit

```sh
oah gates check hello-name
```

The first run prints each CHECK command and runs nothing: you approve them first. Read them, then run wiki
maintenance before the final gate check, because it changes tracked files that are gate inputs:

```sh
oah wiki maintain --task hello-name
oah gates check hello-name --approve
```

```text
OAH_WIKI_MAINTAINED task=hello-name log=added index=added rollover=noop record=…
OAH_GATES_CHECKED task=hello-name run=2 attest=0 carry=0
```

The gates are now ticked with evidence, `GATES.summary.md` records them, and the wiki log has an entry for the
task. Commit everything with the engine that wrote it:

```sh
git add -A
git commit -m "greet a name" -m "Agent-Engine: claude"
```

### 7. Codex reviews and corrects

```sh
oah review --task hello-name
```

Codex reviews a clean copy of your committed work. Follow the outcome it actually reports:

- `OAH_REVIEW_CONVERGED`: approval or only P2/P3 follow-ups remain. Skip the correction commands below
  and go to step 8; record any follow-ups for later work.
- `OAH_REVIEW_CHANGES_REQUESTED`: correct the reported P0/P1 findings with the commands below.
- `OAH_REVIEW_NEEDS_DECISION`: decide how to proceed, then resume with
  `oah review --task hello-name --decision "<your decision>"` and follow its new outcome.

The empty-name finding below is illustrative, and is the case used by the fake reviewer in the test.
Claude may already handle empty names, so a live review need not report it or require any correction:

```text
OAH_REVIEW_CHANGES_REQUESTED: R1 of task hello-name requested changes (P1×1). Correct them (oah fix --task hello-name runs Codex for that), commit, then run oah review --task hello-name again
R1-F1 P1 quality hello.py:7 — an empty name prints 'Hello, !'
```

Only when changes are requested, let Codex correct the actual findings:

```sh
oah fix --task hello-name
```

`oah fix` prints the diff Codex made and leaves it uncommitted for you to inspect. Here `greet` falls back to
`world` for an empty name. Run wiki upkeep and then the gates again, commit the correction as Codex's work, and
bind the commit to the correction:

```sh
oah wiki maintain --task hello-name
oah gates check hello-name --approve
git add -A
git commit -m "fall back to world for an empty name" -m "Agent-Engine: codex"
oah fix --task hello-name --bind
oah review --task hello-name
```

```text
OAH_FIX_BOUND: the committed correction is bound; run oah review --task <task> for the next round.
OAH_REVIEW_CONVERGED: R2 of task hello-name the series converged at head …
independent: no — codex wrote commits in base..head, so this is not an independent review (an honest label for this engine order, not an error)
```

This illustrative review converged. There is no fixed number of rounds: follow each new outcome using the
rules above. Running `oah fix` after convergence refuses with `OAH_FIX_NOT_REQUESTED`; go to step 8 instead.

### 8. Finish the task

The return is your short report: one `INTENT` line and one line per success condition. Print the `INTENT` line:

```sh
oah intent show hello-name
```

Save a file next to the project folder, `~/hello-name-return.txt`, with the `INTENT` line that command printed
and one line per success condition:

```text
INTENT hello-name@r1 sha256:<the digest oah intent show printed>
AC1: met — gate G1
AC2: met — gate G2
```

Then finish:

```sh
oah finish --task hello-name --agent claude --summary "greet a name" --return ../hello-name-return.txt
```

```text
OAH_LEASE_FINISHED task=hello-name agent=claude changed=… gates=met intent=hello-name@r1
```

`finish` checks that every change is inside the scope, the gates are met, the wiki upkeep ran and the return
matches the intent. Then it closes the lease.

### 9. Open the pull request and land

```sh
git push -u origin hello-name
oah review summary --task hello-name
```

The summary is a block for the pull request description: the rounds, their verdicts and the gates. With gh
installed you can open the pull request with `gh pr create --fill` and paste the block in.

If you enabled the CI starter in step 2, wait for CI on the exact reviewed head, then check and land it.
If you disabled CI, this new repository has no workflow: waiting alone will not produce results, even
with gh installed, and landing blocks with `OAH_LAND_CI_UNVERIFIED`. To get verified CI, return to step 2
and enable the starter before starting the task; adding it after review changes the head and needs gates
and a new review. An explicit unverified-CI exception is described below.

With CI results available, run:

```sh
oah land --check --task hello-name
oah land --merge --task hello-name
```

`oah land --check` lists one line per check: lease, intent, review, gates, impact, required checks,
fast-forward, scan and CI. `--merge` runs them all again, asks you to confirm by typing `y`, and only then
fast-forwards the remote default branch to exactly your reviewed head. GitHub then shows the pull request as
merged.

Without gitleaks the scan line fails with `OAH_LAND_SCAN_REQUIRED` and nothing is pushed. Without gh, the CI
line says `unverified` and blocks with `OAH_LAND_CI_UNVERIFIED`; no results or a missing required check
also blocks. If you accept that risk, add `--accept-unverified-ci "<reason>"` to both commands above.
The exception and reason are recorded for that exact head; a new head needs its own exception. CI
stays `unverified`, never a pass, and failed or pending CI still blocks. Use a non-secret reason.
See [Landing](../../docs/concepts.md#landing) for the target branch’s CI policy.

## What you did

| Step | Command | What it proved |
|---|---|---|
| Intent | `oah packet new`, `oah packet check` | The goal and its success conditions are written down and accepted. |
| Lease | `oah preflight`, `oah start` | You started on a fresh branch and reserved the task paths; overlapping leases are refused. |
| Implementation | `oah call claude --stage initial` | Claude did the first implementation from the packet. |
| Gates | `oah gates check --approve` | Each success condition has a command and recorded evidence. |
| Review | `oah review`, `oah fix`, `oah review` | Codex reviewed, corrected, and reviewed until it converged. |
| Finish | `oah finish` | Changes stayed in scope, gates are met, the return matches the intent. |
| Landing | `oah land --check`, `--merge` | The exact reviewed head lands, after you confirm. |

Next: read [Concepts](../../docs/concepts.md) for why each step exists, and
[Troubleshooting](../../docs/troubleshooting.md) when a command refuses.
