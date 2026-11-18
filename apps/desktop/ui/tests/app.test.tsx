// 한국어: App 렌더링·키보드·로딩/오류·설정 동작 테스트 (독립 테스트 전송 계층 사용 — 네이티브 통합 동작 아님).
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { button, byText, click, emit, flush, mount, press, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleProject, sampleSettings } from "./support/fakeTransport";
import type { ProjectDto, SettingsDto } from "../src/contract";

afterEach(async () => {
  await unmount();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("layout A (standalone test transport)", () => {
  it("renders title bar, rail entries, honest empty pages and no later-slice controls", async () => {
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    expect(document.querySelector("header.titlebar")).not.toBeNull();
    const rail = Array.from(document.querySelectorAll("nav.rail button")).map((n) => n.textContent);
    expect(rail).toEqual(["Explore", "Installed", "Team Workflow"]);
    expect(document.querySelector("h1")?.textContent).toBe("Explore");
    expect(document.querySelector(".empty")?.textContent).toMatch(/Nothing to show/);
    await click(button("Installed"));
    expect(document.querySelector("h1")?.textContent).toBe("Installed");
    expect(document.querySelector("nav.rail .active")?.textContent).toBe("Installed");
    await click(button("Team Workflow"));
    expect(document.querySelector("h1")?.textContent).toBe("Team Workflow");
    expect(document.body.textContent).not.toMatch(/Safety Checks|simulat/i);
    expect(document.body.textContent).toMatch(/Version 0\.0\.0 · macos · CLI copy: copied/);
    expect(t.calls).toEqual(expect.arrayContaining(["app_info", "settings_get", "project_current", "tasks_list"]));
  });

  it("shows loading then an app_info error with code, hint and a working retry", async () => {
    const t = createFakeTransport();
    const pending = deferred<never>();
    t.override.appInfo = () => pending.promise;
    await mount(<App transport={t} />);
    expect(document.body.textContent).toMatch(/Loading app information/);
    await emit(() => pending.reject({ code: "OAH_FILE_IO", message: "Could not read ~/.oah.", hint: "Check permissions." }));
    const alert = document.querySelector('[role="alert"]');
    expect(alert?.textContent).toMatch(/OAH_FILE_IO/);
    expect(alert?.textContent).toMatch(/Check permissions\./);
    t.override.appInfo = undefined;
    await click(button("Retry"));
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(document.body.textContent).toMatch(/Version 0\.0\.0/);
  });
});

describe("project open", () => {
  it("shows the opened project, keeps state when the picker is cancelled", async () => {
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    expect(document.querySelector(".project-name")?.textContent).toBe("No project open");
    t.override.projectOpenDialog = async () => null;
    await click(button("+ Open…"));
    expect(document.querySelector(".project-name")?.textContent).toBe("No project open");
    t.override.projectOpenDialog = async () => sampleProject;
    await click(button("+ Open…"));
    expect(document.querySelector(".project-name")?.textContent).toBe("app");
    expect(document.querySelector(".project-card")?.textContent).toMatch(/Git work tree · config: legacy/);
  });

  it("surfaces an invalid project config without hiding the project", async () => {
    const invalid: ProjectDto = {
      ...sampleProject,
      config: { state: "invalid", error: { code: "OAH_CONFIG_INVALID", message: "Bad value.", hint: null } },
    };
    await mount(<App transport={createFakeTransport({ project: invalid })} />);
    expect(document.querySelector(".project-name")?.textContent).toBe("app");
    expect(document.querySelector('[role="alert"]')?.textContent).toMatch(/OAH_CONFIG_INVALID/);
  });

  it("toasts a typed error when the dialog command fails", async () => {
    const t = createFakeTransport();
    t.override.projectOpenDialog = async () => {
      throw { code: "OAH_PROJECT_NOT_FOLDER", message: "Not a folder.", hint: null };
    };
    await mount(<App transport={t} />);
    await click(button("+ Open…"));
    expect(document.querySelector(".toast-stack")?.textContent).toMatch(/OAH_PROJECT_NOT_FOLDER: Not a folder\./);
  });
});

describe("refresh task and Cmd/Ctrl+R", () => {
  it("runs a refresh from Ctrl+R and Cmd+R, shows progress and real counts, blocks a second start", async () => {
    const t = createFakeTransport({ project: sampleProject });
    const id = deferred<string>();
    t.override.startRefresh = () => id.promise;
    await mount(<App transport={t} />);
    const notCancelled = await press({ key: "r", ctrlKey: true });
    expect(notCancelled).toBe(false); // default webview reload is prevented
    expect(t.calls.filter((c) => c === "start_refresh")).toHaveLength(1);
    await press({ key: "r", metaKey: true });
    expect(t.calls.filter((c) => c === "start_refresh")).toHaveLength(1); // joins the running one
    expect(document.querySelector(".task-bar")?.textContent).toMatch(/Refreshing…/);
    await emit(() => id.resolve("task-9"));
    await emit(() => t.emitRefresh({ event: "progress", data: { phase: "read-project", done: 1, total: 1 } }));
    expect(document.querySelector(".task-bar")?.textContent).toMatch(/read-project\) 1 \/ 1/);
    await emit(() => t.emitRefresh({ event: "done", data: { projects: 1, notices: 0 } }));
    expect(document.querySelector(".task-bar")).toBeNull();
    expect(document.querySelector('[role="status"][aria-live="polite"]')?.textContent).toMatch(
      /Refreshed: 1 project\(s\), 0 notice\(s\)\./,
    );
  });

  it("cancels a running refresh and shows the cancelled state", async () => {
    const t = createFakeTransport({ project: sampleProject });
    await mount(<App transport={t} />);
    await click(button(/Refresh/));
    await click(button("Cancel"));
    expect(t.calls).toContain("task_cancel:task-1");
    await emit(() => t.emitRefresh({ event: "cancelled" }));
    expect(document.querySelector(".toast-stack")?.textContent).toMatch(/Refresh cancelled\./);
    expect(button(/Refresh/).disabled).toBe(false);
  });

  it("shows a failed task and a refused start (OAH_TASK_BUSY) as errors", async () => {
    const t = createFakeTransport({ project: sampleProject });
    await mount(<App transport={t} />);
    await click(button(/Refresh/));
    await emit(() => t.emitRefresh({ event: "failed", data: { code: "OAH_FILE_IO", message: "Read failed.", hint: null } }));
    expect(document.querySelector(".toast-stack")?.textContent).toMatch(/OAH_FILE_IO: Read failed\./);
    t.override.startRefresh = async () => {
      throw { code: "OAH_TASK_BUSY", message: "A refresh is already running.", hint: null };
    };
    await click(button(/Refresh/));
    expect(document.querySelector(".toast-stack")?.textContent).toMatch(/OAH_TASK_BUSY/);
    expect(button(/Refresh/).disabled).toBe(false);
  });

  it("tells the person to open a project when Ctrl+R is pressed without one", async () => {
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    await press({ key: "r", ctrlKey: true });
    expect(t.calls).not.toContain("start_refresh");
    expect(document.querySelector(".toast-stack")?.textContent).toMatch(/Open a project folder first/);
  });

  it("re-shows a task that is still running after a webview reload", async () => {
    const t = createFakeTransport({
      project: sampleProject,
      tasks: [{ id: "abc", kind: "refresh", state: "running", progress: { phase: "read-project", done: 0, total: 1 } }],
    });
    await mount(<App transport={t} />);
    expect(document.querySelector(".task-bar")?.textContent).toMatch(/read-project\) 0 \/ 1/);
    await click(button("Cancel"));
    expect(t.calls).toContain("task_cancel:abc");
  });
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

describe("events and live region", () => {
  it("renders notice events as notices and toasts in a polite live region", async () => {
    const t = createFakeTransport({ project: sampleProject });
    await mount(<App transport={t} />);
    const live = document.querySelector(".toast-stack");
    expect(live?.getAttribute("role")).toBe("status");
    expect(live?.getAttribute("aria-live")).toBe("polite");
    await emit(() =>
      t.emitNotice({ id: "n1", level: "warning", code: "OAH_CLI_COPY_FAILED", message: "CLI copy failed.", hint: null, actions: [] }),
    );
    expect(byText("li", /OAH_CLI_COPY_FAILED/)).not.toBeNull();
    expect(live?.textContent).toMatch(/CLI copy failed\./);
  });

  it("answers scan-changed by re-reading the current project", async () => {
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    const before = t.calls.filter((c) => c === "project_current").length;
    await emit(() => t.emitScan({ scope: "all", keys: [] }));
    expect(t.calls.filter((c) => c === "project_current").length).toBe(before + 1);
  });
});
