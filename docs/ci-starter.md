# A CI starter for your project

`oah ci init` gives your project **continuous integration (CI)**: every time you push code to GitHub, or
open a pull request, GitHub runs your tests on fresh computers and scans your history for leaked keys.
You see a green check when everything passes and a red cross when something needs fixing.

[한국어](ko/ci-starter.md)

## Set it up

Run this inside your project (any folder of the git repository works):

```sh
oah ci init
```

1. oah looks for your test command (see [How the test command is found](#how-the-test-command-is-found))
   and asks whether to use it. Press Enter to accept it, or answer `n` and type your own.
2. It prints the whole workflow file, `.github/workflows/oah-ci.yml`.
3. It asks `Write .github/workflows/oah-ci.yml? [y/N]`. Nothing is written unless you answer `y`.
4. Commit the file and push it. GitHub starts the checks on that push.

Options:

| Option | What it does |
|---|---|
| `--os macos,windows,linux` | The operating systems to test on. The default is the one you are using now. |
| `--test-command "make test"` | Use this test command instead of the detected one. |
| `--dry-run` | Show the workflow and write nothing. It asks no questions. |
| `--yes` | Accept the detected command and write without asking. |
| `--force` | Together with `--yes`, replace an existing workflow file. |

You can also keep your choices in `.oah/config.toml`, so you do not have to repeat them:

```toml
[ci]
os = ["macos", "windows"]
test_command = "make test"
```

A flag on the command line wins over the config file.

### If the file already exists

oah never replaces your workflow silently. When `.github/workflows/oah-ci.yml` already exists, oah shows
a diff (lines starting with `-` go away, lines starting with `+` are new) and asks before replacing it.
`--yes` alone refuses to replace it; only `--force --yes` replaces it without asking. If the file is
already what oah would write, it says so and changes nothing.

## What the workflow does

The workflow runs on every `push` and every `pull_request`. It has two jobs.

- **test** runs once for each operating system you chose (`macos-latest`, `windows-latest`,
  `ubuntu-latest`). It downloads your code, installs the language tools your project needs (Python,
  Node.js or Go; Rust is already on GitHub's computers), and runs your test command. Each command is its
  own step, so the log shows exactly which one failed.
- **secret scan** downloads [gitleaks](https://github.com/gitleaks/gitleaks), checks that the download
  is exactly the expected file (by its SHA-256 fingerprint) before running it, and scans your **whole
  git history** for things that look like passwords or API keys. It uses `--redact --no-banner`,
  prints no finding context, and writes no report, so adjacent values cannot leak through context.
  See the [pinned version's flags](https://github.com/gitleaks/gitleaks/blob/v8.30.1/README.md#usage).

It is also careful about what it can do:

- `permissions: contents: read`: the workflow can read your code and nothing else.
- Every action it uses (`actions/checkout`, `actions/setup-python` and so on) is pinned to one exact
  commit, so an update upstream cannot change what runs in your project without you editing the file.
- It uses no secrets and no tokens, and it never changes your repository's settings.

## How the test command is found

| Your project has | oah suggests |
|---|---|
| `pyproject.toml`, `setup.cfg`, or a `tests/` folder with Python files, and pytest is configured | `python -m pip install pytest`, then `python -m pytest` |
| the same, without pytest, with `test*.py` files directly in a packaged `tests/` or at the project root | `python -m unittest discover`, with a zero-tests guard |
| the same, without pytest, with `test*.py` files directly in `tests/`, no `tests/__init__.py` and no root tests | `python -m unittest discover -s tests`, with a zero-tests guard |
| the same, without pytest, with root tests and `test*.py` files directly in `tests/` without `tests/__init__.py` | Both discovery commands above, in separate steps, each with its own zero-tests guard |
| `package.json` with a `test` script | `npm ci`, then `npm test` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |

For Python, a `requirements.txt` is installed first, and a `pyproject.toml` with a `[project]` table is
installed with `python -m pip install -e .`. pytest counts as configured when the project has
`pytest.ini`, a `conftest.py`, a `[tool.pytest.ini_options]` table, a `[tool:pytest]` section in
`setup.cfg`, or pytest among its dependencies.

The unittest fallback requires one of those test file layouts. Root discovery keeps relative imports
working in a packaged `tests/` and finds root tests alongside it. When root tests and a non-package
`tests/` folder coexist, separate steps discover from the root and from `tests/`; either step failing
fails the job. Test filenames must be valid Python module names. Each generated step wraps unittest
in a standard-library Python check of its `Ran N tests`
summary: zero tests or a missing summary fails the step, as does any test failure. pytest's no-tests
exit code also fails the step. Add tests or edit the workflow's test command before pushing again.
Other layouts get the failing placeholder; replace it with your test command before pushing.

`npm ci` needs a `package-lock.json`. Run `npm install` once and commit the lock file.

If your project matches several of these, oah lists them and asks you to pick one. If it matches none,
the workflow gets a step called **Run tests (edit me)**. That step fails on purpose with a message, so
you notice it. Open `.github/workflows/oah-ci.yml`, replace the three `run:` lines of that step with the
command that runs your tests, and push again.

## Read the results on GitHub

1. Open your repository on GitHub and click the **Actions** tab. Each push and pull request is one run
   of `oah-ci`.
2. A green check means every job passed. A red cross means at least one failed.
3. Click the run, then the failed job (for example **test (windows-latest)**), then the step with the
   red cross. The log shows the error message from your tests.
4. On a pull request, the same checks are listed at the bottom of the conversation. **Details** opens
   the log.

GitHub's guide: [Using workflow run logs](https://docs.github.com/en/actions/how-tos/monitor-workflows/use-workflow-run-logs).

If **secret scan** fails, a key may have been committed. Deleting it in a new commit is not enough,
because it stays in the history. Revoke or rotate that key with the service that issued it first, then
clean it up. Keep keys in environment variables or your OS keychain, never in a project file.

## Make the checks required

By default GitHub shows the results but still lets anyone merge a pull request whose checks failed. To
block that, make the checks **required** on your default branch (usually `main`). oah never changes
repository settings; you do this yourself, once, on GitHub:

1. Push the workflow and let it run once, so GitHub knows the check names.
2. Open your repository's **Settings**. In the left sidebar, under "Code, planning, and automation",
   click **Rulesets**, then **New ruleset** → **New branch ruleset**.
3. Give it a name and set the enforcement status to **Active**. Under **Target branches**, click
   **Add a target** → **Include default branch**.
4. Turn on **Require status checks to pass before merging**, then add the checks by name:
   `secret scan` and one `test (…)` per operating system, for example `test (macos-latest)`.
5. Click **Create**.

GitHub's guides: [Creating rulesets for a repository](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository)
and the older [branch protection rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule).
Rulesets work in public repositories on every plan, and in private repositories with GitHub Pro, Team
or Enterprise Cloud.

## Change it later

The file is yours: edit it freely. Running `oah ci init` again shows a diff against your edited file and
asks before replacing it, so you can also use it to see what a fresh starter would look like
(`oah ci init --dry-run`).
