// 사이드카 준비 스크립트: oah CLI 와 프로젝트 도구 실행기(oah-project-tools)를 빌드해 src-tauri/binaries/<이름>-<triple>[.exe] 로 놓고 --version 으로 확인한다.
//
// Usage: node apps/desktop/scripts/stage-cli.mjs [--cli <built oah binary>] [--project-tools <built runner>]
//                                                [--python <interpreter>] [--target <triple>] [--out <folder>]
//
// Without --cli the script runs `cargo build -p oah-cli --release --locked` and stages the result. With --cli it
// stages that already-built file instead and runs no cargo command. The staged files are the sidecars Tauri bundles
// through `bundle.externalBin: ["binaries/oah", "binaries/oah-project-tools"]`; the bundler strips the triple. When
// the target is the host, each staged file is run once with `--version` and must print `<name> <workspace
// version>`; a staged file that fails that check is removed again.
//
// The project-tools runner is the Python impact generator frozen into one file, so an installed app needs no
// Python. Without --project-tools the script freezes it with PyInstaller from project-tools.spec. It installs
// nothing: the interpreter (--python, else OAH_PROJECT_TOOLS_PYTHON, else python3.11, python3, python) must
// already be Python 3.11 with exactly the PyInstaller version that project-tools-requirements.txt pins
// (python -m pip install --require-hashes -r apps/desktop/scripts/project-tools-requirements.txt). Any other
// version is refused before anything is built. PyInstaller cannot build for another platform, so a cross target
// needs --project-tools with a runner that was built on that platform.
//
// On macOS the runner is not an externalBin. The bundler signs every externalBin with the hardened runtime, also
// with the ad-hoc identity, and a one-file runner signed that way cannot load its own Python library (library
// validation needs one Team ID, and ad-hoc code has none). So it is staged as oah-project-tools-macos and
// tauri.macos.conf.json places it beside the app's executable through bundle.macOS.files, which the bundler copies
// and seals into the app's signature without signing the file again; it keeps PyInstaller's own ad-hoc signature.
// The app and the oah CLI keep their signing. On every platform the installed runner is
// <folder of the app's executable>/oah-project-tools[.exe].

import { spawnSync } from "node:child_process";
import { chmodSync, copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
export const DESKTOP_DIR = resolve(SCRIPT_DIR, "..");
export const REPO_ROOT = resolve(DESKTOP_DIR, "..", "..");
export const DEFAULT_OUT = join(DESKTOP_DIR, "src-tauri", "binaries");

export const PROJECT_TOOLS = "oah-project-tools";
export const PROJECT_TOOLS_PYTHON = "3.11";
export const PROJECT_TOOLS_SPEC = join(SCRIPT_DIR, "project-tools.spec");
export const PROJECT_TOOLS_REQUIREMENTS = join(SCRIPT_DIR, "project-tools-requirements.txt");

// A one-file runner unpacks itself before it answers, so its first start is slower than a native binary's.
const VERSION_TIMEOUT_MS = 60_000;
const BUILDER_PROBE = "import sys, PyInstaller; print('%d.%d %s' % (*sys.version_info[:2], PyInstaller.__version__))";
const OPTIONS = {
  "--cli": "cli",
  "--project-tools": "projectTools",
  "--python": "python",
  "--target": "target",
  "--out": "out",
};

export class StageError extends Error {}

/** Parses the command line; unknown or incomplete options are a StageError. */
export function parseArgs(argv) {
  const options = { cli: null, projectTools: null, python: null, target: null, out: DEFAULT_OUT };
  for (let i = 0; i < argv.length; i += 1) {
    const name = argv[i];
    if (!Object.hasOwn(OPTIONS, name)) {
      throw new StageError(`unknown option ${name}`);
    }
    const value = argv[i + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new StageError(`${name} needs a value`);
    }
    options[OPTIONS[name]] = value;
    i += 1;
  }
  return options;
}

function run(command, args, options = {}) {
  return spawnSync(command, args, { encoding: "utf8", shell: false, ...options });
}

/** The host target triple, as rustc reports it. */
export function hostTriple() {
  const result = run("rustc", ["-vV"]);
  const match = result.status === 0 ? /^host: (\S+)$/m.exec(result.stdout) : null;
  if (!match) {
    throw new StageError("rustc -vV did not report a host triple");
  }
  return match[1];
}

export function exeSuffix(triple) {
  return triple.includes("windows") ? ".exe" : "";
}

/** The file name Tauri expects for `externalBin: ["binaries/<name>"]` on this target. */
export function sidecarName(triple, name = "oah") {
  return `${name}-${triple}${exeSuffix(triple)}`;
}

/**
 * The staged file name of the project-tools runner: the externalBin name, except on macOS, where
 * tauri.macos.conf.json names one file for every Mac architecture (see the header).
 */
export function projectToolsName(triple) {
  return triple.endsWith("-apple-darwin") ? `${PROJECT_TOOLS}-macos` : sidecarName(triple, PROJECT_TOOLS);
}

/** The workspace version from the root manifest's `[workspace.package]` table. */
export function workspaceVersion(manifest = join(REPO_ROOT, "Cargo.toml")) {
  const text = readFileSync(manifest, "utf8");
  const table = /^\[workspace\.package\]\s*$([\s\S]*?)(?=^\[|(?![\s\S]))/m.exec(text);
  const version = table && /^version\s*=\s*"([^"]+)"\s*$/m.exec(table[1]);
  if (!version) {
    throw new StageError("the root Cargo.toml has no [workspace.package] version");
  }
  return version[1];
}

