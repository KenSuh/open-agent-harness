// 시작 포커스 확인의 종료 감지와 작업 조회 수명을 가짜 시계로 검증한다.
import { act } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { App } from "../src/App";
import type { ErrorDto, TaskSnapshotDto } from "../src/contract";
import { button, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleAgents } from "./support/fakeTransport";

const nativeFocus: TaskSnapshotDto = {
  id: "native-focus",
  kind: "agents-probe",
  state: "running",
  progress: null,
};
const unavailable: ErrorDto = { code: "OAH_FILE_IO", message: "Could not list running tasks.", hint: null };
const rosterText = () => document.querySelector("section.roster")?.textContent;
const toastText = () => document.querySelector(".toast-stack")?.textContent;

function busyStartup() {
  const transport = createFakeTransport();
  transport.tasksList = async () => [nativeFocus];
  transport.override.startAgentsProbe = async (req) => {
    if (req.focus) {
      throw { code: "OAH_TASK_BUSY", message: "An agents probe is already running.", hint: null } satisfies ErrorDto;
    }
    return "manual-rescan";
  };
  return transport;
}

function advance(ms: number) {
  return act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(async () => {
  await unmount();
  vi.useRealTimers();
});

it("clears the check indicator when a native focus probe ends without publishing", async () => {
  const transport = busyStartup();
  await mount(<App transport={transport} />);
  await advance(5_000);
  expect(rosterText()).toContain("Checking agents");

  transport.tasksList = async () => [
    { ...nativeFocus, state: "cancelled" },
    { id: "refresh-1", kind: "refresh", state: "running", progress: null },
  ];
  await advance(5_000);
  expect(rosterText()).toContain("No agent check has run yet.");
});

it("keeps checking after intermittent read errors and recovers the cache without a notification", async () => {
  const transport = busyStartup();
  await mount(<App transport={transport} />);
  const listActive = transport.tasksList;
  const rejectRead = async () => {
    throw unavailable;
  };
  transport.tasksList = rejectRead;
  await advance(2_000);
  transport.tasksList = listActive;
  await advance(1_000);
  transport.tasksList = rejectRead;
  await advance(2_000);
  expect(rosterText()).toContain("Checking agents");
  expect(toastText()).toBe("");

  transport.agents = [sampleAgents.codexSignedOut];
  transport.tasksList = async () => [];
  await advance(1_000);
  expect(rosterText()).toContain("Codex");
  expect(rosterText()).not.toMatch(/checking/i);
});

it("ends checking when task status stays unavailable and still applies a later notification", async () => {
  const transport = busyStartup();
  await mount(<App transport={transport} />);
  await click(button(/^Agents/));
  expect(button(/Rescan agents/).disabled).toBe(false);
  transport.tasksList = vi.fn(async () => {
    throw unavailable;
  });
  await advance(5_000);
  expect(rosterText()).toContain("No agent check has run yet.");
  expect(button(/Rescan agents/).disabled).toBe(false);
  expect(transport.tasksList).toHaveBeenCalledTimes(3);
  expect(toastText()).toContain(unavailable.message);

  transport.agents = [sampleAgents.codexSignedOut];
  await emit(() => transport.emitAgentsChanged(sampleAgents.changed));
  expect(rosterText()).toContain("Codex");
});

it.each(["native notification", "manual rescan"] as const)(
  "drops an in-flight task snapshot when a %s publishes newer statuses",
  async (publisher) => {
    const transport = busyStartup();
    await mount(<App transport={transport} />);
    const stale = deferred<TaskSnapshotDto[]>();
    const listTasks = vi.fn(() => stale.promise);
    transport.tasksList = listTasks;
    await advance(1_000);
    expect(listTasks).toHaveBeenCalledTimes(1);
    if (publisher === "manual rescan") {
      await click(button(/^Agents/));
      await click(button(/Rescan agents/));
    }

    transport.agents = [sampleAgents.codexSignedOut];
    await emit(() => {
      if (publisher === "native notification") transport.emitAgentsChanged(sampleAgents.changed);
      else transport.emitProbe({ event: "done", data: transport.agents });
      stale.resolve([]);
    });
    await advance(5_000);
    expect(rosterText()).toContain("Codex");
    expect(rosterText()).not.toMatch(/checking/i);
    expect(transport.calls.filter((call) => call === "agents_list")).toHaveLength(2);
    expect(listTasks).toHaveBeenCalledTimes(1);
  },
);

it.each([0, 1_000])("stops polling and cache reads after unmount at %i ms", async (elapsed) => {
  const transport = busyStartup();
  await mount(<App transport={transport} />);
  const pending = deferred<TaskSnapshotDto[]>();
  const listTasks = vi.fn(() => pending.promise);
  transport.tasksList = listTasks;
  await advance(elapsed);

  await unmount();
  await emit(() => pending.resolve([]));
  await advance(5_000);
  expect(listTasks).toHaveBeenCalledTimes(elapsed === 0 ? 0 : 1);
  expect(transport.calls.filter((call) => call === "agents_list")).toHaveLength(1);
});
