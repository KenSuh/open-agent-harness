// 한국어: 팀 작업 흐름 화면 테스트 — core 공유 샘플로 카드·배너·업그레이드 확인·동기 실패·Sync now·Cursor 제외를 본다 (독립 테스트 전송 계층).
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { AgentId, StagesSetDto, WorkflowDto } from "../src/contract";
import { button, click, flush, mount, unmount } from "./support/dom";
import { createFakeTransport, sampleProject, sampleWorkflow } from "./support/fakeTransport";

afterEach(async () => {
  await unmount();
});

async function openPage(t = createFakeTransport({ project: sampleProject })) {
  await mount(<App transport={t} />);
  await click(button("Team Workflow"));
  return t;
}

function card(section: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`[data-section="${section}"]`);
  if (!found) throw new Error(`no card for ${section}`);
  return found;
}

async function choose(section: string, agent: AgentId) {
  const select = card(section).querySelector("select")!;
  await act(async () => {
    select.value = agent;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await flush();
}

const upgradeRequired = { code: "OAH_CONFIG_UPGRADE_REQUIRED", message: "Saving converts the file.", hint: null };

describe("Team Workflow page (standalone test transport)", () => {
  it("shows an empty page without a project and never calls sections_show", async () => {
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    await click(button("Team Workflow"));
    expect(document.querySelector(".empty")?.textContent).toMatch(/Open a project/);
    expect(t.calls).not.toContain("sections_show");
  });

  it("renders one card per config section, the one banner, and no Cursor option", async () => {
    await openPage();
    const cards = Array.from(document.querySelectorAll("[data-section]")).map((n) => n.getAttribute("data-section"));
    expect(cards).toEqual(sampleWorkflow.legacy.sections.map((s) => s.id));
    const banners = document.querySelectorAll('[aria-label="Workflow status"]');
    expect(banners).toHaveLength(1);
    expect(banners[0]?.getAttribute("data-kind")).toBe("not-signed-in");
    expect(banners[0]?.textContent).toMatch(/codex login/);
    const options = Array.from(card("review").querySelectorAll("option")).map((o) => o.value);
    expect(options).not.toContain("cursor");
    const codex = card("review").querySelector<HTMLOptionElement>('option[value="codex"]')!;
    expect(codex.disabled).toBe(false);
    expect(codex.textContent).toBe("Codex (signed out)");
    expect(card("review").querySelector<HTMLOptionElement>('option[value="gemini"]')!.disabled).toBe(true);
    expect(document.querySelector('[aria-label="Rules"]')?.textContent).toMatch(/Rules: not synced/);
    const research = button(/Research and build/);
    expect(research.disabled).toBe(true);
    expect(research.textContent).toMatch(/Gemini CLI is not installed/);
  });

  it("a legacy save shows the upgrade warning verbatim and retries once with the approval", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.stagesSet = async (req) => {
      if (!req.approve_upgrade) throw upgradeRequired;
      return sampleWorkflow.upgraded;
    };
    await openPage(t);
    await choose("review", "claude");
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain(sampleWorkflow.legacy.upgrade_warning);
    expect(t.workflowRequests).toEqual([{ stages: [{ section: "review", agent: "claude" }] }]);
    await click(button("Upgrade and save"));
    expect(t.workflowRequests[1]).toEqual({
      stages: [{ section: "review", agent: "claude" }],
      approve_upgrade: sampleWorkflow.legacy.config_sha256,
    });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.body.textContent).toMatch(/Upgraded the settings and saved\./);
    expect(document.querySelector('[aria-label="Rules"]')?.textContent).toMatch(/Rules: in sync/);
  });

  it("cancelling the upgrade sends nothing more", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.stagesSet = async () => {
      throw upgradeRequired;
    };
    await openPage(t);
    await choose("fix", "claude");
    await click(button("Cancel"));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(t.workflowRequests).toHaveLength(1);
  });

  it("a rules refusal after a saved config keeps the assignment, shows the reason and offers Sync now", async () => {
    const t = createFakeTransport({ project: sampleProject });
    const refused: WorkflowDto = {
      ...sampleWorkflow.upgraded.workflow,
      rules: {
        synced: false,
        drift: [],
        writes: [],
        error: { code: "OAH_RULES_BUDGET", message: "AGENTS.md would exceed 32 KiB.", hint: "Shorten AGENTS.md." },
      },
    };
    const saved: StagesSetDto = {
      ...sampleWorkflow.upgraded,
      upgraded: false,
      synced: false,
      sync_error: refused.rules.error,
      actions: ["rules_sync"],
      workflow: refused,
    };
    t.override.stagesSet = async () => saved;
    await openPage(t);
    await choose("fix", "claude");
    expect(document.body.textContent).toMatch(/Saved, but AGENTS\.md is not synced: OAH_RULES_BUDGET/);
    expect(document.querySelector('[aria-label="Rules"]')?.textContent).toMatch(/OAH_RULES_BUDGET/);
    expect((card("review").querySelector("select") as HTMLSelectElement).value).toBe("claude");
    t.override.sectionsShow = async () => sampleWorkflow.upgraded.workflow;
    await click(button("Sync now"));
    expect(t.calls).toContain("rules_sync");
    expect(document.body.textContent).toMatch(/Rules synced\./);
    expect(document.querySelector('[aria-label="Rules"]')?.textContent).toMatch(/Rules: in sync/);
  });

  it("the independence switch sends the edited list and Advanced shows the real tables and block", async () => {
    const t = createFakeTransport({ project: sampleProject });
    await openPage(t);
    const box = Array.from(card("review").querySelectorAll<HTMLLabelElement>("fieldset label")).find(
      (label) => label.textContent === "Write Code",
    )!;
    const input = box.querySelector("input")!;
    expect(input.checked).toBe(true);
    await click(input);
    expect(t.workflowRequests.at(-1)).toEqual({ independent_of: [{ section: "review", of: [] }] });
    const advanced = document.querySelector<HTMLPreElement>('[aria-label="Config tables"]');
    expect(advanced?.textContent).toBe(sampleWorkflow.upgraded.workflow.advanced.config_tables);
  });

  it("a section added in config needs no UI change", async () => {
    const t = createFakeTransport({ project: sampleProject });
    const legacy = sampleWorkflow.legacy;
    t.override.sectionsShow = async () => ({
      ...legacy,
      sections: [
        ...legacy.sections,
        { id: "docs", label: "Write Docs", agent: "claude", source: "file", independent_of: [], skills: [] },
      ],
    });
    await openPage(t);
    expect(card("docs").querySelector("h2")?.textContent).toBe("Write Docs");
    await choose("docs", "codex");
    expect(t.workflowRequests.at(-1)).toEqual({ stages: [{ section: "docs", agent: "codex" }] });
  });
});
