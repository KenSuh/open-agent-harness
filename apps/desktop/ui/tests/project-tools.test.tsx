// 한국어: 프로젝트 도구 카드 테스트 — core 공유 샘플로 카드 순서·글 상태·다음 단계를 보고, 프로젝트가 바뀐 뒤 늦게 온 응답을 버리며, 다시 확인이 한 번만 알림을 낸다 (독립 테스트 전송 계층).
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { ProjectToolsDto } from "../src/contract";
import { button, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleProject, sampleProjectTools } from "./support/fakeTransport";

afterEach(async () => {
  await unmount();
});

async function openPage(t = createFakeTransport({ project: sampleProject })) {
  await mount(<App transport={t} />);
  await click(button("Team Workflow"));
  return t;
}

function tool(id: string): HTMLElement {
  const found = document.querySelector<HTMLElement>(`[data-tool="${id}"]`);
  if (!found) throw new Error(`no card for ${id}`);
  return found;
}

const toolIds = () => Array.from(document.querySelectorAll("[data-tool]")).map((node) => node.getAttribute("data-tool"));
const announced = () => document.querySelector('.project-tools [role="status"]')?.textContent ?? "";
const secondProject = { ...sampleProject, id: "second-project", root: "/example/second" };
const forProject = (id: string, status: string): ProjectToolsDto => ({
  project_id: id,
  tools: sampleProjectTools.tools.map((card) => (card.id === "wiki" ? { ...card, status } : card)),
});

