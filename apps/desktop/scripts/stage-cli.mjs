// 사이드카 준비 스크립트: oah CLI 를 빌드해 src-tauri/binaries/oah-<triple>[.exe] 로 놓고 --version 으로 확인한다.
//
// Usage: node apps/desktop/scripts/stage-cli.mjs [--cli <built oah binary>] [--target <triple>] [--out <folder>]
//
// Without --cli the script runs `cargo build -p oah-cli --release --locked` and stages the result. With --cli it
// stages that already-built file instead and runs no cargo command. The staged file is the sidecar Tauri bundles
// through `bundle.externalBin: ["binaries/oah"]`; the bundler strips the triple. When the target is the host, the
// staged file is run once with `--version` and must print `oah <workspace version>`; a staged file that fails
// that check is removed again.

import { spawnSync } from "node:child_process";
import { chmodSync, copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
export const DESKTOP_DIR = resolve(SCRIPT_DIR, "..");
export const REPO_ROOT = resolve(DESKTOP_DIR, "..", "..");
export const DEFAULT_OUT = join(DESKTOP_DIR, "src-tauri", "binaries");

const VERSION_TIMEOUT_MS = 10_000;

export class StageError extends Error {}

/** Parses the command line; unknown or incomplete options are a StageError. */
export function parseArgs(argv) {
  const options = { cli: null, target: null, out: DEFAULT_OUT };
  for (let i = 0; i < argv.length; i += 1) {
    const name = argv[i];
    if (!["--cli", "--target", "--out"].includes(name)) {
      throw new StageError(`unknown option ${name}`);
    }
    const value = argv[i + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new StageError(`${name} needs a value`);
    }
    options[name.slice(2)] = value;
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

/** The file name Tauri expects for `externalBin: ["binaries/oah"]` on this target. */
export function sidecarName(triple) {
  return `oah-${triple}${exeSuffix(triple)}`;
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

/** Runs a staged CLI with --version and returns its first output line, or throws. */
export function checkVersion(file, expected) {
  const result = run(file, ["--version"], { timeout: VERSION_TIMEOUT_MS });
  const line = (result.stdout ?? "").split(/\r?\n/, 1)[0];
  if (result.error || result.status !== 0 || line !== `oah ${expected}`) {
    const reason = result.error ? result.error.message : `exit ${result.status}, printed "${line}"`;
    throw new StageError(`the staged CLI did not print "oah ${expected}" (${reason})`);
  }
  return line;
}

/**
 * Copies `source` to `<out>/oah-<triple>[.exe]` through a temporary sibling and a rename, and checks it with
 * `--version` when it can run here. Returns `{ path, version }`; `version` is null when the target is not the
 * host, because nothing was run.
 */
export function stage({ source, triple, host, out, expectedVersion }) {
  if (!existsSync(source) || !statSync(source).isFile()) {
    throw new StageError(`the built CLI ${source} does not exist`);
  }
  mkdirSync(out, { recursive: true });
  const dest = join(out, sidecarName(triple));
  const temp = join(out, `.${sidecarName(triple)}.${process.pid}.tmp${exeSuffix(triple)}`);
  try {
    copyFileSync(source, temp);
    if (process.platform !== "win32") {
      chmodSync(temp, 0o755);
    }
    const version = triple === host ? checkVersion(temp, expectedVersion) : null;
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
  const source = options.cli ? resolve(options.cli) : buildCli(triple, host);
  const result = stage({
    source,
    triple,
    host,
    out: resolve(options.out),
    expectedVersion: workspaceVersion(),
  });
  const checked = result.version ? `checked: ${result.version}` : "not run: target is not the host";
  console.log(`staged ${result.path} (${checked})`);
  return result;
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
