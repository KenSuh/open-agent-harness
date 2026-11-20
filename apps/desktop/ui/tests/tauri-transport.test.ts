// 한국어: 운영 Tauri 전송 계층 단독 테스트 — @tauri-apps/api 를 가짜로 바꿔 명령 이름·req 형태만 확인한다(네이티브 통합 아님).
import { beforeEach, describe, expect, it, vi } from "vitest";

const invoke = vi.fn();
class FakeChannel {
  onmessage: ((message: unknown) => void) | null = null;
}
const listen = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({ invoke, Channel: FakeChannel }));
vi.mock("@tauri-apps/api/event", () => ({ listen }));

const { createTauriTransport } = await import("../src/transport/tauri");

beforeEach(() => {
  invoke.mockReset();
  listen.mockReset();
});

describe("tauri transport (standalone, mocked @tauri-apps/api)", () => {
  it("calls the exact M1-S1 command names with a single req argument", async () => {
    invoke.mockResolvedValue(null);
    const t = createTauriTransport();
    await t.appInfo();
    await t.projectOpenDialog();
    await t.projectCurrent();
    await t.tasksList();
    await t.settingsGet();
    await t.taskCancel("abc");
    await t.settingsSet([{ key: "ui.theme", value: "dark" }]);
    expect(invoke.mock.calls).toEqual([
      ["app_info", { req: {} }],
      ["project_open_dialog", { req: {} }],
      ["project_current", { req: {} }],
      ["tasks_list", { req: {} }],
      ["settings_get", { req: {} }],
      ["task_cancel", { req: { id: "abc" } }],
      ["settings_set", { req: { edits: [{ key: "ui.theme", value: "dark" }] } }],
    ]);
  });

  it("passes a Channel to start_refresh and forwards its messages", async () => {
    invoke.mockResolvedValue("task-1");
    const received: unknown[] = [];
    const id = await createTauriTransport().startRefresh((event) => received.push(event));
    expect(id).toBe("task-1");
    const [command, args] = invoke.mock.calls[0]!;
    expect(command).toBe("start_refresh");
    expect(args.req).toEqual({});
    args.onEvent.onmessage({ event: "cancelled" });
    expect(received).toEqual([{ event: "cancelled" }]);
  });

  it("maps a rejected invoke carrying an ErrorDto and any other rejection to ErrorDto", async () => {
    const t = createTauriTransport();
    invoke.mockRejectedValueOnce({ code: "OAH_TASK_BUSY", message: "busy", hint: "wait" });
    await expect(t.appInfo()).rejects.toEqual({ code: "OAH_TASK_BUSY", message: "busy", hint: "wait" });
    invoke.mockRejectedValueOnce("command not found");
    await expect(t.appInfo()).rejects.toMatchObject({ code: "UI_TRANSPORT_ERROR", message: "command not found" });
  });

  it("subscribes to the notice and scan-changed events and unwraps payloads", async () => {
    const unlisten = vi.fn();
    listen.mockResolvedValue(unlisten);
    const t = createTauriTransport();
    const notices: unknown[] = [];
    await t.onNotice((n) => notices.push(n));
    await t.onScanChanged(() => undefined);
    expect(listen.mock.calls.map((c) => c[0])).toEqual(["notice", "scan-changed"]);
    listen.mock.calls[0]![1]({ payload: { id: "n1" } });
    expect(notices).toEqual([{ id: "n1" }]);
  });
});
