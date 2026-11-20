// 한국어: 에이전트 목록·읽기 전용 화면·다시 확인·포커스 겹침·구독 수명 테스트 (공유 DTO 표본과 가짜 전송 계층 사용 — 네이티브 통합 아님).
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { AgentStatusDto } from "../src/contract";
import { button, byText, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleAgents, type FakeTransport } from "./support/fakeTransport";

afterEach(async () => {
  await unmount();
});

const { claudeError, codexSignedOut, copilotDocs, cursorMissing } = sampleAgents;
const SAMPLES: AgentStatusDto[] = [claudeError, codexSignedOut, copilotDocs, cursorMissing];

const roster = () => document.querySelector("section.roster");
const rosterText = () => roster()?.textContent ?? "";
const toastText = () => document.querySelector(".toast-stack")?.textContent ?? "";
const rosterButton = (name: string) => {
  const found = Array.from(document.querySelectorAll<HTMLButtonElement>("section.roster button")).find((node) =>
    node.textContent?.startsWith(name),
  );
  if (!found) throw new Error(`roster button not found: ${name}`);
  return found;
};
const reads = (t: FakeTransport) => t.calls.filter((call) => call === "agents_list").length;

/** Mounts the app and lets its first probe finish with `list` in the cache. */
async function loaded(list: AgentStatusDto[] = SAMPLES) {
  const t = createFakeTransport();
  await mount(<App transport={t} />);
  t.agents = list;
  await emit(() => t.emitProbe({ event: "done", data: list }));
  return t;
}

describe("initial load", () => {
  it("reads the cache, starts one probe with the window-activation request and shows the result", async () => {
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    expect(t.calls).toEqual(expect.arrayContaining(["agents_list", "start_agents_probe"]));
    expect(t.probeRequests).toEqual([{ sign_in: true, focus: true }]);
    expect(rosterText()).toMatch(/Checking agents…/);

    t.agents = SAMPLES;
    await emit(() => t.emitProbe({ event: "progress", data: { phase: "probe-agents", done: 2, total: 7 } }));
    await emit(() => t.emitProbe({ event: "done", data: SAMPLES }));
    const installed = Array.from(document.querySelectorAll("section.roster .roster-list li")).map((li) => li.textContent);
    expect(installed).toEqual(["Claude Code!", "Codex✕", "Copilot CLI?"]);
    expect(document.querySelector('[role="img"][aria-label="Not signed in"]')).not.toBeNull();
    expect(document.querySelector('[role="img"][aria-label="Check failed"]')).not.toBeNull();
    expect(document.querySelector('[role="img"][aria-label="Sign-in unknown"]')).not.toBeNull();
    // The initial check reports through the roster, not a toast.
    expect(toastText()).toBe("");
  });

  it("puts agents that are not installed in a collapsed group with their install page", async () => {
    await loaded();
    const group = document.querySelector<HTMLDetailsElement>("details.not-installed");
    expect(group?.open).toBe(false);
    expect(group?.querySelector("summary")?.textContent).toBe("Not installed (1)");
    expect(group?.textContent).toMatch(/Cursor/);
    expect(group?.querySelector(".install-url")?.textContent).toBe("https://cursor.com/download");
    // No anchor: the window opens no external page and never navigates away.
    expect(group?.querySelector("a")).toBeNull();
    expect(rosterText()).not.toMatch(/Cursor.*[✓✕]/);
  });

  it("waits quietly for agents-changed when the same request is already running (native focus probe)", async () => {
    const t = createFakeTransport();
    t.override.startAgentsProbe = async () => {
      throw { code: "OAH_TASK_BUSY", message: "An agents probe is already running.", hint: null };
    };
    await mount(<App transport={t} />);
    expect(toastText()).toBe("");
    expect(rosterText()).toMatch(/Checking agents…/);
    const before = reads(t);
    t.agents = [codexSignedOut, claudeError];
    await emit(() => t.emitAgentsChanged(sampleAgents.changed));
    expect(reads(t)).toBe(before + 1);
    expect(rosterText()).not.toMatch(/Checking/);
    expect(rosterText()).toMatch(/Codex/);
  });

  it("says no check has run when the first probe was cancelled before publishing", async () => {
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    await emit(() => t.emitProbe({ event: "cancelled" }));
    expect(rosterText()).toMatch(/No agent check has run yet\./);
  });

  it("shows an agents_list error with a working reload", async () => {
    const t = createFakeTransport();
    t.override.agentsList = async () => {
      throw { code: "OAH_FILE_IO", message: "Could not read.", hint: null };
    };
    await mount(<App transport={t} />);
    expect(rosterText()).toMatch(/OAH_FILE_IO: Could not read\./);
    t.override.agentsList = undefined;
    t.agents = SAMPLES;
    await click(button("Reload agents"));
    expect(rosterText()).toMatch(/Codex/);
  });
});