/** Builds oah-cli in release mode with the locked dependencies and returns the built file. */
export function buildCli(triple, host) {
  const args = ["build", "-p", "oah-cli", "--release", "--locked"];
  if (triple !== host) {
    args.push("--target", triple);
  }
  const result = run("cargo", args, { cwd: REPO_ROOT, stdio: "inherit" });
  if (result.status !== 0) {
    throw new StageError(`cargo ${args.join(" ")} failed`);
  }
  const targetDir = process.env.CARGO_TARGET_DIR
    ? resolve(REPO_ROOT, process.env.CARGO_TARGET_DIR)
    : join(REPO_ROOT, "target");
  const profileDir = triple === host ? join(targetDir, "release") : join(targetDir, triple, "release");
  return join(profileDir, `oah${exeSuffix(triple)}`);
}

/** The PyInstaller version the requirements file pins; the one place that version is written. */
export function pinnedPyInstaller(file = PROJECT_TOOLS_REQUIREMENTS) {
  const pin = /^pyinstaller==(\d[\w.]*)(?=[\s\\;]|$)/m.exec(readFileSync(file, "utf8"));
  if (!pin) {
    throw new StageError(`${file} does not pin pyinstaller==<version>`);
  }
  return pin[1];
}

/** Asks one interpreter for its Python and PyInstaller versions; throws unless both are the pinned ones. */
export function checkBuilder(python, expected = `${PROJECT_TOOLS_PYTHON} ${pinnedPyInstaller()}`) {
  const result = run(python, ["-c", BUILDER_PROBE], { timeout: VERSION_TIMEOUT_MS });
  const line = (result.stdout ?? "").split(/\r?\n/, 1)[0];
  if (result.error || result.status !== 0 || line !== expected) {
    const found = result.error ? result.error.message : line || `exit ${result.status}, no PyInstaller`;
    throw new StageError(`${python} is not Python ${expected.replace(" ", " with PyInstaller ")} (${found})`);
  }
  return python;
}

/** The interpreter that builds the runner: the explicit one, or the first usual name that passes checkBuilder. */
export function findPython(explicit = process.env.OAH_PROJECT_TOOLS_PYTHON || null, expected = undefined) {
  if (explicit) {
    return checkBuilder(explicit, expected);
  }
  const refusals = [];
  for (const candidate of ["python3.11", "python3", "python"]) {
    try {
      return checkBuilder(candidate, expected);
    } catch (error) {
      if (!(error instanceof StageError)) {
        throw error;
      }
      refusals.push(error.message);
    }
  }
  throw new StageError(
    `no interpreter can build ${PROJECT_TOOLS}: ${refusals.join("; ")}. Install the pinned builder into Python ` +
      `${PROJECT_TOOLS_PYTHON} (python -m pip install --require-hashes -r ${PROJECT_TOOLS_REQUIREMENTS}) or pass ` +
      "--python <interpreter>",
  );
}

