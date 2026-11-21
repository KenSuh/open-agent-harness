// 한국어: 팀 작업 흐름 화면 테스트 — core 공유 샘플로 카드·배너·업그레이드 확인·동기 실패·Sync now·Cursor 제외를 본다 (독립 테스트 전송 계층).
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { AgentId, ProjectDto, StagesSetDto, WorkflowDto } from "../src/contract";
import { button, click, emit, flush, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleProject, sampleWorkflow } from "./support/fakeTransport";

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
const secondProject = { ...sampleProject, id: "second-project", root: "/example/second" };
const withDocs = (view: WorkflowDto, label: string): WorkflowDto => ({
  ...view,
  sections: [
    ...view.sections,
    { id: "docs", label, agent: "claude", source: "file", independent_of: [], skills: [] },
  ],
});

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
    t.override.stagesSet = async ({ input }) => {
      if (!input.approve_upgrade) throw upgradeRequired;
      return sampleWorkflow.upgraded;
    };
    await openPage(t);
    await choose("review", "claude");
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain(sampleWorkflow.legacy.upgrade_warning);
    expect(t.workflowRequests).toEqual([{
      project_id: sampleProject.id,
      input: { stages: [{ section: "review", agent: "claude" }] },
    }]);
    await click(button("Upgrade and save"));
    expect(t.workflowRequests[1]?.input).toEqual({
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
    expect(t.workflowRequests.at(-1)?.input).toEqual({ independent_of: [{ section: "review", of: [] }] });
    const advanced = document.querySelector<HTMLPreElement>('[aria-label="Config tables"]');
    expect(advanced?.textContent).toBe(sampleWorkflow.upgraded.workflow.advanced.config_tables);
  });

  it("a section added in config needs no UI change", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.sectionsShow = async () => withDocs(sampleWorkflow.legacy, "Write Docs");
    await openPage(t);
    expect(card("docs").querySelector("h2")?.textContent).toBe("Write Docs");
    await choose("docs", "codex");
    expect(t.workflowRequests.at(-1)?.input).toEqual({ stages: [{ section: "docs", agent: "codex" }] });
  });

  it("a project switch discards a pending upgrade and binds the selected project's actions", async () => {
    const t = createFakeTransport({ project: sampleProject });
    const oldProjectRead = deferred<ProjectDto>();
    const newWorkflow = deferred<WorkflowDto>();
    t.override.projectOpenDialog = async () => secondProject;
    t.override.sectionsShow = ({ project_id }) =>
      project_id === secondProject.id ? newWorkflow.promise : Promise.resolve(sampleWorkflow.legacy);
    t.override.stagesSet = async ({ project_id }) => {
      if (project_id === sampleProject.id) throw upgradeRequired;
      return sampleWorkflow.upgraded;
    };
    await openPage(t);
    await choose("review", "claude");
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();

    t.override.projectCurrent = () => oldProjectRead.promise;
    await emit(() => t.emitScan({ scope: "all", keys: [] }));
    await click(button("+ Open…"));
    expect(document.querySelector(".project-name")?.textContent).toBe("second");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.querySelector("[data-section]")).toBeNull();
    expect(document.querySelector('[aria-label="Rules"]')).toBeNull();
    expect(document.querySelector('[aria-label="Presets"]')).toBeNull();
    await emit(() => oldProjectRead.resolve(sampleProject));
    expect(document.querySelector(".project-name")?.textContent).toBe("second");
    const page = withDocs(sampleWorkflow.legacy, "Second project docs");
    await emit(() => newWorkflow.resolve(page));
    expect(card("docs").textContent).toContain("Second project docs");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(t.workflowRequests).toHaveLength(1);

    await click(button("Sync now"));
    await click(button("Balanced"));
    await choose("review", "codex");
    expect(t.workflowRequests.slice(1)).toEqual([
      { project_id: secondProject.id, input: {} },
      { project_id: secondProject.id, input: { preset: "balanced" } },
      { project_id: secondProject.id, input: { stages: [{ section: "review", agent: "codex" }] } },
    ]);
  });

  it("a project switch ignores the previous workflow read when it resolves last", async () => {
    const t = createFakeTransport({ project: sampleProject });
    const oldWorkflow = deferred<WorkflowDto>();
    t.override.projectOpenDialog = async () => secondProject;
    t.override.sectionsShow = async ({ project_id }) =>
      project_id === sampleProject.id ? oldWorkflow.promise : withDocs(sampleWorkflow.legacy, "Second project docs");
    await openPage(t);
    expect(document.querySelector("[data-section]")).toBeNull();
    await click(button("+ Open…"));
    expect(card("docs").textContent).toContain("Second project docs");
    await emit(() => oldWorkflow.resolve(sampleWorkflow.legacy));
    expect(card("docs").textContent).toContain("Second project docs");
  });

  it("refresh or reopen shows config edits with an unchanged project identity and agent roster", async () => {
    const t = createFakeTransport({ project: sampleProject });
    let page = sampleWorkflow.legacy;
    t.override.sectionsShow = async () => page;
    await openPage(t);
    expect(document.querySelector('[data-section="docs"]')).toBeNull();
    page = withDocs(sampleWorkflow.legacy, "Write Docs");
    await click(button(/Refresh/));
    await emit(() => {
      t.emitRefresh({ event: "done", data: { projects: 1, notices: 0 } });
      t.emitScan({ scope: "all", keys: [] });
    });
    expect(card("docs").querySelector("h2")?.textContent).toBe("Write Docs");
    page = withDocs(sampleWorkflow.legacy, "Publish Docs");
    await click(button("+ Open…"));
    expect(card("docs").querySelector("h2")?.textContent).toBe("Publish Docs");
  });

  it("a stale upgrade reloads the settings and waits for a fresh explicit approval", async () => {
    const t = createFakeTransport({ project: sampleProject });
    const newWorkflow = deferred<WorkflowDto>();
    const changed = { ...sampleWorkflow.legacy, config_sha256: "a".repeat(64) };
    let fingerprint = sampleWorkflow.legacy.config_sha256;
    t.override.stagesSet = async ({ input }) => {
      if (input.approve_upgrade !== fingerprint) throw upgradeRequired;
      return sampleWorkflow.upgraded;
    };
    await openPage(t);
    await choose("review", "claude");
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    fingerprint = changed.config_sha256;
    t.override.sectionsShow = () => newWorkflow.promise;
    await click(button("Upgrade and save"));
    expect(t.workflowRequests[1]?.input.approve_upgrade).toBe(sampleWorkflow.legacy.config_sha256);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.querySelector("[data-section]")).toBeNull();
    await emit(() => newWorkflow.resolve(changed));
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(t.workflowRequests).toHaveLength(2);
    await click(button("Upgrade and save"));
    expect(t.workflowRequests[2]?.input.approve_upgrade).toBe(changed.config_sha256);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.querySelector('[aria-label="Rules"]')?.textContent).toMatch(/Rules: in sync/);
  });
});