describe("read-only Agents view and navigation", () => {
  it("opens one agent's detail from its rail row, marks it current, and goes back to all agents", async () => {
    await loaded();
    await click(rosterButton("Codex"));
    expect(document.querySelector("h1")?.textContent).toBe("Agents");
    expect(rosterButton("Codex").getAttribute("aria-current")).toBe("page");
    const cards = Array.from(document.querySelectorAll("article.agent-card"));
    expect(cards).toHaveLength(1);
    const card = cards[0]!;
    expect(card.querySelector("h2")?.textContent).toBe("Codex");
    expect(card.querySelector(".agent-status")?.textContent).toBe("Not signed in");
    expect(card.textContent).toMatch(/Signed out/);
    expect(card.querySelector("code")?.textContent).toBe("codex login");
    expect(card.textContent).toMatch(/codex-cli 0\.163\.0/);
    expect(card.textContent).toMatch(/Skill links\s*Not checked/);
    expect(card.textContent).toMatch(/Detect install\s*Verified/);
    // Read-only: no control changes an agent.
    expect(card.querySelector("button, input, select")).toBeNull();

    await click(button("← All agents"));
    expect(document.querySelectorAll("article.agent-card")).toHaveLength(4);
    expect(rosterButton("Agents").getAttribute("aria-current")).toBe("page");
    expect(rosterButton("Codex").getAttribute("aria-current")).toBeNull();
    await click(button("Explore"));
    expect(document.querySelector("h1")?.textContent).toBe("Explore");
    expect(document.querySelector("nav.rail .active")?.textContent).toBe("Explore");
    expect(roster()?.querySelector('[aria-current="page"]')).toBeNull();
  });

  it("shows a probe problem as the agent's status, not as an app error", async () => {
    await loaded();
    await click(rosterButton("Claude Code"));
    const card = document.querySelector("article.agent-card")!;
    expect(card.querySelector(".agent-status")?.textContent).toBe("Check failed");
    expect(card.querySelector(".agent-problem")?.textContent).toMatch(/OAH_AGENT_PROBE_FAILED/);
    expect(card.textContent).toMatch(/timed out after 10s/);
    expect(card.textContent).toMatch(/Run the agent's command in a terminal/);
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });

  it("shows the neutral note for sign-in-unknown and plain text for a non-command hint", async () => {
    await loaded();
    await click(rosterButton("Copilot CLI"));
    expect(document.querySelector("article.agent-card")?.textContent).toMatch(/OAH cannot check sign-in for Copilot CLI\./);
    await click(button("← All agents"));
    const cursor = Array.from(document.querySelectorAll("article.agent-card")).find((card) =>
      /Cursor/.test(card.querySelector("h2")?.textContent ?? ""),
    )!;
    expect(cursor.textContent).toMatch(/Cursor Settings → Sign In/);
    expect(cursor.querySelector("code")).toBeNull();
    expect(cursor.textContent).toMatch(/Not checked: the agent is not installed\./);
    expect(cursor.querySelector(".install-url")?.textContent).toBe("https://cursor.com/download");
    expect(cursor.textContent).toMatch(/No \(skills and rules only\)/);
  });
});

describe("manual rescan", () => {
  async function onAgentsPage() {
    const t = await loaded();
    await click(rosterButton("Agents"));
    return t;
  }

  it("rescans every agent with sign-in, shows progress, blocks a second start and re-reads the cache on done", async () => {
    const t = await onAgentsPage();
    await click(button("↻ Rescan agents"));
    expect(t.probeRequests.at(-1)).toEqual({ sign_in: true, focus: false });
    expect(button("↻ Rescan agents").disabled).toBe(true);
    await click(button("↻ Rescan agents"));
    expect(t.probeRequests).toHaveLength(2);
    await emit(() => t.emitProbe({ event: "progress", data: { phase: "probe-agents", done: 3, total: 7 } }));
    expect(document.querySelector('[aria-label="Agent check"]')?.textContent).toMatch(/probe-agents\) 3 \/ 7/);
    const before = reads(t);
    t.agents = [{ ...codexSignedOut, status: "ok", sign_in: "signed-in" }];
    await emit(() => t.emitProbe({ event: "done", data: t.agents }));
    expect(reads(t)).toBe(before + 1);
    expect(document.querySelector('[aria-label="Agent check"]')).toBeNull();
    expect(button("↻ Rescan agents").disabled).toBe(false);
    expect(toastText()).toMatch(/Agents checked: 1 agent\(s\)\./);
    expect(document.querySelector('[role="img"][aria-label="Signed in"]')).not.toBeNull();
  });

  it("cancels its own check by task id and keeps the cached list", async () => {
    const t = await onAgentsPage();
    await click(button("↻ Rescan agents"));
    await click(button("Cancel check"));
    expect(t.calls).toContain("task_cancel:probe-2");
    await emit(() => t.emitProbe({ event: "cancelled" }));
    expect(toastText()).toMatch(/Agent check cancelled\. Nothing changed\./);
    expect(document.querySelectorAll("article.agent-card")).toHaveLength(4);
    expect(button("↻ Rescan agents").disabled).toBe(false);
  });

  it("shows a failed check and a refused start as messages", async () => {
    const t = await onAgentsPage();
    await click(button("↻ Rescan agents"));
    await emit(() => t.emitProbe({ event: "failed", data: { code: "OAH_FILE_IO", message: "Read failed.", hint: null } }));
    expect(toastText()).toMatch(/OAH_FILE_IO: Read failed\./);
    t.override.startAgentsProbe = async () => {
      throw { code: "OAH_TASK_BUSY", message: "busy", hint: null };
    };
    await click(button("↻ Rescan agents"));
    expect(toastText()).toMatch(/already running; the list updates when it ends/);
    expect(button("↻ Rescan agents").disabled).toBe(false);
  });
});