/** Freezes the project-tools runner with the checked interpreter and returns the built file. */
export function buildProjectTools({ python, triple, host }) {
  if (triple !== host) {
    throw new StageError(
      `${PROJECT_TOOLS} cannot be built for ${triple} on ${host}; build it on that platform and pass --project-tools`,
    );
  }
  const targetDir = process.env.CARGO_TARGET_DIR
    ? resolve(REPO_ROOT, process.env.CARGO_TARGET_DIR)
    : join(REPO_ROOT, "target");
  const work = join(targetDir, "project-tools", triple);
  const args = ["-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN"];
  args.push("--distpath", join(work, "dist"), "--workpath", join(work, "build"), PROJECT_TOOLS_SPEC);
  // The spec names this checkout's src/ itself; no inherited Python variable may add another one.
  const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/^PYTHON/i.test(name)));
  const result = run(python, args, { cwd: REPO_ROOT, stdio: "inherit", env });
  if (result.error || result.status !== 0) {
    throw new StageError(`${python} ${args.join(" ")} failed`);
  }
  return join(work, "dist", `${PROJECT_TOOLS}${exeSuffix(triple)}`);
}

/** Runs a staged sidecar with --version and returns its first output line, or throws. */
export function checkVersion(file, expected, name = "oah") {
  const result = run(file, ["--version"], { timeout: VERSION_TIMEOUT_MS });
  const line = (result.stdout ?? "").split(/\r?\n/, 1)[0];
  if (result.error || result.status !== 0 || line !== `${name} ${expected}`) {
    const reason = result.error ? result.error.message : `exit ${result.status}, printed "${line}"`;
    const what = name === "oah" ? "CLI" : name;
    throw new StageError(`the staged ${what} did not print "${name} ${expected}" (${reason})`);
  }
  return line;
}

/**
 * Copies `source` to `<out>/<fileName>` (by default `<name>-<triple>[.exe]`) through a temporary sibling and a
 * rename, and checks it with `--version` when it can run here. Returns `{ path, version }`; `version` is null
 * when the target is not the host, because nothing was run.
 */
export function stage({ source, triple, host, out, expectedVersion, name = "oah", fileName = sidecarName(triple, name) }) {
  if (!existsSync(source) || !statSync(source).isFile()) {
    throw new StageError(`the built ${name === "oah" ? "CLI" : name} ${source} does not exist`);
  }
  mkdirSync(out, { recursive: true });
  const dest = join(out, fileName);
  const temp = join(out, `.${fileName}.${process.pid}.tmp${exeSuffix(triple)}`);
  try {
    copyFileSync(source, temp);
    if (process.platform !== "win32") {
      chmodSync(temp, 0o755);
    }
    const version = triple === host ? checkVersion(temp, expectedVersion, name) : null;
    renameSync(temp, dest);
    return { path: dest, version };
  } finally {
    rmSync(temp, { force: true });
  }
}

export function main(argv) {
  const options = parseArgs(argv);
  const host = hostTriple();
  const triple = options.target ?? process.env.TAURI_ENV_TARGET_TRIPLE ?? host;
  const out = resolve(options.out);
  const expectedVersion = workspaceVersion();
  // Refuse a wrong builder before the long CLI build, not after it.
  const python = options.projectTools || triple !== host ? null : findPython(options.python ?? undefined);
  const staged = [
    stage({ source: options.cli ? resolve(options.cli) : buildCli(triple, host), triple, host, out, expectedVersion }),
    stage({
      source: options.projectTools ? resolve(options.projectTools) : buildProjectTools({ python, triple, host }),
      triple,
      host,
      out,
      expectedVersion,
      name: PROJECT_TOOLS,
      fileName: projectToolsName(triple),
    }),
  ];
  for (const result of staged) {
    const checked = result.version ? `checked: ${result.version}` : "not run: target is not the host";
    console.log(`staged ${result.path} (${checked})`);
  }
  return { ...staged[0], projectTools: staged[1] };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    if (error instanceof StageError) {
      console.error(`stage-cli: ${error.message}`);
      process.exit(1);
    }
    throw error;
  }
}
