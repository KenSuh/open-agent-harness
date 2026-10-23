#!/bin/bash
# open-agent-harness installer for macOS. Two ways to run it:
#
#   bash install/install.sh [--yes] [--dry-run] [--skip-tools]     (from a clone)
#   curl -fsSL https://github.com/KenSuh/open-agent-harness/releases/download/<version>/install.sh | bash -s -- [options]
#                                                                    (no clone, no git needed)
#
# Outside a clone it downloads the pinned release archive below, checks its SHA-256 before
# unpacking it, and continues from there. It then finds Python 3.11+ (offering uv when it is
# missing), runs the guided tool setup (python -m oah.setup, which asks before each install),
# installs oah, checks that a new terminal finds it, and finishes with `oah doctor`.
# It never edits a shell profile without asking first. Written for bash 3.2, the version macOS ships.
# The whole script is one function, so a piped download runs only once it has fully arrived.

# ---- Release pin: the owner fills in the version and digest when a release is published. -----
RELEASE_VERSION='__OAH_RELEASE_VERSION__'  # the release tag
RELEASE_SHA256='__OAH_RELEASE_SHA256__'    # SHA-256 of the .tar.gz below, as published with the release
REPOSITORY='https://github.com/KenSuh/open-agent-harness'
RELEASE_URL="$REPOSITORY/releases/download/$RELEASE_VERSION/open-agent-harness-$RELEASE_VERSION.tar.gz"
# ------------------------------------------------------------------------------------------------

SCRIPT_PATH="${BASH_SOURCE[0]:-}"

main() {
set -u

UV_INSTALL_TEXT='curl -LsSf https://astral.sh/uv/install.sh | sh'
UV_URL='https://docs.astral.sh/uv/getting-started/installation/'
PYTHON_TO_INSTALL='3.13'

YES=0
DRY_RUN=0
SKIP_TOOLS=0

usage() {
  cat <<'EOF'
Usage: bash install/install.sh [--yes] [--dry-run] [--skip-tools]
   or: curl -fsSL <repository>/releases/download/<version>/install.sh | bash -s -- [options]

Installs oah, after checking the tools it uses.
  --yes         install missing tools without asking about each one
  --dry-run     print every step and change nothing
  --skip-tools  skip the tool check and only install oah
  --help        show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --skip-tools) SKIP_TOOLS=1 ;;
    --help|-h) usage; exit 0 ;;
    *)
      echo "OAH_INSTALL_USAGE: unknown option '$arg'. See the options below." >&2
      usage >&2
      exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
step() { say ""; say "== Step $1/5: $2"; }
fail() {
  say ""
  say "$1"
  say "OAH_INSTALL_FAILED"
  exit 1
}

# Print a command the way a user would type it: arguments with spaces in single quotes.
show() {
  shown=""
  for part in "$@"; do
    case "$part" in
      *" "*) shown="$shown '$part'" ;;
      *) shown="$shown $part" ;;
    esac
  done
  printf '%s' "${shown# }"
}

# Execution helpers are reachable only through execute_plan.
run() {
  say "  Running: $(show "$@")"
  UV_NO_MODIFY_PATH=1 "$@" <"$ANSWERS"
}

# Run an official one-liner through /bin/sh, showing its exact text first.
run_text() {
  say "  Running: $1"
  UV_NO_MODIFY_PATH=1 /bin/sh -c "$1" <"$ANSWERS"
}

# Ask only while executing a real plan.
ask() {
  printf '  %s [y/N] ' "$1"
  answer=""
  read -r answer <"$ANSWERS" || answer=""
  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
  esac
  return 1
}

# Like ask, but --yes answers it.
consent() {
  if [ "$YES" = 1 ]; then
    return 0
  fi
  ask "$1"
}

# Probes set PYTHONDONTWRITEBYTECODE: Apple's python3 otherwise writes a cache into ~/Library.
python_ok() {
  PYTHONDONTWRITEBYTECODE=1 "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_uv() {
  UV=$(command -v uv 2>/dev/null) || UV=""
  if [ -n "$UV" ]; then
    return 0
  fi
  for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      UV=$candidate
      return 0
    fi
  done
  return 1
}

# Refresh only this process; never source or edit a profile to find newly installed tools.
refresh_session_path() {
  for folder in "${XDG_BIN_HOME:-$HOME/.local/bin}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    if [ -d "$folder" ]; then
      case ":${PATH:-}:" in
        *":$folder:"*) ;;
        *) PATH="${PATH:+$PATH:}$folder" ;;
      esac
    fi
  done
  export PATH
}

