# Install on macOS

You need macOS 13 or newer and a Terminal window. One command does the rest, and it asks before it
installs anything.

## 1. Choose how to install

There are two ways. Both run the same installer and ask the same questions.

**From a clone (works now, while the repository is private).** You need `git` first. If
`git --version` fails, run `xcode-select --install` and finish the window that opens. Then:

```bash
git clone https://github.com/KenSuh/open-agent-harness.git
cd open-agent-harness
bash install/install.sh
```

**With one command (once a release is published).** No clone and no `git` needed:

```bash
curl -fsSL https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.sh | bash
```

Replace `<version>` with the release you want, as listed on the releases page
(https://github.com/KenSuh/open-agent-harness/releases).

This form downloads the pinned release archive, checks its SHA-256 fingerprint against the one
published with the release, and stops before unpacking anything if they differ
(`OAH_INSTALL_DIGEST_MISMATCH`). Everything it unpacks goes to a temporary folder that is removed at
the end. Until a release is published it stops with `OAH_INSTALL_NOT_PUBLISHED`; use the clone then.
To pass options this way, write them after `bash -s --`, for example
`curl -fsSL https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.sh | bash -s -- --dry-run`.

## 2. What the installer does

To see every step first without changing anything, add `--dry-run`. The installer builds an ordered
plan and only prints it: it does not start tools, probe versions, run guided setup or load profiles.
The plan includes conditional commands because installed tools are checked only during a real run.
A public dry run also prints the release download, SHA-256 check, extraction and cleanup; it creates
no temporary folder and does not download or verify the archive. Run without `--dry-run` to execute
the plan with consent. Actual release downloads and all redirects must use HTTPS.

The installer goes through five steps.

1. **Find Python 3.11 or newer.** If there is none, it offers **uv**, which can download Python for
   you. It shows the official command (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and asks
   before running it. The subprocess gets `UV_NO_MODIFY_PATH=1`, so uv leaves shell profiles and
   persistent PATH settings alone. To change those, accept the separate prompt in step 4.
2. **Check the tools `oah` uses.** It prints a table of what is found and what is missing, then a
   plan. For each missing tool it shows the **exact official command**, a short note and the
   official page, and asks `Install now? [y/N]`. Only `y` installs it. Anything else skips it.
   Installers that need your password ask you for it themselves; `oah` never sees it.
3. **Install `oah`.** With uv: `uv tool install --force <this folder>`. Without uv it uses `pipx`,
   or `python -m pip install --user` as a last resort.
4. **Check that a new terminal finds `oah`.** It starts your shell the way a new Terminal window
   does and looks for `oah`. If it is not found, it offers `uv tool update-shell`, which edits your
   shell profile. It runs only if you answer `y`. For pipx or pip it prints the one line to add to
   `~/.zprofile` instead.
5. **Run `oah doctor`.** It lists every tool, and for each missing one the command that installs it.

The last line is `OAH_INSTALL_OK` when everything is ready, or `OAH_INSTALL_INCOMPLETE` when a
required tool still needs your attention. The list above it says what to do. Then open a new
Terminal window, go to your project folder and run `oah init`.

### Options

| Option | What it does |
|---|---|
| `--yes` | Installs every missing tool without asking about each one. It still never edits your shell profile. |
| `--dry-run` | Prints every step and changes nothing. |
| `--skip-tools` | Skips step 2 and only installs `oah`. |

## 3. Sign in to Claude Code and Codex

During step 2 the setup checks whether you are signed in. If you are not, it tells you:

1. Open a **new** Terminal window.
2. Run the command it shows: `claude auth login` for Claude Code, `codex login` for Codex.
3. Finish the sign-in in your browser, then come back and press Enter.

It then checks again with each tool's own status command (`claude auth status`,
`codex login status`). Your password and keys stay with those tools. You can skip with `s` and
sign in later; `oah doctor` checks it any time.

Claude Code needs a Pro, Max, Team, Enterprise or Console account. Codex accepts a ChatGPT plan or
an API key.

## The tools

| Tool | Needed? | Official command on macOS |
|---|---|---|
| git | required | `brew install git` or `xcode-select --install` |
| Python 3.11+ | required | `uv python install 3.13` or `brew install python@3.13` |
| Claude Code | required | `curl -fsSL https://claude.ai/install.sh \| bash` |
| Codex CLI | required | `brew install --cask codex` or `npm install -g @openai/codex` |
| uv | recommended | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | recommended; required in a project that uses gates | `brew install node` |
| unlazy | recommended (gates) | After lane P5 lands: `oah init --with-unlazy` at P5's contract-tested pin |
| graphify | recommended | `uv tool install graphifyy` |
| gh | recommended | `brew install gh` |
| gitleaks | recommended | `brew install gitleaks` |
| jq | recommended | `brew install jq` |
| ripgrep (`rg`) | recommended | `brew install ripgrep` |

Commands that start with `brew` need [Homebrew](https://brew.sh). Without it, the setup shows the
tool's download page instead. `brew install node` installs the current Node.js release and puts it
on your PATH; unlazy needs Node 16 or newer.

Guided setup does not install unlazy in this lane. After P5 lands, use `oah init --with-unlazy`;
it will verify the contract-tested commit and scripts digest before running gate commands.

`oah doctor` counts Node as required once your project uses gates: when it has
`.oah/tasks/*/GATES.md` files, or a `[gates]` section in `.oah/config.toml` that does not say
`enabled = false`. Before that it is only recommended.

Aside (browser tasks) and Orca (a desktop app) are optional and install from their own download
pages.

## When something goes wrong

- **`oah: command not found` after installing.** Open a new Terminal window. If it still fails, run
  `uv tool update-shell` (or add the line the installer printed to `~/.zprofile`).
- **pip says "externally managed environment".** Install uv with the command above and run the
  installer again.
- **A tool's installer failed.** Read its message, fix the cause, and run `bash install/install.sh`
  again. Tools that are already installed are not touched.
- **Check everything again:** `oah doctor`.
