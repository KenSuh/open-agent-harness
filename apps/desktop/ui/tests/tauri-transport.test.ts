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
  it("calls apply_project_skill with the request fields under req", async () => {
    invoke.mockResolvedValue({ status: "installed", review_id: "rec-1" });
    const req = { project: "p1", engine: "codex" as const, review_id: "rec-1", expected_review_id: null, acknowledge_warnings: false };
    expect(await createTauriTransport().applyProjectSkill(req)).toEqual({ status: "installed", review_id: "rec-1" });
    expect(invoke.mock.calls).toEqual([["apply_project_skill", { req }]]);
    invoke.mockRejectedValue({ code: "OAH_X", message: "no", hint: null });
    await expect(createTauriTransport().applyProjectSkill(req)).rejects.toEqual({ code: "OAH_X", message: "no", hint: null });
  });

  it("calls project_tools_status with the project id under req", async () => {
    const answer = { project_id: "p1", tools: [] };
    invoke.mockResolvedValue(answer);
    const req = { project_id: "p1", input: {} };
    expect(await createTauriTransport().projectToolsStatus(req)).toEqual(answer);
    expect(invoke.mock.calls).toEqual([["project_tools_status", { req }]]);
  });

  it("calls project_tools_plan and project_tools_apply with the project id and input under req", async () => {
    invoke.mockResolvedValue(null);
    const t = createTauriTransport();
    const edits = [{ kind: "wiki-dir" as const, value: "notes" }];
    await t.projectToolsPlan({ project_id: "p1", input: { edits } });
    await t.projectToolsApply({ project_id: "p1", input: { edits, plan_id: "plan-1", approve_upgrade: "abc" } });
    expect(invoke.mock.calls).toEqual([
      ["project_tools_plan", { req: { project_id: "p1", input: { edits } } }],
      ["project_tools_apply", { req: { project_id: "p1", input: { edits, plan_id: "plan-1", approve_upgrade: "abc" } } }],
    ]);
  });

  it("calls the Project Wiki commands with the project id and input under req, and a Channel for the apply", async () => {
    invoke.mockResolvedValue(null);
    const t = createTauriTransport();
    const request = { action: "setup" as const, selected: "notes", today: null };
    await t.projectWikiOverview({ project_id: "p1", input: {} });
    await t.projectWikiPlan({ project_id: "p1", input: request });
    await t.projectWikiSearch({ project_id: "p1", input: { dir: null, query: "plan", max_hits: null } });
    expect(invoke.mock.calls).toEqual([
      ["project_wiki_overview", { req: { project_id: "p1", input: {} } }],
      ["project_wiki_plan", { req: { project_id: "p1", input: request } }],
      ["project_wiki_search", { req: { project_id: "p1", input: { dir: null, query: "plan", max_hits: null } } }],
    ]);
    invoke.mockReset();
    invoke.mockResolvedValue("task-3");
    const received: unknown[] = [];
    const apply = { project_id: "p1", input: { request: { ...request, today: "2026-01-02" }, plan_id: "sha256:abc" } };
    expect(await t.startProjectWikiApply(apply, (event) => received.push(event))).toBe("task-3");
    const [command, args] = invoke.mock.calls[0]!;
    expect(command).toBe("start_project_wiki_apply");
    expect(args.req).toEqual(apply);
    expect(args.onEvent).toBeInstanceOf(FakeChannel);
    args.onEvent.onmessage({ event: "cancelled" });
    expect(received).toEqual([{ event: "cancelled" }]);
  });

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

  it("calls start_review with the spec under req and a Channel, omitting optional fields", async () => {
    invoke.mockResolvedValue("task-9");
    const received: unknown[] = [];
    const id = await createTauriTransport().startReview({ spec: { repo: "example/skills", path: "skills/tidy" } }, (event) => received.push(event));
    expect(id).toBe("task-9");
    const [command, args] = invoke.mock.calls[0]!;
    expect(command).toBe("start_review");
    expect(args.req).toEqual({ spec: { repo: "example/skills", path: "skills/tidy" } });
    expect(args.onEvent).toBeInstanceOf(FakeChannel);
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

  it("calls the M1-S2 agents commands with one req argument and a Channel for the probe", async () => {
    const { default: probeRequest } = await import("../../../../crates/oah-core/tests/dto_samples/agents/probe-request.json");
    invoke.mockResolvedValueOnce([]).mockResolvedValueOnce("task-7");
    const t = createTauriTransport();
    expect(await t.agentsList()).toEqual([]);
    const received: unknown[] = [];
    const req = probeRequest as { agents: ["claude", "codex"]; sign_in: boolean; focus: boolean };
    expect(await t.startAgentsProbe(req, (event) => received.push(event))).toBe("task-7");
    expect(invoke.mock.calls[0]).toEqual(["agents_list", { req: {} }]);
    const [command, args] = invoke.mock.calls[1]!;
    expect(command).toBe("start_agents_probe");
    expect(args.req).toEqual({ agents: ["claude", "codex"], sign_in: true, focus: false });
    expect(args.onEvent).toBeInstanceOf(FakeChannel);
    args.onEvent.onmessage({ event: "done", data: [] });
    expect(received).toEqual([{ event: "done", data: [] }]);
  });

  it("subscribes to agents-changed and unwraps its payload", async () => {
    listen.mockResolvedValue(vi.fn());
    const changes: unknown[] = [];
    await createTauriTransport().onAgentsChanged((change) => changes.push(change));
    expect(listen.mock.calls[0]![0]).toBe("agents-changed");
    listen.mock.calls[0]![1]({ payload: { agents: ["claude", "codex"] } });
    expect(changes).toEqual([{ agents: ["claude", "codex"] }]);
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
