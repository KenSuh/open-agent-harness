# Install on Windows

You need Windows 11 (Windows 10 22H2 works on a best-effort basis) and PowerShell. WSL is not
needed. One command does the rest, and it asks before it installs anything.

## 1. Get the code

If you have `git`, open PowerShell, clone the repository and go into it:

```powershell
git clone <repository URL> open-agent-harness
cd open-agent-harness
```

No `git` yet? Download the repository as a ZIP from its page, unzip it, and `cd` into the folder.
The installer offers to install Git for Windows for you.

## 2. Run the installer

```powershell
powershell -ExecutionPolicy Bypass -File .\install\install.ps1
```

`-ExecutionPolicy Bypass` applies to this one run only; no system setting changes. If you prefer,
run `Unblock-File .\install\install.ps1` once and then start it under your normal policy.

To see every step first without changing anything, add `-DryRun`.

The installer goes through five steps.

1. **Find Python 3.11 or newer.** It tries `py -3`, `python` and `python3`. If there is none, it
   offers **uv**, which can download Python for you. It shows the official command
   (`irm https://astral.sh/uv/install.ps1 | iex`) and asks before running it.
2. **Check the tools `oah` uses.** It prints a table of what is found and what is missing, then a
   plan. For each missing tool it shows the **exact official command**, a short note and the
   official page, and asks `Install now? [y/N]`. Only `y` installs it. Anything else skips it.
   After each install it reloads PATH, so the new tool works in the same window.
3. **Install `oah`.** With uv: `uv tool install --force <this folder>`. Without uv it uses `pipx`,
   or `python -m pip install --user` as a last resort.
4. **Check your PATH.** If the folder that holds `oah` is not on your PATH, it offers
   `uv tool update-shell`, which adds that folder to your user PATH setting. It runs only if you
   answer `y`. For pipx or pip it prints a PowerShell command you can run yourself.
5. **Run `oah doctor`.** It lists every tool, and for each missing one the command that installs it.

The last line is `OAH_INSTALL_OK` when everything is ready, or `OAH_INSTALL_INCOMPLETE` when a
required tool still needs your attention. The list above it says what to do.

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
| Node.js | recommended (for unlazy) | `winget install OpenJS.NodeJS.LTS` |
| unlazy | recommended (gates) | `npx skills add Leonxlnx/unlazy` |
| graphify | recommended | `uv tool install graphifyy` |
| gh | recommended | `winget install --id GitHub.cli --source winget` |
| gitleaks | recommended | `scoop install gitleaks` (needs [Scoop](https://scoop.sh)) |
| jq | recommended | `winget install jqlang.jq` |
| ripgrep (`rg`) | recommended | `winget install BurntSushi.ripgrep.MSVC` |

The `irm … | iex` one-liners run in a separate Windows PowerShell process. Aside (browser tasks)
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
