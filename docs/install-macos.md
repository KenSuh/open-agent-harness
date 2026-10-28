# Install on macOS

You need macOS 13 or newer and a Terminal window. One command does the rest, and it asks before it
installs anything.

## 1. Choose how to install

There are two ways. Both run the same installer and ask the same questions.

**From a clone.** You need `git` first. If
`git --version` fails, run `xcode-select --install` and finish the window that opens. Then:

```bash
git clone https://github.com/KenSuh/open-agent-harness.git
cd open-agent-harness
bash install/install.sh
```

**From the v0.1.0 release.** Open the repository’s release page and download the source archive of
that tag and its `SHA256SUMS` file. Use the exact archive named in `SHA256SUMS`; do not substitute a
different archive format. No clone is needed, but git is still one of oah’s required tools.

Before unpacking, compute the archive’s SHA-256 (replace `<downloaded-archive>` with its filename).

```bash
shasum -a 256 "./<downloaded-archive>"
```

Compare the full digest with that file’s entry in the release’s `SHA256SUMS` (hexadecimal letter case
does not matter). **Refuse to unpack or run the installer unless they match.** If the digest differs
or the entry is missing, stop and download the matching archive and checksum file again from the
release page, or use a clone. After verification, unpack it, open a terminal in the extracted source
folder and run the included installer.

```bash
bash install/install.sh
```

The installer source documents this release link pattern:
`https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.sh`.
Find actual published files on the repository’s release page. Checkout copies still contain release-pin
placeholders; the verified source-archive method above does not assume a standalone installer is published.

## 2. What the installer does

To see every step first without changing anything, add `--dry-run`. The installer builds an ordered
plan and only prints it: it does not start tools, probe versions, run guided setup or load profiles.
The plan includes conditional commands because installed tools are checked only during a real run.
A dry run does not verify your manually downloaded archive; complete the checksum step above first.
Run without `--dry-run` to execute the plan with consent. Actual release downloads and all redirects must use HTTPS.

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
| unlazy | recommended (gates) | `oah init --with-unlazy`, at the commit `oah`'s gates are tested with |
| graphify | recommended | `uv tool install graphifyy` |
| gh | recommended | `brew install gh` |
| gitleaks | recommended | `brew install gitleaks` |
| jq | recommended | `brew install jq` |
| ripgrep (`rg`) | recommended | `brew install ripgrep` |

Commands that start with `brew` need [Homebrew](https://brew.sh). Without it, the setup shows the
tool's download page instead. `brew install node` installs the current Node.js release and puts it
on your PATH; unlazy needs Node 16 or newer.

The installer does not install unlazy. `oah init --with-unlazy` does, inside your project: it installs
the commit `oah`'s gates are tested with, and verifies that commit and its scripts digest before any gate
command runs.

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