find_python() {
  PY=""
  for name in python3.14 python3.13 python3.12 python3.11 python3 python; do
    candidate=$(command -v "$name" 2>/dev/null) || continue
    # Without the Command Line Tools, /usr/bin/python3 only opens an install dialog.
    if [ "$candidate" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then
      continue
    fi
    if python_ok "$candidate"; then
      PY=$candidate
      return 0
    fi
  done
  if [ -n "$UV" ]; then
    candidate=$("$UV" python find '>=3.11' 2>/dev/null) || candidate=""
    if [ -n "$candidate" ] && python_ok "$candidate"; then
      PY=$candidate
      return 0
    fi
  fi
  return 1
}

is_clone() {
  [ -f "$1/pyproject.toml" ] && [ -d "$1/src/oah" ] && [ -f "$1/install/install.sh" ]
}

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d ' ' -f 1
  else
    sha256sum "$1" | cut -d ' ' -f 1
  fi
}

# Is oah found by a new terminal? Runs the user's shell as a fresh login shell with a clean
# environment, the way a new Terminal window starts.
fresh_shell_finds_oah() {
  user_shell="${SHELL:-/bin/zsh}"
  if [ ! -x "$user_shell" ]; then user_shell=/bin/zsh; fi
  found=$(env -i HOME="$HOME" USER="${USER:-}" LOGNAME="${LOGNAME:-}" SHELL="$user_shell" TERM=dumb \
    PATH=/usr/bin:/bin:/usr/sbin:/sbin "$user_shell" -i -l -c 'command -v oah' </dev/null 2>/dev/null | tail -n 1)
  case "$found" in
    */oah) FRESH_OAH=$found; return 0 ;;
  esac
  FRESH_OAH=""
  return 1
}

