# 설치 스크립트 검사 — 임시 클론과 가짜 도구로 install.sh 의 단계·대체 경로·드라이런 무변경을, install.ps1 의 ASCII·CRLF·드라이런을 확인한다.
from __future__ import annotations

import base64
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import unittest
import zipfile

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
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run([shutil.which(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                          env=env, capture_output=True, timeout=300)


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

    def test_install_sh_is_bash_32_compatible(self):
        source = INSTALL_SH.read_text(encoding="utf-8")
        for pattern in (r"declare -A", r"\bmapfile\b", r"\breadarray\b", r"\$\{[A-Za-z_]+,,", r"\$\{[A-Za-z_]+\^\^",
                        r"\|&", r"&>>", r";;&", r"\bcoproc\b", r"\$\{[A-Za-z_]+\[-1\]\}"):
            self.assertIsNone(re.search(pattern, source), pattern)
        self.assertNotIn("\r", source)
        self.assertTrue(source.startswith("#!/bin/bash\n"))
        # Piped downloads run only once complete: everything is inside main(), called on the last line.
        self.assertEqual(source.rstrip().splitlines()[-1], 'main "$@"')
        self.assertIn("RELEASE_URL='__OAH_RELEASE_URL__'", source)
        self.assertIn("RELEASE_SHA256='__OAH_RELEASE_SHA256__'", source)
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

    def test_piped_dry_run_verifies_the_release_and_changes_nothing(self):
        self.fake_python()
        self.fake_uv()
        archive, digest = make_release(self)
        before_home = snapshot(self.home)
        result = self.run_piped("--dry-run", url=str(archive), digest=digest.upper())
        text = self.text(result)
        self.assertEqual(result.returncode, 0, text)
        self.assertIn(f"SHA-256 verified: {digest}", text)
        for number in range(1, 6):
            self.assertIn(f"Step {number}/5", text)
        self.assertIn("SETUP_DRY_RUN planned=", text)
        self.assertRegex(text, r"\[dry-run\] would run: \S+/uv tool install --force \S+/oah-install\.\w+/release/"
                               r"open-agent-harness-1\.0\.0")
        self.assertEqual(self.last_line(result), "OAH_INSTALL_DRY_RUN_OK")
        self.assertEqual(snapshot(self.home), before_home)
        self.assertEqual(list(self.scratch.iterdir()), [])  # the temporary download is gone
        self.assertNotIn("uv tool install", "\n".join(self.calls()))

    def test_piped_install_uses_the_unpacked_release(self):
        self.fake_python()
        self.fake_uv()
        self.new_terminal_finds(self.tool_bin)
        archive, digest = make_release(self)
        result = self.run_piped("--skip-tools", url=str(archive), digest=digest)
        self.assertEqual(result.returncode, 0, self.text(result))
        installs = [call for call in self.calls() if call.startswith("uv tool install --force ")]
        self.assertEqual(len(installs), 1)
        self.assertRegex(installs[0], r"/oah-install\.\w+/release/open-agent-harness-1\.0\.0$")
        self.assertIn("oah doctor", self.calls())
        self.assertEqual(self.last_line(result), "OAH_INSTALL_OK")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_piped_refuses_a_digest_mismatch_before_unpacking(self):
        self.fake_python()
        archive, digest = make_release(self)
        result = self.run_piped("--dry-run", url=str(archive), digest="0" * 64)
        text = self.text(result)
        self.assertEqual(result.returncode, 1, text)
        self.assertIn(f"OAH_INSTALL_DIGEST_MISMATCH: the download's SHA-256 is {digest}", text)
        self.assertNotIn("Step 1/5", text)
        self.assertEqual(self.last_line(result), "OAH_INSTALL_FAILED")
        self.assertEqual(list(self.scratch.iterdir()), [])

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
        result = self.run_piped(url=str(archive), digest=digest)
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
        self.assertIn("$ReleaseUrl = '__OAH_RELEASE_URL__'        # the release's .zip archive", lines)
        self.assertIn("$ReleaseSha256 = '__OAH_RELEASE_SHA256__'  # its SHA-256, as published with the release", lines)

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_piped_dry_run_verifies_the_release(self):
        archive, digest = make_zip_release(self)
        for shell in [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]:
            with self.subTest(shell=shell):
                scratch = make_tmp_dir(self)
                env = dict(os.environ, OAH_RELEASE_URL=str(archive), OAH_RELEASE_SHA256=digest.upper(),
                           TEMP=str(scratch), TMP=str(scratch))
                env.pop("PYTHONPATH", None)
                script = (f"& ([scriptblock]::Create([IO.File]::ReadAllText('{INSTALL_PS1}'))) -DryRun; "
                          "exit $LASTEXITCODE")
                result = powershell_command(shell, script, env)
                text = result.stdout.decode("utf-8", "replace")
                self.assertEqual(result.returncode, 0, text + result.stderr.decode("utf-8", "replace"))
                self.assertIn(f"SHA-256 verified: {digest}", text)
                self.assertIn("SETUP_DRY_RUN planned=", text)
                self.assertEqual(text.rstrip().splitlines()[-1], "OAH_INSTALL_DRY_RUN_OK")
                self.assertEqual([p.name for p in scratch.iterdir() if p.name.startswith("oah-install-")], [])

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_piped_iex_keeps_the_session_open(self):
        archive, digest = make_zip_release(self)
        scratch = make_tmp_dir(self)
        env = dict(os.environ, OAH_RELEASE_URL=str(archive), OAH_RELEASE_SHA256="0" * 64, TEMP=str(scratch),
                   TMP=str(scratch))
        script = (f"[IO.File]::ReadAllText('{INSTALL_PS1}') | Invoke-Expression; "
                  "[Console]::Out.WriteLine('SESSION_ALIVE ' + $LASTEXITCODE)")
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
                env = dict(os.environ, USERPROFILE=str(home), HOME=str(home))
                env.pop("PYTHONPATH", None)
                result = subprocess.run([shutil.which(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                         str(clone / "install" / "install.ps1"), "-DryRun"],
                                        env=env, capture_output=True, timeout=300, cwd=str(home))
                text = result.stdout.decode("utf-8", "replace")
                self.assertEqual(result.returncode, 0, text + result.stderr.decode("utf-8", "replace"))
                self.assertEqual(text.rstrip().splitlines()[-1], "OAH_INSTALL_DRY_RUN_OK")
                for number in range(1, 6):
                    self.assertIn(f"Step {number}/5", text)
                self.assertEqual(snapshot(clone), before_clone)
                self.assertEqual(snapshot(home), {})

    @unittest.skipUnless(os.name == "nt", "runs Windows PowerShell")
    def test_install_ps1_rejects_unknown_option(self):
        result = subprocess.run([shutil.which("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(INSTALL_PS1), "--bogus"], capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"OAH_INSTALL_USAGE", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
