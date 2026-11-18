// 데스크톱 시작 점검: 빌드된 앱을 빈 OAH_HOME 으로 두 번 띄워 CLI 복사본과 그 버전, 두 번째 시작의 무변경을 본다.
//
// Usage: node apps/desktop/scripts/smoke-start.mjs --app <built oah-desktop binary> [--timeout <seconds>]
//
// Used by the manual desktop workflow job on macOS and Windows. It starts the app with OAH_HOME set to a new
// empty temporary folder, waits for OAH_HOME/bin/oah[.exe] to appear, runs it with --version and requires
// `oah <workspace version>`, and checks the app is still running. It stops the app, starts it again, and requires
// the copied file to keep its bytes and modification time. It prints one JSON summary line and exits non-zero on
// any failure. It observes the app; it does not stand in for the real-machine checks of a person using it.

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { pathToFileURL } from "node:url";

import { workspaceVersion } from "./stage-cli.mjs";

const SECOND_START_SETTLE_MS = 10_000;

function parseArgs(argv) {
  const options = { app: null, timeout: 90 };
  for (let i = 0; i < argv.length; i += 2) {
    const [name, value] = [argv[i], argv[i + 1]];
    if (name === "--app" && value) options.app = resolve(value);
    else if (name === "--timeout" && value && Number(value) > 0) options.timeout = Number(value);
    else throw new Error(`unknown or incomplete option ${name}`);
  }
  if (!options.app) throw new Error("--app is required");
  return options;
}

function sha256(file) {
  return createHash("sha256").update(readFileSync(file)).digest("hex");
}

function startApp(app, oahHome) {
  const child = spawn(app, [], {
    env: { ...process.env, OAH_HOME: oahHome },
    stdio: ["ignore", "inherit", "inherit"],
    shell: false,
  });
  const state = { exited: false, code: null };
  child.on("exit", (code) => Object.assign(state, { exited: true, code }));
  child.on("error", (error) => Object.assign(state, { exited: true, code: `spawn error: ${error.message}` }));
  return { child, state };
}

async function stopApp({ child, state }) {
  if (state.exited) return;
  child.kill();
  for (let i = 0; i < 50 && !state.exited; i += 1) await sleep(100);
  if (!state.exited) child.kill("SIGKILL");
}

async function waitFor(predicate, timeoutMs, running) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    if (running.state.exited) return false;
    await sleep(250);
  }
  return predicate();
}

export async function smoke({ app, timeout }) {
  const oahHome = mkdtempSync(join(tmpdir(), "oah-smoke-home-"));
  const cli = join(oahHome, "bin", process.platform === "win32" ? "oah.exe" : "oah");
  const expected = `oah ${workspaceVersion()}`;
  const summary = { app, oah_home_was_empty: true, cli_path: cli, first: {}, second: {}, ok: false };
  try {
    const first = startApp(app, oahHome);
    try {
      const appeared = await waitFor(() => existsSync(cli), timeout * 1000, first);
      summary.first.cli_appeared = appeared;
      if (!appeared) throw new Error(`the app did not create ${cli} (app exited: ${first.state.exited}, code ${first.state.code})`);
      await sleep(1000);
      const run = spawnSync(cli, ["--version"], { encoding: "utf8", timeout: 10_000, shell: false });
      summary.first.version = (run.stdout ?? "").trim();
      if (run.status !== 0 || summary.first.version !== expected) {
        throw new Error(`the copied CLI printed "${summary.first.version}" (exit ${run.status}); expected "${expected}"`);
      }
      summary.first.app_running = !first.state.exited;
      if (first.state.exited) throw new Error(`the app exited after the copy (code ${first.state.code})`);
    } finally {
      await stopApp(first);
    }
    const before = { sha256: sha256(cli), mtime_ms: statSync(cli).mtimeMs };
    const second = startApp(app, oahHome);
    try {
      await sleep(SECOND_START_SETTLE_MS);
      summary.second.app_running = !second.state.exited;
      if (second.state.exited) throw new Error(`the second start exited (code ${second.state.code})`);
    } finally {
      await stopApp(second);
    }
    const after = { sha256: sha256(cli), mtime_ms: statSync(cli).mtimeMs };
    summary.second.unchanged = before.sha256 === after.sha256 && before.mtime_ms === after.mtime_ms;
    if (!summary.second.unchanged) throw new Error("the second start changed the copied CLI");
    summary.ok = true;
    return summary;
  } catch (error) {
    summary.error = error.message;
    return summary;
  } finally {
    rmSync(oahHome, { recursive: true, force: true });
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const summary = await smoke(parseArgs(process.argv.slice(2)));
  console.log(JSON.stringify(summary));
  process.exit(summary.ok ? 0 : 1);
}
