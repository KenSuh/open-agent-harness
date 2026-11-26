import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const desktop = fileURLToPath(new URL("../", import.meta.url));
const [directory, head] = process.argv.slice(2);
const signingKey = process.env.TAURI_SIGNING_PRIVATE_KEY_PATH;
if (process.platform !== "darwin" || !directory || !/^[a-f0-9]{40}$/.test(head ?? "") || !signingKey) {
  throw new Error("On macOS, set TAURI_SIGNING_PRIVATE_KEY_PATH and pass the installer directory and full source SHA.");
}

const assets = resolve(directory);
const version = JSON.parse(readFileSync(join(desktop, "package.json"), "utf8")).version;
const config = JSON.parse(readFileSync(join(desktop, "src-tauri", "tauri.conf.json"), "utf8"));
if (!/^\d+\.\d+\.\d+$/.test(version) ||
    readFileSync(signingKey + ".pub", "utf8").trim() !== config.plugins.updater.pubkey) {
  throw new Error("The release version or signing key does not match the app configuration.");
}

const apps = {
  dmg: "OAH_" + version + "_aarch64.dmg",
  setup: "OAH_" + version + "_x64-setup.exe",
  update: "OAH_" + version + "_aarch64.app.tar.gz",
};
const source = readFileSync(join(assets, "SOURCE.txt"), "utf8").split(/\r?\n/);
if (!source.includes("commit: " + head)) {
  throw new Error("The installers were not built from the requested commit.");
}
const sums = readFileSync(join(assets, "SHA256SUMS"), "utf8").split(/\r?\n/);
for (const file of [apps.dmg, apps.setup]) {
  const digest = createHash("sha256").update(readFileSync(join(assets, file))).digest("hex");
  if (!sums.includes(digest + "  " + file)) {
    throw new Error("The installer checksum differs from CI for " + file);
  }
}

const mountRoot = mkdtempSync(join(assets, ".oah-mount-"));
const mount = join(mountRoot, "disk");
mkdirSync(mount);
let attached = false;
try {
  execFileSync("hdiutil", ["attach", "-readonly", "-nobrowse", "-mountpoint", mount, join(assets, apps.dmg)]);
  attached = true;
  const app = join(mount, "OAH.app");
  execFileSync("codesign", ["--verify", "--deep", "--strict", app]);
  const appVersion = execFileSync("/usr/libexec/PlistBuddy", [
    "-c", "Print :CFBundleShortVersionString", join(app, "Contents", "Info.plist"),
  ], { encoding: "utf8" }).trim();
  if (appVersion !== version) throw new Error("The DMG contains a different version.");
  execFileSync("tar", ["-czf", join(assets, apps.update), "-C", mount, "OAH.app"], {
    env: { ...process.env, COPYFILE_DISABLE: "1" },
  });
} finally {
  if (attached) execFileSync("hdiutil", ["detach", mount]);
  rmSync(mountRoot, { recursive: true });
}

const cli = join(desktop, "node_modules", ".bin", "tauri");
const releaseUrl = "https://github.com/KenSuh/open-agent-harness/releases/download/v" + version + "/";
const platforms = {};
for (const [platform, file] of [["darwin-aarch64", apps.update], ["windows-x86_64", apps.setup]]) {
  const asset = join(assets, file);
  execFileSync(cli, ["signer", "sign", "--app-version", version, asset]);
  platforms[platform] = {
    url: releaseUrl + file,
    signature: readFileSync(asset + ".sig", "utf8").trim(),
  };
}
writeFileSync(join(assets, "latest.json"), JSON.stringify({ version, platforms }, null, 2) + "\n", { flag: "wx" });
console.log("Prepared signed OAH v" + version + " updates for macOS arm64 and Windows x64.");
