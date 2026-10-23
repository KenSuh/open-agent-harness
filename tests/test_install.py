# 설치 스크립트 검사 — 임시 클론과 가짜 도구로 install.sh 의 단계·대체 경로·드라이런 무변경을, install.ps1 의 ASCII·CRLF·드라이런을 확인한다.
from __future__ import annotations

import base64
import hashlib
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import unittest
import zipfile
from unittest import mock

from oah.setup import catalog, display
from tests.helpers import make_tmp_dir, make_unlazy, write_posix_tool

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install" / "install.sh"
INSTALL_PS1 = ROOT / "install" / "install.ps1"
BASH = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")  # /bin/bash is 3.2 on macOS
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# A macOS login shell also gets these from path_helper; an oah there would be found by any new terminal.
SYSTEM_OAH = any(os.path.exists(f"{folder}/oah") for folder in ("/usr/local/bin", "/opt/homebrew/bin"))


def make_clone(test: unittest.TestCase) -> pathlib.Path:
    """A copy of the parts of this checkout that the installers use."""
    clone = make_tmp_dir(test) / "clone dir"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "install", clone / "install", ignore=ignore)
    shutil.copytree(ROOT / "src", clone / "src", ignore=ignore)
    shutil.copy2(ROOT / "pyproject.toml", clone / "pyproject.toml")
    return clone


