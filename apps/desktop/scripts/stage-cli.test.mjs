// stage-cli 테스트: 실제로 빌드된 oah CLI 를 임시 폴더에 놓아 보고, 잘못된 후보는 남지 않는지 본다.
//
// Run: OAH_STAGE_TEST_CLI=<built oah binary> node --test apps/desktop/scripts/
// The staging tests need an oah CLI that was really built from this workspace; without OAH_STAGE_TEST_CLI they
// are reported as skipped, never as passed.

import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  StageError,
  hostTriple,
  parseArgs,
  sidecarName,
  stage,
  workspaceVersion,
} from "./stage-cli.mjs";

const REAL_CLI = process.env.OAH_STAGE_TEST_CLI;
const NO_CLI = "OAH_STAGE_TEST_CLI is not set to a built oah binary";

function tempDir(t) {
  const dir = mkdtempSync(join(tmpdir(), "oah-stage-test-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test("sidecar names follow Tauri's externalBin rule", () => {
  assert.equal(sidecarName("aarch64-apple-darwin"), "oah-aarch64-apple-darwin");
  assert.equal(sidecarName("x86_64-unknown-linux-gnu"), "oah-x86_64-unknown-linux-gnu");
  assert.equal(sidecarName("x86_64-pc-windows-msvc"), "oah-x86_64-pc-windows-msvc.exe");
});

test("options are parsed strictly", () => {
  assert.deepEqual(parseArgs(["--cli", "a", "--target", "t", "--out", "o"]), { cli: "a", target: "t", out: "o" });
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