# Build data only: no tool execution, prompts, downloads or profile loading.
# Commands with tool names or <...> paths are resolved by the executor after earlier steps.
build_plan() {
  REPO=""
  if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
    case "$SCRIPT_PATH" in
      */*) script_folder=${SCRIPT_PATH%/*} ;;
      *) script_folder=. ;;
    esac
    candidate=$(cd "$script_folder/.." 2>/dev/null && pwd -P) || candidate=""
    if [ -n "$candidate" ] && is_clone "$candidate"; then REPO=$candidate; fi
  fi

  PLAN_ACTIONS=()
  PLAN_TITLES=()
  PLAN_DETAILS=()
  if [ -z "$REPO" ]; then
    url="${OAH_RELEASE_URL:-$RELEASE_URL}"
    digest="${OAH_RELEASE_SHA256:-$RELEASE_SHA256}"
    case "$url$digest" in
      *__OAH_RELEASE_*)
        fail "OAH_INSTALL_NOT_PUBLISHED: no release has been published yet, so there is nothing to download.
Clone the repository instead. It needs git (xcode-select --install installs it):
    git clone $REPOSITORY.git
    cd open-agent-harness
    bash install/install.sh" ;;
    esac
    case "$url" in
      https://?*) ;;
      *) fail "OAH_INSTALL_HTTPS_REQUIRED: use an HTTPS release URL, or run this installer from a clone." ;;
    esac
    PLAN_ACTIONS+=(release)
    PLAN_TITLES+=("== Get the release (not running from a clone)")
    PLAN_DETAILS+=("would create a temporary folder for the release.
would download: $url (HTTPS only, including redirects)
would run: curl --proto '=https' --proto-redir '=https' -fsSL '$url' -o <temporary-folder>/release.tar.gz
would verify SHA-256: $digest (shasum -a 256, or sha256sum; refuse a mismatch before extraction)
would extract: tar -xzf <temporary-folder>/release.tar.gz -C <release-folder>
would remove the temporary folder when finished, including on failure.")
    REPO='<release-folder>'
  fi

  PLAN_ACTIONS+=(python tools install path doctor)
  PLAN_TITLES+=("== Step 1/5: Find Python 3.11 or newer"
    "== Step 2/5: Check the tools oah uses"
    "== Step 3/5: Install oah"
    "== Step 4/5: Check that a new terminal finds oah"
    "== Step 5/5: Check everything with oah doctor")
  PLAN_DETAILS+=("would check: python3.14, python3.13, python3.12, python3.11, python3, python; then uv python find '>=3.11'.
would ask before installing uv if Python and uv are missing (unless --yes).
would run: $UV_INSTALL_TEXT
would set UV_NO_MODIFY_PATH=1 for that installer; it must leave profiles alone.
would ask before downloading Python if needed (unless --yes).
would run: uv python install $PYTHON_TO_INSTALL")
  if [ "$SKIP_TOOLS" = 1 ]; then
    PLAN_DETAILS+=("would refresh only this process's PATH with existing uv directories.
would skip the tool check (--skip-tools). Next: run oah doctor after installing.")
  else
    setup_yes=''
    if [ "$YES" = 1 ]; then setup_yes=' --yes'; fi
    PLAN_DETAILS+=("would refresh only this process's PATH with existing uv directories.
would run: PYTHONPATH='$REPO/src' PYTHONDONTWRITEBYTECODE=1 python -m oah.setup --os macos$setup_yes
would ask before each tool installation (unless --yes); the setup prints its exact official command first.")
  fi
  PLAN_DETAILS+=("would select the first available installer, after tool setup:
would run: uv tool install --force '$REPO'
or run: pipx install --force '$REPO'
or run: python -m pip install --user '$REPO'
would set UV_NO_MODIFY_PATH=1 for installation subprocesses.")
  PLAN_DETAILS+=("would locate oah with uv tool dir --bin, pipx environment --value PIPX_BIN_DIR, or Python's user scripts directory.
would start a new login shell (${SHELL:-/bin/zsh}) with a clean environment and check: command -v oah
would offer: uv tool update-shell only with a separate yes; --yes never edits a profile.
would otherwise print the PATH line to add yourself; then open a new terminal.")
  PLAN_DETAILS+=("would refresh only this process's PATH, locate the installed executable, then run: oah doctor
would report missing tools and sign-ins. Next: open a new terminal and run oah init in your project.")
}

# Print strings only. In particular, do not run tool/version checks or the guided setup here.
print_plan() {
  printf '%s\n' 'open-agent-harness installer (macOS)' 'Dry run: conditional actions only; no tools are started.'
  for ((index=0; index<${#PLAN_ACTIONS[@]}; index++)); do
    printf '\n%s\n' "${PLAN_TITLES[$index]}"
    details=${PLAN_DETAILS[$index]}
    printf '  [dry-run] %s\n' "${details//$'\n'/$'\n'  [dry-run] }"
  done
  printf '\n%s\n' 'Next: run this installer without --dry-run to review prompts and install.' 'OAH_INSTALL_DRY_RUN_OK'
}

# The sole executor consumes the same ordered actions that print_plan displays.
execute_plan() {
  ANSWERS=/dev/stdin
  if [ "$REPO" = '<release-folder>' ]; then
    if (: </dev/tty) 2>/dev/null; then ANSWERS=/dev/tty; else ANSWERS=/dev/null; fi
    REPO=""
  fi
  UV=""
  PY=""
  say "open-agent-harness installer (macOS)"
  if [ "$(uname -s)" != Darwin ]; then
    say "Note: this script is written for macOS. Other systems are not supported, but it will try."
  fi
  for ((index=0; index<${#PLAN_ACTIONS[@]}; index++)); do
    say ""
    say "${PLAN_TITLES[$index]}"
    case "${PLAN_ACTIONS[$index]}" in
      release)
        WORK=$(mktemp -d "${TMPDIR:-/tmp}/oah-install.XXXXXX") || fail "OAH_INSTALL_FAILED: cannot create a temporary folder."
        trap 'rm -rf "$WORK"' EXIT
        say "  Downloading $url"
        curl --proto '=https' --proto-redir '=https' -fsSL "$url" -o "$WORK/release.tar.gz" || fail "The download failed. Check your internet connection and run this again."
        actual=$(sha256_of "$WORK/release.tar.gz")
        wanted=$(printf '%s' "$digest" | tr 'A-F' 'a-f')
        if [ "$actual" != "$wanted" ]; then
          fail "OAH_INSTALL_DIGEST_MISMATCH: the download's SHA-256 is $actual, but the release says $wanted.
Nothing was unpacked or installed. Download again later, or clone the repository instead."
        fi
        say "  SHA-256 verified: $actual"
        mkdir "$WORK/release" && tar -xzf "$WORK/release.tar.gz" -C "$WORK/release" || fail "The release archive could not be unpacked. Clone the repository instead."
        for candidate in "$WORK/release" "$WORK/release"/*; do
          if [ -d "$candidate" ] && is_clone "$candidate"; then
            REPO=$(cd "$candidate" && pwd -P)
            break
          fi
        done
        if [ -z "$REPO" ]; then
          fail "The release archive does not contain open-agent-harness. Report this, or clone the repository instead."
        fi
        say "  Unpacked to a temporary folder; it is removed when this script ends."
        ;;
      python)
        find_uv || true
        if find_python; then
          say "  Found: $PY"
        else
          say "  Python 3.11 or newer was not found."
          if [ -z "$UV" ]; then
            say "  uv can download Python for you. Its official install command is:"
            say "    $UV_INSTALL_TEXT"
            say "  It downloads the official script from astral.sh and runs it with UV_NO_MODIFY_PATH=1."
            say "  To change your shell profile, accept the separate PATH prompt in step 4. More: $UV_URL"
            if consent "Install uv now?"; then
              run_text "$UV_INSTALL_TEXT" || fail "The uv installer did not finish. Read its messages above, then run this script again."
              find_uv || true
            fi
          fi
          if [ -n "$UV" ]; then
            if consent "Download Python $PYTHON_TO_INSTALL with uv?"; then
              run "${UV:-uv}" python install "$PYTHON_TO_INSTALL" || fail "uv could not download Python. Read its messages above, then run this script again."
              find_python || true
            fi
          fi
          if [ -z "$PY" ]; then
            fail "oah needs Python 3.11 or newer. Next step: install uv with
            $UV_INSTALL_TEXT
or install Python from https://www.python.org/downloads/, then run this script again."
          fi
        fi
        ;;
      tools)
        refresh_session_path
        if [ "$SKIP_TOOLS" = 1 ]; then
          say "  Skipped (--skip-tools). Run 'oah doctor' later to see what is missing."
        else
          set -- --os macos
          if [ "$YES" = 1 ]; then set -- "$@" --yes; fi
          say "  The guided setup checks each tool and asks before installing anything."
          if ! PYTHONPATH="$REPO/src" PYTHONDONTWRITEBYTECODE=1 "$PY" -m oah.setup "$@" <"$ANSWERS"; then
            say "  Some tools are not ready yet. The check at the end lists them again."
          fi
          find_uv || true
        fi
        ;;
      install)
        METHOD=""
        PIPX=$(command -v pipx 2>/dev/null) || PIPX=""
        if [ -n "$UV" ]; then
          METHOD=uv
          run "$UV" tool install --force "$REPO" || fail "uv could not install oah. Read its messages above, then run this script again."
        elif [ -n "$PIPX" ]; then
          METHOD=pipx
          run "$PIPX" install --force "$REPO" || fail "pipx could not install oah. Read its messages above, then run this script again."
        elif [ -n "$PY" ]; then
          METHOD=pip
          say "  uv and pipx were not found, so oah is installed for your user with pip."
          run "$PY" -m pip install --user "$REPO" || fail "pip could not install oah. If it mentions an externally managed environment, install uv with
            $UV_INSTALL_TEXT
and run this script again."
        fi
        ;;
      path)
        BIN_DIR=""
        case "$METHOD" in
          uv)
            if [ -n "$UV" ]; then BIN_DIR=$("$UV" tool dir --bin 2>/dev/null) || BIN_DIR=""; fi ;;
          pipx)
            BIN_DIR=$("$PIPX" environment --value PIPX_BIN_DIR 2>/dev/null) || BIN_DIR=""
            if [ -z "$BIN_DIR" ]; then BIN_DIR="$HOME/.local/bin"; fi ;;
          pip)
            BIN_DIR=$(PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sysconfig; print(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))' 2>/dev/null) || BIN_DIR="" ;;
        esac
        if fresh_shell_finds_oah; then
          say "  A new terminal finds oah: $FRESH_OAH"
        else
          say "  A new terminal does not find oah yet. It is installed in ${BIN_DIR:-a folder that is not on PATH}."
          if [ "$METHOD" = uv ]; then
            say "  'uv tool update-shell' adds that folder to your PATH. It edits your shell profile."
            if [ "$YES" = 1 ]; then
              say "  --yes does not cover that, because it edits your shell profile. Run it yourself if you want it:"
              say "    $(show "$UV") tool update-shell"
            elif ask "Run 'uv tool update-shell' now?"; then
              run "$UV" tool update-shell || say "  uv tool update-shell did not finish; you can run it again later."
              if fresh_shell_finds_oah; then say "  A new terminal now finds oah: $FRESH_OAH"; fi
            else
              say "  Skipped. You can run it later: $(show "$UV") tool update-shell"
            fi
          elif [ -n "$BIN_DIR" ]; then
            say "  To fix that, add this line to your shell profile (~/.zprofile for zsh):"
            say "    export PATH=\"$BIN_DIR:\$PATH\""
          fi
          say "  Then open a new terminal window so it takes effect."
        fi
        ;;
      doctor)
        refresh_session_path
        OAH="${BIN_DIR:+$BIN_DIR/}oah"
        if [ ! -x "$OAH" ]; then
          OAH=$(command -v oah 2>/dev/null) || fail "oah was installed, but this script cannot find it. Open a new terminal window and run: oah doctor"
        fi
        if run "$OAH" doctor; then
          say ""
          say "oah is installed and every required tool is ready."
          say "Next: open a new terminal window, go to your project folder and run: oah init"
          say "OAH_INSTALL_OK"
          exit 0
        fi
        say ""
        say "oah is installed, but some required tools are not ready (see the list above)."
        say "Install or sign in to them as shown, run: oah doctor, then in your project folder: oah init"
        say "OAH_INSTALL_INCOMPLETE"
        exit 1
        ;;
      *) fail "Unknown install action. Run an unchanged installer from the repository." ;;
    esac
  done
}

build_plan
if [ "$DRY_RUN" = 1 ]; then
  print_plan
  return 0
fi
execute_plan
}

main "$@"
