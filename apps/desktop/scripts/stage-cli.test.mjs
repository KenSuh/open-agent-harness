// stage-cli 테스트: 실제로 빌드된 oah CLI 와 프로젝트 도구 실행기를 임시 폴더에 놓아 보고, 잘못된 후보나 고정되지 않은 빌더는 남지 않는지 본다.
//
// Run: OAH_STAGE_TEST_CLI=<built oah binary> [OAH_STAGE_TEST_PROJECT_TOOLS=<built oah-project-tools binary>]
//      node --test apps/desktop/scripts/
// The staging tests need an oah CLI that was really built from this workspace; without OAH_STAGE_TEST_CLI they
// are reported as skipped, never as passed. The same holds for the project-tools runner and
// OAH_STAGE_TEST_PROJECT_TOOLS, and for the pinned builder: its test runs only where findPython() finds Python 3.11
// with the pinned PyInstaller (OAH_PROJECT_TOOLS_PYTHON or the usual interpreter names).

import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  DESKTOP_DIR,
  PROJECT_TOOLS,
  PROJECT_TOOLS_PYTHON,
  StageError,
  buildProjectTools,
  checkBuilder,
  findPython,
  hostTriple,
  parseArgs,
  pinnedPyInstaller,
  projectToolsName,
  sidecarName,
  stage,
  workspaceVersion,
} from "./stage-cli.mjs";

const REAL_CLI = process.env.OAH_STAGE_TEST_CLI;
const NO_CLI = "OAH_STAGE_TEST_CLI is not set to a built oah binary";
const REAL_TOOLS = process.env.OAH_STAGE_TEST_PROJECT_TOOLS;
const NO_TOOLS = "OAH_STAGE_TEST_PROJECT_TOOLS is not set to a built oah-project-tools binary";
const NO_BUILDER = "no Python 3.11 with the pinned PyInstaller is installed here";

function pinnedBuilder() {
  try {
    return findPython();
  } catch (error) {
    if (error instanceof StageError) return null;
    throw error;
  }
}
const BUILDER = pinnedBuilder();

