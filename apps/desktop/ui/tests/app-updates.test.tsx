import { afterEach, expect, it, vi } from "vitest";
import { AppUpdates } from "../src/appUpdates";
import { button, byText, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport } from "./support/fakeTransport";

afterEach(async () => {
  await unmount();
  vi.useRealTimers();
});

it("offers an update on startup and waits for the user to install the displayed version", async () => {
  vi.useFakeTimers();
  const transport = createFakeTransport();
  transport.override.checkAppUpdate = async () => ({ version: "1.3.0" });
  const install = vi.fn(async (): Promise<void> => {});
  transport.override.installAppUpdate = install;
  await mount(<AppUpdates transport={transport} />);

  await emit(() => { vi.advanceTimersByTime(0); });
  expect(byText("strong", "OAH v1.3.0 is available")).not.toBeNull();
  expect(install).not.toHaveBeenCalled();

  await click(button("Install and restart"));
  expect(install).toHaveBeenCalledExactlyOnceWith("1.3.0");
  expect(button("Install and restart").disabled).toBe(true);
});

it("keeps the app usable when the feed is offline and allows a manual retry", async () => {
  vi.useFakeTimers();
  const transport = createFakeTransport();
  transport.override.checkAppUpdate = async () => {
    throw { code: "OAH_APP_UPDATE_CHECK_FAILED", message: "OAH could not check for updates.", hint: null };
  };
  await mount(<AppUpdates transport={transport} />);
  await emit(() => { vi.advanceTimersByTime(0); });
  expect(byText("span", "OAH could not check for updates.")).toBeNull();

  await click(button("Check for app updates"));
  expect(byText("span", "OAH could not check for updates.")).not.toBeNull();
  transport.override.checkAppUpdate = async () => null;
  await click(button("Check for app updates"));
  expect(byText('[role="status"]', "OAH is up to date.")).not.toBeNull();
});

it("leaves update checking available after an install failure", async () => {
  vi.useFakeTimers();
  const transport = createFakeTransport();
  transport.override.checkAppUpdate = async () => ({ version: "1.3.0" });
  transport.override.installAppUpdate = async () => {
    throw {
      code: "OAH_APP_UPDATE_INSTALL_FAILED",
      message: "OAH could not install the update.",
      hint: "Retry or use the installer from GitHub Releases.",
    };
  };
  await mount(<AppUpdates transport={transport} />);
  await emit(() => { vi.advanceTimersByTime(0); });
  await click(button("Install and restart"));
  expect(byText("span", /Retry or use the installer/)).not.toBeNull();
  await click(button("Check for app updates"));
  expect(button("Install and restart").disabled).toBe(false);
});
