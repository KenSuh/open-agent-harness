// 한국어: 설정(테마·빠른 시작) 동작 테스트 — settings_set 으로만 저장되는지 확인한다 (독립 테스트 전송 계층 사용).
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { button, click, emit, flush, mount, unmount } from "./support/dom";
import { createFakeTransport, sampleSettings } from "./support/fakeTransport";
import type { SettingsDto } from "../src/contract";

afterEach(async () => {
  await unmount();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("settings: theme and quick start (persisted only through settings_set)", () => {
  it("saves the theme through settings_set and applies the returned value", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    const select = document.querySelector("select") as HTMLSelectElement;
    await emit(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!;
      setter.call(select, "dark");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    expect(t.settingsEdits).toEqual([[{ key: "ui.theme", value: "dark" }]]);
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(setItem).not.toHaveBeenCalled(); // no web storage persistence
  });

  it("keeps the old theme and reports the error when saving fails", async () => {
    const t = createFakeTransport();
    t.override.settingsSet = async () => {
      throw { code: "OAH_FILE_BUSY", message: "settings.toml is busy.", hint: "Try again." };
    };
    await mount(<App transport={t} />);
    const select = document.querySelector("select") as HTMLSelectElement;
    await emit(() => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, "light");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(document.documentElement.dataset.theme).toBe("auto");
    expect(document.querySelector(".toast-stack")?.textContent).toMatch(/OAH_FILE_BUSY/);
  });

  it("dismisses the quick-start strip through settings_set and hides it when already dismissed", async () => {
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    expect(document.querySelector(".quick-start")).not.toBeNull();
    await click(button("Dismiss"));
    expect(t.settingsEdits).toEqual([[{ key: "ui.quick_start_dismissed", value: true }]]);
    expect(document.querySelector(".quick-start")).toBeNull();
    await unmount();

    const dismissed: SettingsDto = { ...sampleSettings, ui: { theme: "light", quick_start_dismissed: true } };
    const t2 = createFakeTransport();
    t2.override.settingsGet = async () => dismissed;
    await mount(<App transport={t2} />);
    expect(document.querySelector(".quick-start")).toBeNull();
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("shows a settings load error with retry", async () => {
    const t = createFakeTransport();
    t.override.settingsGet = async () => {
      throw { code: "OAH_CONFIG_INVALID", message: "settings.toml is invalid.", hint: null };
    };
    await mount(<App transport={t} />);
    expect(document.querySelector('[role="alert"]')?.textContent).toMatch(/OAH_CONFIG_INVALID/);
    expect(document.querySelector("select")?.disabled).toBe(true);
  });
});