function tempDir(t) {
  const dir = mkdtempSync(join(tmpdir(), "oah-stage-test-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test("sidecar names follow Tauri's externalBin rule", () => {
  assert.equal(sidecarName("aarch64-apple-darwin"), "oah-aarch64-apple-darwin");
  assert.equal(sidecarName("x86_64-unknown-linux-gnu"), "oah-x86_64-unknown-linux-gnu");
  assert.equal(sidecarName("x86_64-pc-windows-msvc"), "oah-x86_64-pc-windows-msvc.exe");
  assert.equal(sidecarName("aarch64-apple-darwin", PROJECT_TOOLS), "oah-project-tools-aarch64-apple-darwin");
  assert.equal(sidecarName("x86_64-pc-windows-msvc", PROJECT_TOOLS), "oah-project-tools-x86_64-pc-windows-msvc.exe");
});

test("the runner is an externalBin everywhere but on macOS, where the bundle places one fixed file", () => {
  assert.equal(projectToolsName("x86_64-pc-windows-msvc"), "oah-project-tools-x86_64-pc-windows-msvc.exe");
  assert.equal(projectToolsName("x86_64-unknown-linux-gnu"), "oah-project-tools-x86_64-unknown-linux-gnu");
  assert.equal(projectToolsName("aarch64-apple-darwin"), "oah-project-tools-macos");
  assert.equal(projectToolsName("x86_64-apple-darwin"), "oah-project-tools-macos");
  const read = (name) => JSON.parse(readFileSync(join(DESKTOP_DIR, "src-tauri", name), "utf8")).bundle;
  assert.deepEqual(read("tauri.conf.json").externalBin, ["binaries/oah", `binaries/${PROJECT_TOOLS}`]);
  const mac = read("tauri.macos.conf.json");
  assert.deepEqual(mac.externalBin, ["binaries/oah"], "the bundler would sign it with the hardened runtime");
  assert.deepEqual(mac.macOS.files, { [`MacOS/${PROJECT_TOOLS}`]: `binaries/${projectToolsName("aarch64-apple-darwin")}` });
});

test("options are parsed strictly", () => {
  assert.deepEqual(parseArgs(["--cli", "a", "--target", "t", "--out", "o"]), {
    cli: "a",
    projectTools: null,
    python: null,
    target: "t",
    out: "o",
  });
  const tools = parseArgs(["--project-tools", "p", "--python", "y"]);
  assert.deepEqual([tools.projectTools, tools.python, tools.cli], ["p", "y", null]);
  assert.throws(() => parseArgs(["--project-tools"]), StageError);
  assert.throws(() => parseArgs(["--bogus"]), StageError);
  assert.throws(() => parseArgs(["--cli"]), StageError);
  assert.throws(() => parseArgs(["--cli", "--out", "o"]), StageError);
});

test("the workspace version is read from the root manifest", (t) => {
  assert.match(workspaceVersion(), /^\d+\.\d+\.\d+/);
  const manifest = join(tempDir(t), "Cargo.toml");
  writeFileSync(manifest, '[workspace]\nmembers = []\n\n[workspace.package]\nedition = "2024"\nversion = "1.2.3"\n');
  assert.equal(workspaceVersion(manifest), "1.2.3");
  writeFileSync(manifest, '[workspace]\nmembers = []\n[package]\nversion = "9.9.9"\n');
  assert.throws(() => workspaceVersion(manifest), StageError);
});

test("the real CLI is staged under the host sidecar name and prints the workspace version", { skip: !REAL_CLI && NO_CLI }, (t) => {
  const out = tempDir(t);
  const host = hostTriple();
  const result = stage({ source: REAL_CLI, triple: host, host, out, expectedVersion: workspaceVersion() });
  assert.equal(result.path, join(out, sidecarName(host)));
  assert.equal(result.version, `oah ${workspaceVersion()}`);
  if (process.platform !== "win32") {
    assert.equal(statSync(result.path).mode & 0o777, 0o755);
  }
  assert.deepEqual(readdirSync(out), [sidecarName(host)], "no temporary file is left");
});

test("staging the real CLI again replaces the file and still checks it", { skip: !REAL_CLI && NO_CLI }, (t) => {
  const out = tempDir(t);
  const host = hostTriple();
  const args = { source: REAL_CLI, triple: host, host, out, expectedVersion: workspaceVersion() };
  stage(args);
  const again = stage(args);
  assert.equal(again.version, `oah ${workspaceVersion()}`);
  assert.deepEqual(readdirSync(out), [sidecarName(host)]);
});

test("a CLI that prints another version is refused and nothing is staged", { skip: !REAL_CLI && NO_CLI }, (t) => {
  const out = tempDir(t);
  const host = hostTriple();
  assert.throws(
    () => stage({ source: REAL_CLI, triple: host, host, out, expectedVersion: "999.0.0" }),
    (error) => error instanceof StageError && /did not print "oah 999\.0\.0"/.test(error.message),
  );
  assert.deepEqual(readdirSync(out), []);
});

test("a missing built CLI is refused and nothing is staged", (t) => {
  const out = tempDir(t);
  const host = hostTriple();
  assert.throws(
    () => stage({ source: join(out, "absent"), triple: host, host, out, expectedVersion: "0.0.0" }),
    (error) => error instanceof StageError && /does not exist/.test(error.message),
  );
  assert.equal(existsSync(join(out, sidecarName(host))), false);
});

test("a cross target is staged without running it", { skip: !REAL_CLI && NO_CLI }, (t) => {
  const out = tempDir(t);
  const host = hostTriple();
  const other = host.includes("windows") ? "aarch64-apple-darwin" : "x86_64-pc-windows-msvc";
  const result = stage({ source: REAL_CLI, triple: other, host, out, expectedVersion: "999.0.0" });
  assert.equal(result.version, null);
  assert.deepEqual(readdirSync(out), [sidecarName(other)]);
});

test("the PyInstaller pin is read from the hash-locked requirements", (t) => {
  assert.match(pinnedPyInstaller(), /^\d+\.\d+(\.\d+)*$/);
  const file = join(tempDir(t), "requirements.txt");
  const hash = "    --hash=sha256:0000000000000000000000000000000000000000000000000000000000000000\n";
  writeFileSync(
    file,
    `altgraph==0.17.5 \\\n${hash}pyinstaller-hooks-contrib==2026.1 \\\n${hash}pyinstaller==6.1.2 \\\n${hash}`,
  );
  assert.equal(pinnedPyInstaller(file), "6.1.2");
  writeFileSync(file, "pyinstaller-hooks-contrib==2026.1\npyinstaller>=6\n");
  assert.throws(() => pinnedPyInstaller(file), StageError);
});

test("an interpreter that is not the pinned builder is refused", (t) => {
  const absent = join(tempDir(t), "no-python");
  assert.throws(() => checkBuilder(absent), StageError);
  assert.throws(() => checkBuilder(process.execPath), StageError, "node is not Python with PyInstaller");
  assert.throws(() => findPython(absent), StageError);
});

test("the pinned builder is accepted and any other pin is refused", { skip: !BUILDER && NO_BUILDER }, () => {
  const pinned = `${PROJECT_TOOLS_PYTHON} ${pinnedPyInstaller()}`;
  assert.equal(checkBuilder(BUILDER, pinned), BUILDER);
  for (const other of [`${PROJECT_TOOLS_PYTHON} 0.0.0`, `3.10 ${pinnedPyInstaller()}`]) {
    assert.throws(
      () => checkBuilder(BUILDER, other),
      (error) => error instanceof StageError && error.message.includes(`(${pinned})`),
    );
    assert.throws(() => findPython(BUILDER, other), StageError);
  }
});

test("the project-tools runner is not built for another platform", () => {
  const host = hostTriple();
  const other = host.includes("windows") ? "aarch64-apple-darwin" : "x86_64-pc-windows-msvc";
  assert.throws(
    () => buildProjectTools({ python: join("absent", "python"), triple: other, host }),
    (error) => error instanceof StageError && /--project-tools/.test(error.message),
  );
});

test("the CLI is refused under the project-tools name and nothing is staged", { skip: !REAL_CLI && NO_CLI }, (t) => {
  const out = tempDir(t);
  const host = hostTriple();
  const expectedVersion = workspaceVersion();
  assert.throws(
    () => stage({ source: REAL_CLI, triple: host, host, out, expectedVersion, name: PROJECT_TOOLS }),
    (error) => error instanceof StageError && error.message.includes(`did not print "${PROJECT_TOOLS} ${expectedVersion}"`),
  );
  assert.deepEqual(readdirSync(out), []);
});

test("the real runner is staged under its sidecar name and prints the workspace version", { skip: !REAL_TOOLS && NO_TOOLS }, (t) => {
  const out = tempDir(t);
  const host = hostTriple();
  const fileName = projectToolsName(host);
  const args = { source: REAL_TOOLS, triple: host, host, out, expectedVersion: workspaceVersion(), name: PROJECT_TOOLS, fileName };
  const result = stage(args);
  assert.equal(result.path, join(out, fileName));
  assert.equal(result.version, `${PROJECT_TOOLS} ${workspaceVersion()}`);
  assert.equal(stage(args).version, result.version, "staging again replaces the file and still checks it");
  assert.deepEqual(readdirSync(out), [fileName], "no temporary file is left");
  assert.throws(
    () => stage({ ...args, expectedVersion: "999.0.0" }),
    (error) => error instanceof StageError && /did not print "oah-project-tools 999\.0\.0"/.test(error.message),
  );
  assert.throws(() => stage({ ...args, name: "oah" }), StageError, "the runner is refused under the CLI's name");
  assert.deepEqual(readdirSync(out), [fileName], "a refused candidate replaces nothing");
});
