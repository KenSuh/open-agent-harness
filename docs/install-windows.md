# Install on Windows

You need Windows 11 (Windows 10 22H2 works on a best-effort basis) and PowerShell. WSL is not
needed. One command does the rest, and it asks before it installs anything.

## 1. Choose how to install

There are two ways. Both run the same installer and ask the same questions.

**From a clone (works now, while the repository is private).** You need Git first. If
`git --version` fails, run `winget install --id Git.Git -e` and open a new PowerShell window. Then:

```powershell
git clone https://github.com/KenSuh/open-agent-harness.git
cd open-agent-harness
powershell -ExecutionPolicy Bypass -File .\install\install.ps1
```

`-ExecutionPolicy Bypass` applies to this one run only; no system setting changes. If you prefer,
run `Unblock-File .\install\install.ps1` once and then start it under your normal policy.

**With one command (once a release is published).** No clone and no Git needed:

```powershell
irm https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.ps1 | iex
```

Replace `<version>` with the release you want, as listed on the releases page
(https://github.com/KenSuh/open-agent-harness/releases).

This form downloads the pinned release archive, checks its SHA-256 fingerprint against the one
published with the release, and stops before unpacking anything if they differ
(`OAH_INSTALL_DIGEST_MISMATCH`). Everything it unpacks goes to a temporary folder that is removed at
the end, and your PowerShell window stays open whatever happens. Until a release is published it
stops with `OAH_INSTALL_NOT_PUBLISHED`; use the clone then. To pass options this way, use
`& ([scriptblock]::Create((irm https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.ps1))) -DryRun`.

## 2. What the installer does

To see every step first without changing anything, add `-DryRun`. The installer builds an ordered
plan and only prints it: it does not start tools, probe versions, run guided setup or refresh PATH.
The plan includes conditional commands because installed tools are checked only during a real run.
A public dry run also prints the release download, SHA-256 check, extraction and cleanup; it creates
no temporary folder and does not download or verify the archive. Run without `-DryRun` to execute
the plan with consent. Actual release downloads and all redirects must use HTTPS.

The installer goes through five steps.

1. **Find Python 3.11 or newer.** It tries `py -3`, `python` and `python3`. If there is none, it
   offers **uv**, which can download Python for you. It shows the official command
   (`irm https://astral.sh/uv/install.ps1 | iex`) and asks before running it. The subprocess gets
   `UV_NO_MODIFY_PATH=1`, so uv leaves profiles and persistent PATH settings alone. Its folder is
   added to this process's PATH so setup can use it immediately. To change your saved PATH,
   accept the separate prompt in step 4.
2. **Check the tools `oah` uses.** It prints a table of what is found and what is missing, then a
   plan. For each missing tool it shows the **exact official command**, a short note and the
   official page, and asks `Install now? [y/N]`. Only `y` installs it. Anything else skips it.
   After each install it reloads PATH, so the new tool works in the same window.
3. **Install `oah`.** With uv: `uv tool install --force <this folder>`. Without uv it uses `pipx`,
   or `python -m pip install --user` as a last resort.
4. **Check that a new PowerShell window finds `oah`.** It looks for `oah` on the PATH a new window
   gets from your settings. If it is not found, it offers `uv tool update-shell`, which adds that
   folder to your user PATH setting. It runs only if you answer `y`. For pipx or pip it prints a
   PowerShell command you can run yourself.
5. **Run `oah doctor`.** It lists every tool, and for each missing one the command that installs it.

The last line is `OAH_INSTALL_OK` when everything is ready, or `OAH_INSTALL_INCOMPLETE` when a
required tool still needs your attention. The list above it says what to do. Then open a new
PowerShell window, go to your project folder and run `oah init`.

### Options

| Option | What it does |
|---|---|
| `-Yes` | Installs every missing tool without asking about each one. It still never changes your PATH setting. |
| `-DryRun` | Prints every step and changes nothing. |
| `-SkipTools` | Skips step 2 and only installs `oah`. |

## 3. Sign in to Claude Code and Codex

During step 2 the setup checks whether you are signed in. If you are not, it tells you:

1. Open a **new** PowerShell window.
2. Run the command it shows: `claude auth login` for Claude Code, `codex login` for Codex.
3. Finish the sign-in in your browser, then come back and press Enter.

It then checks again with each tool's own status command (`claude auth status`,
`codex login status`). Your password and keys stay with those tools. You can skip with `s` and
sign in later; `oah doctor` checks it any time.

Claude Code needs a Pro, Max, Team, Enterprise or Console account. Codex accepts a ChatGPT plan or
an API key.

## The tools

| Tool | Needed? | Official command on Windows |
|---|---|---|
| Git | required | `winget install --id Git.Git -e --source winget` |
| Python 3.11+ | required | `uv python install 3.13` or `winget install Python.Python.3.13` |
| Claude Code | required | `irm https://claude.ai/install.ps1 \| iex` or `winget install Anthropic.ClaudeCode` |
| Codex CLI | required | `irm https://chatgpt.com/codex/install.ps1 \| iex` |
| uv | recommended | `irm https://astral.sh/uv/install.ps1 \| iex` |
| Node.js | recommended; required in a project that uses gates | `winget install OpenJS.NodeJS.LTS` |
| unlazy | recommended (gates) | `oah init --with-unlazy`, at the commit `oah`'s gates are tested with |
| graphify | recommended | `uv tool install graphifyy` |
| gh | recommended | `winget install --id GitHub.cli --source winget` |
| gitleaks | recommended | `scoop install gitleaks` (needs [Scoop](https://scoop.sh)) |
| jq | recommended | `winget install jqlang.jq` |
| ripgrep (`rg`) | recommended | `winget install BurntSushi.ripgrep.MSVC` |

The `irm … | iex` one-liners run in a separate Windows PowerShell process.

The installer does not install unlazy. `oah init --with-unlazy` does, inside your project: it installs
the commit `oah`'s gates are tested with, and verifies that commit and its scripts digest before any gate
command runs.

`oah doctor` counts Node as required once your project uses gates: when it has
`.oah/tasks/*/GATES.md` files, or a `[gates]` section in `.oah/config.toml` that does not say
`enabled = false`. Before that it is only recommended. Aside (browser tasks)
and Orca (a desktop app) are optional and install from their own download pages.

## When something goes wrong

- **`oah` is not recognized after installing.** Open a new PowerShell window. If it still fails,
  run `uv tool update-shell` (or the command the installer printed).
- **`winget` is not recognized.** Install "App Installer" from the Microsoft Store, then run the
  installer again.
- **"running scripts is disabled on this system".** Start the installer exactly as shown above,
  with `-ExecutionPolicy Bypass`.
- **A tool's installer failed.** Read its message, fix the cause, and run the installer again.
  Tools that are already installed are not touched.
- **Check everything again:** `oah doctor`.