describe("focus probe and rescan overlap", () => {
  it("follows agents-changed from the focus probe during a rescan, and an older read never replaces a newer one", async () => {
    const t = await loaded();
    await click(rosterButton("Agents"));
    await click(button("↻ Rescan agents"));

    // The native focus probe ends first: agents-changed invalidates, and the UI reads the cache.
    const focusRead = deferred<AgentStatusDto[]>();
    const rescanRead = deferred<AgentStatusDto[]>();
    const pending = [focusRead, rescanRead];
    t.override.agentsList = () => pending.shift()!.promise;
    await emit(() => t.emitAgentsChanged(sampleAgents.changed));
    expect(document.querySelector('[aria-label="Agent check"]')).not.toBeNull(); // the rescan still runs

    // The rescan ends next and its read answers first; the focus read answers last with older data.
    const newer: AgentStatusDto[] = [{ ...codexSignedOut, status: "ok", sign_in: "signed-in" }];
    await emit(() => t.emitProbe({ event: "done", data: newer }));
    await emit(() => rescanRead.resolve(newer));
    await emit(() => focusRead.resolve(SAMPLES));
    const marks = Array.from(document.querySelectorAll("section.roster .sign-in-mark")).map((m) => m.getAttribute("aria-label"));
    expect(marks).toEqual(["Signed in"]);
    expect(document.querySelectorAll("article.agent-card")).toHaveLength(1);
    expect(document.querySelector('[aria-label="Agent check"]')).toBeNull();
  });

  it("re-reads the cache for every agents-changed, also after a project refresh", async () => {
    const t = await loaded();
    const before = reads(t);
    await emit(() => t.emitAgentsChanged({ agents: ["claude"] }));
    await emit(() => t.emitAgentsChanged({ agents: ["codex"] }));
    expect(reads(t)).toBe(before + 2);
  });
});

describe("agents-changed subscription lifetime", () => {
  it("unsubscribes on unmount, so a later event reads nothing", async () => {
    const t = await loaded();
    expect(t.agentsSubscribed()).toBe(true);
    await unmount();
    expect(t.calls).toContain("unlisten:agents-changed");
    expect(t.agentsSubscribed()).toBe(false);
    const before = reads(t);
    t.emitAgentsChanged(sampleAgents.changed);
    expect(reads(t)).toBe(before);
  });

  it("stops a subscription that answers only after the view is gone", async () => {
    const t = createFakeTransport();
    const subscribed = deferred<() => void>();
    let stopped = 0;
    t.override.onAgentsChanged = () => subscribed.promise;
    await mount(<App transport={t} />);
    await unmount();
    subscribed.resolve(() => {
      stopped += 1;
    });
    await subscribed.promise;
    await Promise.resolve();
    expect(stopped).toBe(1);
  });
});

describe("existing pages keep working beside the roster", () => {
  it("keeps the three section buttons and their pages", async () => {
    await loaded();
    const rail = Array.from(document.querySelectorAll("nav.rail button")).map((n) => n.textContent);
    expect(rail).toEqual(["Explore", "Installed", "Team Workflow"]);
    await click(button("Installed"));
    expect(document.querySelector("h1")?.textContent).toBe("Installed");
    expect(byText("p", /No skills are managed yet/)).not.toBeNull();
  });
});
