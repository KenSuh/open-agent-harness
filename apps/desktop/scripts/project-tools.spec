# 프로젝트 도구 실행기 번들 명세 — PyInstaller 가 이 체크아웃의 src/ 만으로 단일 실행 파일 oah-project-tools 를 만들게 하고, 지도 도구 신원에 쓰이는 영향 지도 소스 바이트와 패키지 데이터(templates)를 함께 싣는다.
#
# Run through apps/desktop/scripts/stage-cli.mjs, which first checks the pinned Python and PyInstaller versions:
#   python -m PyInstaller --noconfirm --clean --distpath <dist> --workpath <work> apps/desktop/scripts/project-tools.spec
#
# One file, console mode, no UPX. The bundle carries:
# - the import closure of oah.project_tools_runner, compiled into the archive;
# - every src/oah/impact/*.py as a data file at oah/impact/: oah.impact.mapstore.tool_digest hashes those source
#   bytes, so the frozen runner and the Python source name the same generator and accept each other's maps;
# - src/oah/templates/**, the package data pyproject.toml declares.
#
# codesign_identity stays None: on macOS PyInstaller then signs the file ad hoc, and tauri.macos.conf.json ships
# exactly that signature (stage-cli.mjs says why the bundler must not sign it again with the hardened runtime).
import pathlib

SCRIPTS = pathlib.Path(SPECPATH).resolve()
SRC = SCRIPTS.parents[2] / "src"
PACKAGE = SRC / "oah"

impact_sources = [(str(path), "oah/impact") for path in sorted((PACKAGE / "impact").glob("*.py"))]
templates = [(str(path), path.parent.relative_to(SRC).as_posix())
             for path in sorted((PACKAGE / "templates").rglob("*"))
             if path.is_file() and "__pycache__" not in path.parts]
if not any(source.endswith("mapstore.py") for source, _ in impact_sources) or not templates:
    raise SystemExit(f"project-tools.spec: the impact sources or templates are missing under {PACKAGE}")

analysis = Analysis(
    [str(SCRIPTS / "project-tools-entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=impact_sources + templates,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="oah-project-tools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