describe("Project tool cards (standalone test transport)", () => {
  it("is not read without a project", async () => {
    const t = createFakeTransport();
    await mount(<App transport={t} />);
    await click(button("Team Workflow"));
    expect(t.calls).not.toContain("project_tools_status");
    expect(document.querySelector(".project-tools")).toBeNull();
  });

  it("shows the three cards as name, text status, then next step, beside the unchanged workflow controls", async () => {
    const t = await openPage();
    expect(toolIds()).toEqual(["wiki", "impact-map", "jev"]);
    for (const card of sampleProjectTools.tools) {
      const node = tool(card.id);
      const parts = Array.from(node.children).map((child) => child.className || child.tagName);
      expect(parts.slice(0, 3)).toEqual(["H3", "tool-status", "tool-next"]);
      expect(node.querySelector("h3")?.textContent).toBe(card.name);
      expect(node.querySelector(".tool-status")?.textContent).toBe(card.status);
      expect(node.querySelector(".tool-next")?.textContent).toBe(`Next step: ${card.next_step}`);
      expect(node.getAttribute("data-state")).toBe(card.state);
      expect(node.getAttribute("aria-labelledby")).toBe(node.querySelector("h3")?.id);
      // A step the app cannot do yet is text, never a button; the one control opens this card's settings.
      const controls = Array.from(node.querySelectorAll("button, a, input, select, textarea"));
      expect(controls.map((control) => control.textContent)).toEqual([`Edit ${card.name} Settings`]);
    }
    expect(tool("wiki").querySelector("code")?.textContent).toBe("docs/wiki");
    expect(tool("wiki").querySelector(".tool-paths")?.textContent).toContain("(in the project)");
    expect(tool("jev").querySelector(".tool-notes")?.textContent).toContain("did not look for a key");
    expect(t.calls.filter((call) => call === "project_tools_status")).toHaveLength(1);
    expect(announced()).toBe("");
    // The existing page is all still there.
    expect(document.querySelectorAll("[data-section]").length).toBeGreaterThan(0);
    expect(document.querySelector('[aria-label="Workflow status"]')).not.toBeNull();
    expect(document.querySelector('[aria-label="Rules"]')).not.toBeNull();
    expect(document.querySelector('[aria-label="Presets"]')).not.toBeNull();
  });

  it("shows the reason between the status and the next step", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectToolsStatus = async ({ project_id }) => ({
      project_id,
      tools: sampleProjectTools.tools.map((card) =>
        card.id === "jev" ? { ...card, state: "invalid", reason: "enabled under [rerank] is not true or false." } : card,
      ),
    });
    await openPage(t);
    const parts = Array.from(tool("jev").children).map((child) => child.className || child.tagName);
    expect(parts.slice(0, 4)).toEqual(["H3", "tool-status", "tool-reason", "tool-next"]);
    expect(tool("jev").querySelector(".tool-reason")?.textContent).toMatch(/not true or false/);
  });

  it("drops the previous project's answer when it arrives after the swap", async () => {
    const t = createFakeTransport({ project: sampleProject });
    const late = deferred<ProjectToolsDto>();
    t.override.projectOpenDialog = async () => secondProject;
    t.override.projectToolsStatus = async ({ project_id }) =>
      project_id === sampleProject.id ? late.promise : forProject(project_id, "Second project wiki.");
    await openPage(t);
    expect(toolIds()).toEqual([]);
    await click(button("+ Open…"));
    expect(tool("wiki").querySelector(".tool-status")?.textContent).toBe("Second project wiki.");
    await emit(() => late.resolve(forProject(sampleProject.id, "First project wiki.")));
    expect(tool("wiki").querySelector(".tool-status")?.textContent).toBe("Second project wiki.");
    expect(document.querySelector(".project-tools [role='alert']")).toBeNull();
  });

  it("never shows an answer that names another project", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectToolsStatus = async () => forProject("some-other-project", "Another project's wiki.");
    await openPage(t);
    expect(toolIds()).toEqual([]);
    expect(document.body.textContent).not.toContain("Another project's wiki.");
    expect(document.querySelector(".project-tools [role='alert']")?.textContent).toMatch(/another project/);
  });

  it("Check Again reads once more, keeps focus, ignores a second press while busy and announces once", async () => {
    const t = createFakeTransport({ project: sampleProject });
    await openPage(t);
    const again = deferred<ProjectToolsDto>();
    t.override.projectToolsStatus = () => again.promise;
    const check = button("Check Again");
    expect(check.getAttribute("aria-disabled")).toBe("false");
    check.focus();
    await click(check);
    expect(check.getAttribute("aria-disabled")).toBe("true");
    expect(check.disabled).toBe(false);
    expect(document.activeElement).toBe(check);
    expect(document.querySelector(".tool-cards")?.getAttribute("aria-busy")).toBe("true");
    await click(check);
    expect(t.calls.filter((call) => call === "project_tools_status")).toHaveLength(2);
    expect(announced()).toBe("");

    await emit(() => again.resolve(forProject(sampleProject.id, "No wiki folder found in this project.")));
    expect(tool("wiki").querySelector(".tool-status")?.textContent).toBe("No wiki folder found in this project.");
    expect(document.querySelectorAll('.project-tools [role="status"]')).toHaveLength(1);
    expect(announced()).toMatch(/^Checked again\. Project Wiki: No wiki folder found in this project\. Impact Map: /);
    expect(document.activeElement).toBe(check);
    expect(check.getAttribute("aria-disabled")).toBe("false");
  });

  it("a failed check says why beside Check Again, leaves the workflow usable and recovers", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectToolsStatus = async () => {
      throw { code: "OAH_PROJECT_CHANGED", message: "the open project changed", hint: "Open the project again." };
    };
    await openPage(t);
    const alert = document.querySelector(".project-tools [role='alert']");
    expect(alert?.textContent).toContain("OAH_PROJECT_CHANGED: the open project changed");
    expect(alert?.textContent).toContain("Open the project again.");
    expect(toolIds()).toEqual([]);
    expect(document.querySelectorAll("[data-section]").length).toBeGreaterThan(0);

    t.override.projectToolsStatus = undefined;
    await click(button("Check Again"));
    expect(document.querySelector(".project-tools [role='alert']")).toBeNull();
    expect(toolIds()).toEqual(["wiki", "impact-map", "jev"]);
  });
});