def make_release(test: unittest.TestCase) -> tuple[pathlib.Path, str]:
    """A release archive like the published one: the clone's files under one top-level folder."""
    clone = make_clone(test)
    archive = make_tmp_dir(test) / "open-agent-harness-1.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(clone, arcname="open-agent-harness-1.0.0")
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def make_zip_release(test: unittest.TestCase) -> tuple[pathlib.Path, str]:
    """The Windows form of the release archive: a .zip with one top-level folder."""
    clone = make_clone(test)
    archive = make_tmp_dir(test) / "open-agent-harness-1.0.0.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(clone.rglob("*")):
            if path.is_file():
                bundle.write(path, "open-agent-harness-1.0.0/" + path.relative_to(clone).as_posix())
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def powershell_command(shell: str, script: str, env: dict) -> subprocess.CompletedProcess:
    """Run PowerShell code given as -EncodedCommand, so no quoting reaches the command line."""
    script = "$ProgressPreference = 'SilentlyContinue'; " + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run([shutil.which(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-EncodedCommand", encoded],
                          env=env, capture_output=True, timeout=300)


def powershell_env(test: unittest.TestCase, **extra: str) -> dict[str, str]:
    """Redirect every installer write location into disposable, snapshottable folders."""
    cache = make_tmp_dir(test)
    env = dict(os.environ, HOME=str(cache / "home"), USERPROFILE=str(cache / "home"),
               APPDATA=str(cache / "Roaming"), LOCALAPPDATA=str(cache / "Local"),
               TEMP=str(cache / "Temp"), TMP=str(cache / "Temp"))
    env.pop("PYTHONPATH", None)
    env.update(extra)
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        pathlib.Path(env[name]).mkdir(parents=True, exist_ok=True)
    return env


def ps_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def ps1_with_overrides(overrides: str) -> str:
    """Load the real installer, replacing only external collaborators for offline process tests."""
    replacement = overrides + "\nfunction Invoke-OahInstall {"
    return (f"$installer = [IO.File]::ReadAllText({ps_literal(str(INSTALL_PS1))}); "
            f"$installer = $installer.Replace('function Invoke-OahInstall {{', {ps_literal(replacement)}); ")


def ps1_release_fixture(archive: pathlib.Path) -> str:
    return ("function Save-Release([Uri]$Uri, [string]$Destination) { "
            f"[IO.File]::Copy({ps_literal(str(archive))}, $Destination) }}\n"
            # No actual tool installs are needed to test release verification.
            "function Find-Python { return $null }\nfunction Find-Uv { return $null }\n"
            "function Ask([string]$Question) { return $false }\n")


def snapshot(folder: pathlib.Path) -> dict[str, tuple[int, str]]:
    found = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if path.is_symlink() or not path.is_file():
            found[relative] = (-1, "dir-or-link")
        else:
            data = path.read_bytes()
            found[relative] = (path.stat().st_mtime_ns, hashlib.sha256(data).hexdigest())
    return found


def powershell_snapshot(env: dict[str, str]) -> dict[str, dict[str, tuple[int, str]]]:
    """Ignore only PowerShell's cache subtrees and their directory ancestors, never all AppData."""
    cache_dirs = ("Microsoft/Windows/PowerShell", "Microsoft/PowerShell")
    found = {}
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        folder = pathlib.Path(env[name])
        allowed = cache_dirs if name in ("APPDATA", "LOCALAPPDATA") else ()
        if name in ("HOME", "USERPROFILE"):
            allowed = tuple(f"AppData/{kind}/{relative}" for kind in ("Local", "Roaming") for relative in cache_dirs)
        contents = {}
        for relative, value in snapshot(folder).items():
            path = folder / relative
            key = relative.casefold()
            if not path.is_symlink() and any(
                    key.startswith(cache.casefold() + "/") or
                    (path.is_dir() and (key == cache.casefold() or cache.casefold().startswith(key + "/")))
                    for cache in allowed):
                continue
            contents[relative] = value
        found[name] = contents
    return found


def assert_dry_run_branches(test: unittest.TestCase, source: str, shell: str) -> None:
    """Fail closed on dry-run statements beyond printing, bookkeeping and the guarded run wrapper."""
    if shell == "bash":
        starts = list(re.finditer(r'(?m)^([ \t]*)(?:if|elif) \[ "\$(?:DRY_RUN|RELEASE_DRY_RUN)" = 1 \]; then([^\n]*)',
                                  source))
        branches = []
        for match in starts:
            if match[2].strip():
                test.assertTrue(match[2].endswith("; fi"), match[0])
                branches.append(match[2][:-4])
            else:
                end = re.search(r"(?m)^" + re.escape(match[1]) + r"(?:else|elif|fi)\b", source[match.end():])
                test.assertIsNotNone(end, match[0])
                branches.append(source[match.end():match.end() + end.start()])
        allowed = {"return 0", "exit 0", "REPO='<release-folder>'", "RELEASE_DRY_RUN=1",
                   'set -- "$@" --dry-run', 'run "$OAH" doctor'}
    else:
        starts = re.findall(r"\b(?:if|elseif) \(\$(?:DryRun|ReleaseDryRun)\) \{", source)
        branches = re.findall(r"(?s)\b(?:if|elseif) \(\$(?:DryRun|ReleaseDryRun)\) \{([^{}]*)\}", source)
        test.assertEqual(len(branches), len(starts), "nested or unrecognized dry-run branch")
        allowed = {"$script:StepCode = 0", "return", "return $true", "return '<release-folder>'", "return 0",
                   "$setupArgs += '--dry-run'", "$null", "Invoke-Step @($Oah, 'doctor')",
                   "Say ('  [dry-run] would run: ' + (Format-Command $Argv))",
                   "Say ('  [dry-run] would run: ' + $Text)",
                   "Say ('  [dry-run] would ask: ' + $Question + ' [y/N]')"}
    test.assertTrue(branches, "no dry-run branches checked")
    for branch in branches:
        for line in branch.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line in allowed:
                continue
            if shell == "bash":
                printed = line.replace('$(show "$@")', "<formatted-argv>")
                test.assertFalse(any(token in printed for token in ("$(", "`", "<(", ">(")), line)
                lexer = shlex.shlex(printed, posix=True, punctuation_chars=";&|<>")
                lexer.whitespace_split = True
                words = list(lexer)
                test.assertTrue(len(words) == 2 and words[0] == "say", line)
            else:
                test.assertIsNotNone(re.fullmatch(r'''Say (?:'(?:[^']|'')*'|"(?:[^"`$]|\$\w+)*")''', line), line)


@unittest.skipIf(os.name == "nt" or BASH is None, "install.sh is the macOS installer")
class InstallShTest(unittest.TestCase):
    def setUp(self):
        self.work = make_tmp_dir(self)
        self.clone = make_clone(self)
        self.home = self.work / "home"
        self.home.mkdir()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.tool_bin = self.work / "uv-tool-bin"
        self.log = self.work / "calls.log"
        self.extra_path: list[str] = []
        self.scratch = self.work / "tmp"
        self.scratch.mkdir()

    # Fake programs. Each logs its argv to calls.log; none installs anything.
    def fake_python(self, *, pip_creates_oah: bool = True):
        user_scripts = ("import sysconfig; print(sysconfig.get_path('scripts', "
                        "sysconfig.get_preferred_scheme('user')))")
        write_posix_tool(self.bin, "python3", f'''
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  echo "python3 $*" >> "{self.log}"
  {"" if pip_creates_oah else "exit 0"}
  dir=$("{sys.executable}" -c "{user_scripts}")
  mkdir -p "$dir" && printf '#!/bin/sh\\necho "oah $*" >> "{self.log}"\\necho DOCTOR_OK\\n' > "$dir/oah" && chmod +x "$dir/oah"
  exit 0
fi
exec "{sys.executable}" "$@"''')

    def fake_uv(self, doctor_code: int = 0):
        write_posix_tool(self.bin, "uv", f'''
echo "uv $*" >> "{self.log}"
case "$1 $2" in
  "tool install")
    mkdir -p "{self.tool_bin}"
    printf '#!/bin/sh\\necho "oah $*" >> "{self.log}"\\necho DOCTOR_RESULT\\nexit {doctor_code}\\n' > "{self.tool_bin}/oah"
    chmod +x "{self.tool_bin}/oah" ;;
  "tool dir") echo "{self.tool_bin}" ;;
  "python find") exit 1 ;;
esac
exit 0''')

    def fake_pipx(self):
        pipx_bin = self.home / ".local" / "bin"
        write_posix_tool(self.bin, "pipx", f'''
echo "pipx $*" >> "{self.log}"
case "$1" in
  install) mkdir -p "{pipx_bin}"; printf '#!/bin/sh\\necho "oah $*" >> "{self.log}"\\necho DOCTOR_OK\\n' > "{pipx_bin}/oah"; chmod +x "{pipx_bin}/oah" ;;
  environment) echo "{pipx_bin}" ;;
esac
exit 0''')

    def fake_every_tool(self):
        """Every catalog tool present and signed in, so the guided setup has nothing to install."""
        for tool in catalog():
            if tool.check:
                write_posix_tool(self.bin, tool.check[0], f'echo "{tool.check[0]} 99.0.0"')
        write_posix_tool(self.bin, "claude", '[ "$1" = auth ] && echo \'{"loggedIn": true}\'; echo "2.1.0"')
        make_unlazy(self.home / ".agents" / "skills" / "unlazy")

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def release_download(self, archive: pathlib.Path) -> str:
        """Serve a local fixture through a fake HTTPS downloader, never a production URL exception."""
        write_posix_tool(self.bin, "curl", f'''
echo "curl $*" >> "{self.log}"
for destination in "$@"; do :; done
cp "{archive}" "$destination"''')
        return "https://releases.example.invalid/" + archive.name

    def env(self, **extra: str) -> dict[str, str]:
        env = {"PATH": os.pathsep.join([str(self.bin), *self.extra_path, SYSTEM_PATH]), "HOME": str(self.home),
               "LANG": "en_US.UTF-8", "TMPDIR": str(self.scratch), "SHELL": "/bin/bash"}
        env.update(extra)
        return env

    def run_sh(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(self.clone / "install" / "install.sh"), *args], env=self.env(),
                              input=stdin.encode("utf-8"), capture_output=True, timeout=120, cwd=str(self.work))

    def run_piped(self, *args: str, url: str | None = None, digest: str | None = None) -> subprocess.CompletedProcess:
        """`curl ... | bash -s -- args`: the script arrives on stdin, outside any clone."""
        extra = {}
        if url is not None:
            extra["OAH_RELEASE_URL"] = url
        if digest is not None:
            extra["OAH_RELEASE_SHA256"] = digest
        return subprocess.run([BASH, "-s", "--", *args], env=self.env(**extra), input=INSTALL_SH.read_bytes(),
                              capture_output=True, timeout=120, cwd=str(self.work))

    def new_terminal_finds(self, folder: pathlib.Path) -> None:
        """A login profile that puts `folder` on PATH, as `uv tool update-shell` would."""
        (self.home / ".profile").write_text(f'export PATH="{folder}:$PATH"\n', encoding="utf-8")

    def text(self, result) -> str:
        return result.stdout.decode("utf-8") + result.stderr.decode("utf-8")

    def last_line(self, result) -> str:
        return result.stdout.decode("utf-8").rstrip().splitlines()[-1]

    def test_install_sh_dry_run_changes_nothing(self):
        self.fake_python()
        before_clone, before_home = snapshot(self.clone), snapshot(self.home)
        result = self.run_sh("--dry-run")
        self.assertEqual(result.returncode, 0, self.text(result))
        self.assertEqual(self.last_line(result), "OAH_INSTALL_DRY_RUN_OK")
        self.assertEqual(snapshot(self.clone), before_clone)
        self.assertEqual(snapshot(self.home), before_home)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.tool_bin.exists())

    def test_install_sh_dry_run_prints_every_step(self):
        self.fake_python()
        self.fake_uv()
        result = self.run_sh("--dry-run")
        text = self.text(result)
        self.assertEqual(result.returncode, 0, text)
        for number in range(1, 6):
            self.assertIn(f"Step {number}/5", text)
        self.assertIn("SETUP_DRY_RUN planned=", text)  # the guided setup ran in its dry-run mode
        self.assertIn(f"[dry-run] would run: {self.bin / 'uv'} tool install --force '{self.clone}'", text)
        self.assertIn(f"[dry-run] would run: {self.tool_bin / 'oah'} doctor", text)
        self.assertNotIn("uv tool install", "\n".join(self.calls()))  # only read-only uv calls happened

    def test_install_sh_dry_run_without_python_shows_bootstrap(self):
        result = self.run_sh("--dry-run")
        text = self.text(result)
        self.assertEqual(result.returncode, 0, text)
        self.assertIn("[dry-run] would run: curl -LsSf https://astral.sh/uv/install.sh | sh", text)
        self.assertIn("[dry-run] would run: uv python install 3.13", text)
        self.assertEqual(self.last_line(result), "OAH_INSTALL_DRY_RUN_OK")
        self.assertEqual(snapshot(self.home), {})

    def test_install_sh_uv_bootstrap_disables_profile_changes(self):
        self.fake_uv()
        uv_template = self.work / "uv-template"
        (self.bin / "uv").rename(uv_template)
        write_posix_tool(self.bin, "curl", f'''cat <<'EOF'
[ "$UV_NO_MODIFY_PATH" = 1 ] || exit 23
printf '%s' "$UV_NO_MODIFY_PATH" > "{self.work / 'uv-env.txt'}"
mkdir -p "$HOME/.local/bin"
cp "{uv_template}" "$HOME/.local/bin/uv"
EOF''')
        result = self.run_sh("--yes", "--skip-tools")
        self.assertTrue((self.work / "uv-env.txt").is_file(), self.text(result))
        self.assertEqual((self.work / "uv-env.txt").read_text(encoding="utf-8"), "1")
        self.assertNotIn("uv tool update-shell", self.calls())

    def check_install_sh_path_refresh(self, *, bootstrapped_uv: bool):
        self.fake_every_tool()
        local_bin = self.home / ".local" / "bin"
        (self.bin / "claude").unlink()
        write_posix_tool(self.bin, "curl", f'''cat <<'EOF'
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/claude" <<'CLAUDE'
#!/bin/sh
printf '%s\\n' '{{"loggedIn": true}}'
CLAUDE
chmod +x "$HOME/.local/bin/claude"
EOF''')
        # Install a real doctor entry point so the final check probes the actual refreshed PATH.
        write_posix_tool(self.bin, "uv", f'''
case "$1 $2" in
  "tool install")
    mkdir -p "{self.tool_bin}"
    printf '#!/bin/sh\\nPYTHONPATH="{self.clone}/src" exec "{sys.executable}" -m oah "$@"\\n' > "{self.tool_bin}/oah"
    chmod +x "{self.tool_bin}/oah" ;;
  "tool dir") echo "{self.tool_bin}" ;;
esac''')
        if bootstrapped_uv:
            # Setup must discover uv even though its bootstrap directory was not on the initial PATH.
            local_bin.mkdir(parents=True)
            (self.bin / "uv").rename(local_bin / "uv")
        else:
            self.assertFalse(local_bin.exists())  # setup's Claude install creates it
        self.fake_python()
        result = self.run_sh("--yes")
        text = self.text(result)
        self.assertEqual(result.returncode, 0, text)
        self.assertNotIn("Install uv", text)
        self.assertNotIn("Running: curl -LsSf https://astral.sh/uv/install.sh", text)
        self.assertIn("DOCTOR_OK", text)
        self.assertEqual(self.last_line(result), "OAH_INSTALL_OK")

    def test_install_sh_refreshes_path_before_setup_with_bootstrapped_uv(self):
        self.check_install_sh_path_refresh(bootstrapped_uv=True)

    def test_install_sh_refreshes_path_before_doctor_with_new_local_bin(self):
        self.check_install_sh_path_refresh(bootstrapped_uv=False)

    def test_install_sh_is_bash_32_compatible(self):
        source = INSTALL_SH.read_text(encoding="utf-8")
        for pattern in (r"declare -A", r"\bmapfile\b", r"\breadarray\b", r"\$\{[A-Za-z_]+,,", r"\$\{[A-Za-z_]+\^\^",
                        r"\|&", r"&>>", r";;&", r"\bcoproc\b", r"\$\{[A-Za-z_]+\[-1\]\}"):
            self.assertIsNone(re.search(pattern, source), pattern)
        self.assertNotIn("\r", source)
        self.assertTrue(source.startswith("#!/bin/bash\n"))
        # Piped downloads run only once complete: everything is inside main(), called on the last line.
        self.assertEqual(source.rstrip().splitlines()[-1], 'main "$@"')
        # Only the release version and its digest are placeholders; the repository path is real.
        self.assertIn("RELEASE_VERSION='__OAH_RELEASE_VERSION__'", source)
        self.assertIn("RELEASE_SHA256='__OAH_RELEASE_SHA256__'", source)
        self.assertIn('RELEASE_URL="$REPOSITORY/releases/download/$RELEASE_VERSION/'
                      'open-agent-harness-$RELEASE_VERSION.tar.gz"', source)
        result = subprocess.run([BASH, "-n", str(INSTALL_SH)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_sh_hands_over_to_guided_setup_and_uses_uv(self):
        self.fake_python()
        self.fake_every_tool()
        self.fake_uv()
        self.new_terminal_finds(self.tool_bin)
        result = self.run_sh("--yes")
        text = self.text(result)
        self.assertEqual(result.returncode, 0, text)
        self.assertIn("Every tool is already installed.", text)
        self.assertIn("SETUP_OK", text)
        self.assertIn(f"uv tool install --force {self.clone}", self.calls())
        self.assertIn("oah doctor", self.calls())
        self.assertIn(f"A new terminal finds oah: {self.tool_bin / 'oah'}", text)
        self.assertNotIn("uv tool update-shell", self.calls())  # a new terminal already finds it
        self.assertEqual(self.last_line(result), "OAH_INSTALL_OK")

    @unittest.skipIf(SYSTEM_OAH, "a system-wide oah is found by every new terminal")
    def test_install_sh_offers_update_shell_only_with_consent(self):
        self.fake_python()
        self.fake_uv()
        result = self.run_sh("--skip-tools", stdin="n\n")
        self.assertEqual(result.returncode, 0, self.text(result))
        self.assertIn("uv tool update-shell", self.text(result))
        self.assertNotIn("uv tool update-shell", self.calls())
        self.log.unlink()
        result = self.run_sh("--skip-tools", "--yes")
        self.assertNotIn("uv tool update-shell", self.calls())  # --yes never edits a shell profile
        self.assertIn("edits your shell profile", self.text(result))
        self.log.unlink()
        result = self.run_sh("--skip-tools", stdin="y\n")
        self.assertIn("uv tool update-shell", self.calls())
        self.assertEqual(self.last_line(result), "OAH_INSTALL_OK")

    @unittest.skipIf(SYSTEM_OAH, "a system-wide oah is found by every new terminal")
    def test_install_sh_falls_back_to_pipx(self):
        self.fake_python()
        self.fake_pipx()
        result = self.run_sh("--skip-tools", stdin="n\n")
        self.assertEqual(result.returncode, 0, self.text(result))
        self.assertIn(f"pipx install --force {self.clone}", self.calls())
        self.assertIn(f'export PATH="{self.home / ".local" / "bin"}:$PATH"', self.text(result))
        self.assertEqual(self.last_line(result), "OAH_INSTALL_OK")

    @unittest.skipIf(SYSTEM_OAH, "a system-wide oah is found by every new terminal")
    def test_install_sh_falls_back_to_pip_user_with_path_note(self):
        self.fake_python()
        result = self.run_sh("--skip-tools")
        text = self.text(result)
        self.assertEqual(result.returncode, 0, text)
        self.assertIn(f"python3 -m pip install --user {self.clone}", self.calls())
        self.assertIn("A new terminal does not find oah yet", text)
        self.assertIn('export PATH="', text)
        self.assertEqual(self.last_line(result), "OAH_INSTALL_OK")

    def test_install_sh_without_python_or_uv_fails_with_next_step(self):
        result = self.run_sh(stdin="n\n")
        text = self.text(result)
        self.assertEqual(result.returncode, 1, text)
        self.assertIn("curl -LsSf https://astral.sh/uv/install.sh | sh", text)
        self.assertEqual(self.last_line(result), "OAH_INSTALL_FAILED")
        self.assertEqual(snapshot(self.home), {})

    def test_install_sh_reports_incomplete_when_doctor_fails(self):
        self.fake_python()
        self.fake_uv(doctor_code=1)
        self.new_terminal_finds(self.tool_bin)
        result = self.run_sh("--skip-tools")
        self.assertEqual(result.returncode, 1, self.text(result))
        self.assertIn("DOCTOR_RESULT", self.text(result))
        self.assertEqual(self.last_line(result), "OAH_INSTALL_INCOMPLETE")

    def test_piped_dry_run_prints_release_operations_without_invoking_them(self):
        self.fake_python()
        self.fake_uv()
        # Fail even if a command's transient files would have been cleaned up before snapshotting.
        for name in ("mktemp", "mkdir", "cp", "curl", "tar", "rm", "shasum", "sha256sum"):
            write_posix_tool(self.bin, name, f'echo "FORBIDDEN {name}" >> "{self.log}"; exit 91')
        before_home = snapshot(self.home)
        before_scratch = snapshot(self.scratch)
        url, digest = "https://releases.example.invalid/oah.tar.gz", "a" * 64
        result = self.run_piped("--dry-run", url=url, digest=digest.upper())
        text = self.text(result)
        self.assertEqual(result.returncode, 0, text)
        self.assertIn(f"[dry-run] would download: {url}", text)
        self.assertIn("[dry-run] would verify SHA-256:", text)
        self.assertIn("[dry-run] would extract:", text)
        self.assertNotIn("SHA-256 verified:", text)
        for number in range(1, 6):
            self.assertIn(f"Step {number}/5", text)
        self.assertIn("python -m oah.setup", text)
        self.assertIn("tool install --force <release-folder>", text)
        self.assertEqual(self.last_line(result), "OAH_INSTALL_DRY_RUN_OK")
        self.assertEqual(snapshot(self.home), before_home)
        self.assertEqual(snapshot(self.scratch), before_scratch)
        self.assertNotIn("FORBIDDEN", "\n".join(self.calls()))
        self.assertNotIn("uv tool install", "\n".join(self.calls()))

    def test_piped_install_uses_the_unpacked_release(self):
        self.fake_python()
        self.fake_uv()
        self.new_terminal_finds(self.tool_bin)
        archive, digest = make_release(self)
        result = self.run_piped("--skip-tools", url=self.release_download(archive), digest=digest)
        self.assertEqual(result.returncode, 0, self.text(result))
        installs = [call for call in self.calls() if call.startswith("uv tool install --force ")]
        self.assertEqual(len(installs), 1)
        self.assertRegex(installs[0], r"/oah-install\.\w+/release/open-agent-harness-1\.0\.0$")
        self.assertIn("oah doctor", self.calls())
        download = next(call for call in self.calls() if call.startswith("curl "))
        self.assertIn("--proto =https", download)
        self.assertIn("--proto-redir =https", download)
        self.assertEqual(self.last_line(result), "OAH_INSTALL_OK")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_piped_refuses_a_digest_mismatch_before_unpacking(self):
        self.fake_python()
        archive, digest = make_release(self)
        result = self.run_piped(url=self.release_download(archive), digest="0" * 64)
        text = self.text(result)
        self.assertEqual(result.returncode, 1, text)
        self.assertIn(f"OAH_INSTALL_DIGEST_MISMATCH: the download's SHA-256 is {digest}", text)
        self.assertNotIn("Step 1/5", text)
        self.assertEqual(self.last_line(result), "OAH_INSTALL_FAILED")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_piped_refuses_non_https_release_urls_before_writing_or_downloading(self):
        archive, digest = make_release(self)
        self.release_download(archive)
        for url in ("http://releases.example.invalid/oah.tar.gz", archive.as_uri(), str(archive)):
            with self.subTest(url=url):
                result = self.run_piped(url=url, digest=digest)
                self.assertEqual(result.returncode, 1, self.text(result))
                self.assertIn("OAH_INSTALL_HTTPS_REQUIRED", self.text(result))
                self.assertNotIn("Step 1/5", self.text(result))
                self.assertEqual(list(self.scratch.iterdir()), [])
                self.assertEqual(self.calls(), [])

    def test_piped_refuses_while_the_release_is_unpublished(self):
        result = self.run_piped("--dry-run")
        self.assertEqual(result.returncode, 1, self.text(result))
        self.assertIn("OAH_INSTALL_NOT_PUBLISHED", self.text(result))
        self.assertIn("xcode-select --install", self.text(result))
        # A copy of the script outside a clone is treated the same way.
        alone = self.work / "install.sh"
        shutil.copy2(INSTALL_SH, alone)
        result = subprocess.run([BASH, str(alone), "--dry-run"], env=self.env(), capture_output=True, timeout=60)
        self.assertIn("OAH_INSTALL_NOT_PUBLISHED", self.text(result))

    def test_piped_questions_never_read_the_script_itself(self):
        # No Python and no uv: the first question is "Install uv now?". With the script on stdin and no
        # terminal, the answer must read as "no" instead of swallowing the rest of the script.
        archive, digest = make_release(self)
        result = self.run_piped(url=self.release_download(archive), digest=digest)
        text = self.text(result)
        self.assertEqual(result.returncode, 1, text)
        self.assertIn("Install uv now? [y/N]", text)
        self.assertIn("curl -LsSf https://astral.sh/uv/install.sh | sh", text)
        self.assertEqual(self.last_line(result), "OAH_INSTALL_FAILED")
        self.assertEqual(snapshot(self.home), {})

    def test_install_sh_rejects_unknown_option(self):
        result = self.run_sh("--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("OAH_INSTALL_USAGE", self.text(result))


class InstallPs1Test(unittest.TestCase):
    def test_powershell_command_is_noninteractive_and_suppresses_progress(self):
        with mock.patch.object(shutil, "which", return_value="powershell"), mock.patch.object(
                subprocess, "run") as run:
            powershell_command("powershell", "[Console]::Out.WriteLine('ready')", {})
        argv = run.call_args.args[0]
        self.assertIn("-NoProfile", argv)
        self.assertIn("-NonInteractive", argv)
        script = base64.b64decode(argv[-1]).decode("utf-16-le")
        self.assertTrue(script.startswith("$ProgressPreference = 'SilentlyContinue'; "))
        self.assertIn("[Console]::Out.WriteLine('ready')", script)

    def test_powershell_snapshot_catches_writes_in_every_redirected_location(self):
        env = powershell_env(self)
        for name in ("HOME", "APPDATA", "LOCALAPPDATA", "TEMP"):
            with self.subTest(location=name):
                before = powershell_snapshot(env)
                folder = pathlib.Path(env[name])
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / "unexpected-installer-write.txt"
                target.write_text("created", encoding="utf-8")
                after = powershell_snapshot(env)
                self.assertNotEqual(after[name], before[name])
                target.write_text("modified", encoding="utf-8")
                self.assertNotEqual(powershell_snapshot(env)[name], after[name])

    def test_powershell_snapshot_allows_only_exact_cache_directories(self):
        env = powershell_env(self)
        before = powershell_snapshot(env)
        caches = {
            "HOME": ("AppData/Local/Microsoft/Windows/PowerShell", "AppData/Roaming/Microsoft/PowerShell"),
            "APPDATA": ("Microsoft/Windows/PowerShell", "Microsoft/PowerShell"),
            "LOCALAPPDATA": ("Microsoft/Windows/PowerShell", "Microsoft/PowerShell"),
        }
        for name, relatives in caches.items():
            for relative in relatives:
                folder = pathlib.Path(env[name]) / relative
                folder.mkdir(parents=True)
                (folder / "ModuleAnalysisCache").write_bytes(b"PowerShell cache")
        self.assertEqual(powershell_snapshot(env), before)
        for name, relative in (("HOME", "AppData/Local/Other/cache"),
                               ("APPDATA", "Microsoft/Windows/PowerShell-other/cache"),
                               ("LOCALAPPDATA", "Microsoft/Other/cache"),
                               ("TEMP", "Microsoft/Windows/PowerShell/cache")):
            with self.subTest(location=name, relative=relative):
                previous = powershell_snapshot(env)
                path = pathlib.Path(env[name]) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"must be caught")
                self.assertNotEqual(powershell_snapshot(env)[name], previous[name])

    def test_installers_dry_run_branches_only_print(self):
        for shell, path in (("bash", INSTALL_SH), ("powershell", INSTALL_PS1)):
            with self.subTest(shell=shell):
                assert_dry_run_branches(self, path.read_text(encoding="utf-8"), shell)

    def test_dry_run_static_check_rejects_writes_downloads_and_extraction(self):
        for shell, path, marker, operations in (
                ("bash", INSTALL_SH, '    REPO=\'<release-folder>\'',
                 ('touch "$HOME/changed"', 'curl https://example.invalid/file', 'tar -xf release.tar.gz')),
                ("powershell", INSTALL_PS1, "        return '<release-folder>'",
                 ("[IO.File]::WriteAllText('changed', 'data')", "Save-Release $releaseUri 'file'",
                  "Expand-Archive release.zip"))):
            source = path.read_text(encoding="utf-8")
            for operation in operations:
                with self.subTest(shell=shell, operation=operation):
                    mutated = source.replace(marker, operation + "\n" + marker)
                    self.assertNotEqual(mutated, source)
                    with self.assertRaises(AssertionError):
                        assert_dry_run_branches(self, mutated, shell)

    def test_install_ps1_hashing_does_not_depend_on_modules(self):
        text = INSTALL_PS1.read_text(encoding="ascii")
        self.assertNotIn("Get-FileHash", text)
        self.assertIn("[System.Security.Cryptography.SHA256]::Create()", text)
        self.assertIn("[IO.File]::OpenRead($archive)", text)
        self.assertIn(".ComputeHash(", text)

    def test_install_ps1_restricts_redirects_to_https(self):
        text = INSTALL_PS1.read_text(encoding="ascii")
        self.assertIn("OAH_INSTALL_HTTPS_REQUIRED", text)
        self.assertIn(".AllowAutoRedirect = $false", text)
        self.assertIn("$Uri.Scheme -ne 'https'", text)
        self.assertNotIn("Test-Path -LiteralPath $url", text)

    def test_install_ps1_is_ascii_with_crlf(self):
        data = INSTALL_PS1.read_bytes()
        self.assertTrue(all(byte < 128 for byte in data), "install.ps1 must be ASCII only")
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertNotIn(b"\r", data.replace(b"\r\n", b""))
        self.assertTrue(data.endswith(b"\r\n"))

    def test_install_scripts_use_catalog_uv_command(self):
        uv = next(tool for tool in catalog() if tool.name == "uv")
        self.assertIn(f"'{display(uv.install['macos'][0])}'", INSTALL_SH.read_text(encoding="utf-8"))
        self.assertIn(f"'{display(uv.install['windows'][0])}'", INSTALL_PS1.read_text(encoding="ascii"))

    def test_scripts_and_guides_use_one_repository_url(self):
        repository = re.search(r"^REPOSITORY='(https://github\.com/[^/']+/open-agent-harness)'$",
                               INSTALL_SH.read_text(encoding="utf-8"), re.M).group(1)
        self.assertIn(f"$Repository = '{repository}'", INSTALL_PS1.read_text(encoding="ascii"))
        guides = {name: (ROOT / "docs" / name).read_text(encoding="utf-8")
                  for name in ("install-macos.md", "install-windows.md", "ko/install-macos.md", "ko/install-windows.md")}
        for name, text in guides.items():
            with self.subTest(guide=name):
                self.assertIn(f"git clone {repository}.git", text)
                self.assertNotIn("<repository URL>", text)
                self.assertNotIn("<저장소 주소>", text)
                script = "install.ps1" if "windows" in name else "install.sh"
                self.assertIn(f"{repository}/releases/download/<version>/{script}", text)

    def test_install_ps1_has_no_double_quotes_in_native_arguments(self):
        # Windows PowerShell 5.1 mangles embedded double quotes passed to native programs, so the
        # script keeps Python code and one-liners in single-quoted strings.
        text = INSTALL_PS1.read_text(encoding="ascii")
        self.assertNotIn('-c "', text)
        self.assertIn("Update-SessionPath", text)
        self.assertIn("[Environment]::GetEnvironmentVariable('Path', 'Machine')", text)

    def test_install_ps1_never_exits_when_piped(self):
        # `irm | iex` runs in the user's own session: an exit there would close the window.
        lines = [line.strip() for line in INSTALL_PS1.read_text(encoding="ascii").splitlines()]
        exits = [line for line in lines if re.search(r"\bexit\b", line) and not line.startswith("#")]
        self.assertEqual(exits, ["if ($OahScriptRoot) { exit $OahExitCode }"])
        self.assertIn("throw 'OAH_INSTALL_FAILED'", lines)
        self.assertIn("$ReleaseVersion = '__OAH_RELEASE_VERSION__'  # the release tag", lines)
        self.assertIn('$ReleaseUrl = "$Repository/releases/download/$ReleaseVersion/'
                      'open-agent-harness-$ReleaseVersion.zip"', lines)

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_refreshes_uv_discovered_outside_persistent_path(self):
        source = INSTALL_PS1.read_text(encoding="ascii")
        functions = "\n".join(re.search(r"(?ms)^function " + name + r"\b.*?^}", source).group(0)
                              for name in ("Update-SessionPath", "Find-Program", "Find-Uv"))
        # Model a fresh Windows account whose registry PATH has never included uv.
        for scope in ("Machine", "User"):
            functions = functions.replace(f"[Environment]::GetEnvironmentVariable('Path', '{scope}')", "''")
        for shell in [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]:
            with self.subTest(shell=shell):
                env = powershell_env(self, PATH=os.path.join(os.environ["SystemRoot"], "System32"))
                folder = pathlib.Path(env["HOME"]) / ".local" / "bin"
                folder.mkdir(parents=True)
                uv = folder / "uv.exe"
                script = functions + f"""
if (Find-Uv) {{ throw 'uv must start absent' }}
[IO.File]::WriteAllBytes({ps_literal(str(uv))}, [byte[]]@())
$script:Uv = Find-Uv
if ($script:Uv -ne {ps_literal(str(uv))}) {{ throw 'uv was not discovered' }}
Update-SessionPath
if ((Find-Program 'uv') -ne $script:Uv) {{ throw 'uv is missing from the session PATH' }}
$child = & {ps_literal(sys.executable)} -c 'import shutil; print(shutil.which(''uv''))'
if ($child.Trim() -ine $script:Uv) {{ throw 'setup does not inherit the uv directory' }}
[Console]::Out.WriteLine('UV_SESSION_PATH_OK')
exit 0
"""
                result = powershell_command(shell, script, env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(b"UV_SESSION_PATH_OK", result.stdout)

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_powershell_snapshot_catches_dotnet_writes_despite_command_guards(self):
        for shell in [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]:
            with self.subTest(shell=shell):
                env = powershell_env(self)
                before = powershell_snapshot(env)
                script = "function New-Item { throw 'guard must not be called' };\n"
                for name in ("HOME", "APPDATA", "LOCALAPPDATA", "TEMP"):
                    target = pathlib.Path(env[name]) / "unexpected-dotnet-write.txt"
                    script += f"[IO.File]::WriteAllText({ps_literal(str(target))}, 'changed');\n"
                script += "exit 0"
                result = powershell_command(shell, script, env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                after = powershell_snapshot(env)
                for name in ("HOME", "APPDATA", "LOCALAPPDATA", "TEMP"):
                    self.assertNotEqual(after[name], before[name], name)

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_piped_dry_run_never_downloads_or_writes(self):
        for shell in [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]:
            with self.subTest(shell=shell):
                scratch = make_tmp_dir(self)
                home = make_tmp_dir(self)
                url = "https://releases.example.invalid/oah.zip"
                env = powershell_env(self, OAH_RELEASE_URL=url, OAH_RELEASE_SHA256="a" * 64,
                                     TEMP=str(scratch), TMP=str(scratch), HOME=str(home), USERPROFILE=str(home))
                guards = "\n".join(
                    f"function {command} {{ [Console]::Out.WriteLine('FORBIDDEN {command}'); throw 'forbidden' }}"
                    for command in ("New-Item", "Copy-Item", "Expand-Archive", "Remove-Item", "Save-Release",
                                    "Invoke-WebRequest"))
                script = ps1_with_overrides(guards) + (
                    "& ([scriptblock]::Create($installer)) -DryRun; exit $LASTEXITCODE")
                before = powershell_snapshot(env)
                result = powershell_command(shell, script, env)
                text = result.stdout.decode("utf-8", "replace")
                self.assertEqual(result.returncode, 0, text + result.stderr.decode("utf-8", "replace"))
                self.assertIn(f"[dry-run] would download: {url}", text)
                self.assertIn("[dry-run] would verify SHA-256:", text)
                self.assertIn("[dry-run] would extract:", text)
                self.assertIn("python -m oah.setup", text)
                self.assertNotIn("SHA-256 verified:", text)
                self.assertNotIn("FORBIDDEN", text)
                self.assertEqual(text.rstrip().splitlines()[-1], "OAH_INSTALL_DRY_RUN_OK")
                self.assertEqual(powershell_snapshot(env), before)

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_verifies_release_without_get_filehash(self):
        archive, digest = make_zip_release(self)
        for shell in [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]:
            with self.subTest(shell=shell):
                scratch = make_tmp_dir(self)
                env = powershell_env(self, OAH_RELEASE_URL="https://releases.example.invalid/oah.zip",
                                     OAH_RELEASE_SHA256=digest.upper(), TEMP=str(scratch), TMP=str(scratch))
                overrides = ps1_release_fixture(archive) + (
                    "function Get-FileHash { throw 'Get-FileHash must not be used' }\n")
                script = ps1_with_overrides(overrides) + "$installer | Invoke-Expression; " + (
                    "[Console]::Out.WriteLine('SESSION_ALIVE ' + $LASTEXITCODE)")
                result = powershell_command(shell, script, env)
                text = result.stdout.decode("utf-8", "replace")
                self.assertIn(f"SHA-256 verified: {digest}", text)
                self.assertIn("Step 1/5", text)
                self.assertEqual(text.rstrip().splitlines()[-1], "SESSION_ALIVE 1")
                self.assertEqual([p.name for p in scratch.iterdir() if p.name.startswith("oah-install-")], [])

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_uv_bootstrap_environment(self):
        # Run the actual process-start helpers with an inert installer that only reads its env.
        source = INSTALL_PS1.read_text(encoding="ascii")
        functions = "\n".join(re.search(r"(?ms)^function " + name + r"\b.*?^}", source).group(0)
                              for name in ("Join-Arguments", "Start-Native", "Invoke-OneLiner"))
        for shell in [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]:
            with self.subTest(shell=shell):
                script = (functions + "\nfunction Say {}\nfunction Update-SessionPath {}\n"
                          "$DryRun = $false; $env:UV_NO_MODIFY_PATH = '0'; "
                          "$WindowsPowerShell = Join-Path $env:SystemRoot "
                          "'System32\\WindowsPowerShell\\v1.0\\powershell.exe'; "
                          "Invoke-OneLiner 'if ($env:UV_NO_MODIFY_PATH -ne ''1'') { exit 23 }'; "
                          "if ($env:UV_NO_MODIFY_PATH -ne '0') { exit 24 }; exit $script:StepCode")
                result = powershell_command(shell, script, powershell_env(self))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_refuses_http_redirects_before_downloading(self):
        source = INSTALL_PS1.read_text(encoding="ascii")
        match = re.search(r"(?ms)^function Save-Release\b.*?^}", source)
        self.assertIsNotNone(match, "the downloader must be separate from local archive fixtures")
        function = match.group(0).replace("[Net.WebRequest]::Create($Uri)", "(New-TestRequest $Uri)")
        for shell in [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]:
            with self.subTest(shell=shell):
                output = make_tmp_dir(self) / "must-not-exist.zip"
                script = function + """
$script:requests = 0
$script:bodyReads = 0
function New-TestRequest($Uri) {
    $script:requests++
    $response = [pscustomobject]@{StatusCode=302; Headers=@{Location='http://example.invalid/oah.zip'}}
    $response | Add-Member ScriptMethod Dispose {}
    $response | Add-Member ScriptMethod GetResponseStream {
        $script:bodyReads++
        throw 'FORBIDDEN response body download'
    }
    $request = [pscustomobject]@{AllowAutoRedirect=$true; Response=$response}
    $request | Add-Member ScriptMethod GetResponse {
        if ($this.AllowAutoRedirect) { throw 'FORBIDDEN automatic redirect' }
        return $this.Response
    }
    return $request
}
try {
""" + f"Save-Release ([Uri]'https://example.invalid/oah.zip') {ps_literal(str(output))}\n" + """
    exit 2
} catch {
    if ($_.Exception.Message -ne 'Use an HTTPS release URL with HTTPS-only redirects, or clone the repository instead.' -or
        $script:requests -ne 1 -or $script:bodyReads -ne 0) { throw }
    [Console]::Out.WriteLine($_.Exception.Message)
}
# A caught terminating error can leave $? false; success here means the refusal was verified.
exit 0
"""
                result = powershell_command(shell, script, powershell_env(self))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(b"Use an HTTPS release URL with HTTPS-only redirects, or clone the repository instead.",
                              result.stdout)
                self.assertFalse(output.exists())

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_refuses_non_https_release_urls_before_writing(self):
        archive, digest = make_zip_release(self)
        for url in ("http://example.invalid/oah.zip", archive.as_uri(), str(archive)):
            for shell in [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]:
                with self.subTest(shell=shell, url=url):
                    scratch = make_tmp_dir(self)
                    env = powershell_env(self, OAH_RELEASE_URL=url, OAH_RELEASE_SHA256=digest,
                                         TEMP=str(scratch), TMP=str(scratch))
                    guards = ("function New-Item { throw 'FORBIDDEN mkdir' }\n"
                              "function Save-Release { throw 'FORBIDDEN download' }\n")
                    script = ps1_with_overrides(guards) + "$installer | Invoke-Expression; exit $LASTEXITCODE"
                    result = powershell_command(shell, script, env)
                    text = result.stdout.decode("utf-8", "replace")
                    self.assertEqual(result.returncode, 1, text + result.stderr.decode("utf-8", "replace"))
                    self.assertIn("OAH_INSTALL_HTTPS_REQUIRED", text)
                    self.assertNotIn("FORBIDDEN", text)
                    self.assertEqual(list(scratch.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_piped_iex_keeps_the_session_open(self):
        archive, digest = make_zip_release(self)
        scratch = make_tmp_dir(self)
        env = powershell_env(self, OAH_RELEASE_URL="https://releases.example.invalid/oah.zip",
                             OAH_RELEASE_SHA256="0" * 64, TEMP=str(scratch), TMP=str(scratch))
        script = ps1_with_overrides(ps1_release_fixture(archive)) + (
            "$installer | Invoke-Expression; [Console]::Out.WriteLine('SESSION_ALIVE ' + $LASTEXITCODE)")
        result = powershell_command("powershell", script, env)
        text = result.stdout.decode("utf-8", "replace")
        self.assertIn(f"OAH_INSTALL_DIGEST_MISMATCH: the download's SHA-256 is {digest}", text)
        self.assertEqual(text.rstrip().splitlines()[-1], "SESSION_ALIVE 1")
        self.assertEqual([p.name for p in scratch.iterdir() if p.name.startswith("oah-install-")], [])

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_dry_run_changes_nothing(self):
        shells = [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]
        self.assertIn("powershell", shells)
        for shell in shells:
            with self.subTest(shell=shell):
                clone = make_clone(self)
                home = make_tmp_dir(self)
                before_clone = snapshot(clone)
                env = powershell_env(self, USERPROFILE=str(home), HOME=str(home))
                before = powershell_snapshot(env)
                script = (f"Set-Location -LiteralPath {ps_literal(str(home))}; "
                          f"& {ps_literal(str(clone / 'install' / 'install.ps1'))} -DryRun; exit $LASTEXITCODE")
                result = powershell_command(shell, script, env)
                text = result.stdout.decode("utf-8", "replace")
                self.assertEqual(result.returncode, 0, text + result.stderr.decode("utf-8", "replace"))
                self.assertEqual(text.rstrip().splitlines()[-1], "OAH_INSTALL_DRY_RUN_OK")
                for number in range(1, 6):
                    self.assertIn(f"Step {number}/5", text)
                self.assertEqual(snapshot(clone), before_clone)
                self.assertEqual(powershell_snapshot(env), before)

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_rejects_unknown_option(self):
        script = f"& {ps_literal(str(INSTALL_PS1))} --bogus; exit $LASTEXITCODE"
        result = powershell_command("powershell", script, powershell_env(self))
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"OAH_INSTALL_USAGE", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
