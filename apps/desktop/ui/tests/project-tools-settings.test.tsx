// 한국어: 프로젝트 도구 설정 편집 테스트 — 열기 전에는 계획을 읽지 않고, 미리보기 계획의 변경 전후를 그대로 보이며, 취소·프로젝트 변경·미리보기 뒤 수정·낡은 계획은 저장으로 이어지지 않는다 (독립 테스트 전송 계층, 쓰기 없음).
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { ProjectToolsApplyDto, ProjectToolsPlanDto } from "../src/contract";
import { button, byText, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleProject, samplePlan } from "./support/fakeTransport";

afterEach(async () => {
  await unmount();
});

async function openPage(t = createFakeTransport({ project: sampleProject })) {
  await mount(<App transport={t} />);
  await click(button("Team Workflow"));
  return t;
}

function field<T extends HTMLElement = HTMLInputElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) throw new Error("no field " + id);
  return found as T;
}

async function type(element: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(element, value);
  await emit(() => element.dispatchEvent(new Event("input", { bubbles: true })));
}

const panel = () => document.querySelector<HTMLElement>(".tool-settings");
const feedback = () => document.querySelector(".tool-settings-feedback")?.textContent ?? "";
const count = (t: { calls: string[] }, name: string) => t.calls.filter((call) => call === name).length;
const hasButton = (text: string) => byText("button", text) !== null;
const secondProject = { ...sampleProject, id: "second-project", root: "/example/second" };
const wikiEdit = [{ kind: "wiki-dir" as const, value: "notes/wiki" }];

async function previewWikiFolder(t: Awaited<ReturnType<typeof openPage>>) {
  await click(button("Edit Project Wiki Settings"));
  await type(field("tool-setting-wiki-dir"), "notes/wiki");
  await click(button("Preview Changes"));
  return t;
}

describe("Project tool settings (standalone test transport, nothing is written)", () => {
  it("reads no plan on a cold opening and only an empty plan after the Edit button", async () => {
    const t = await openPage();
    expect(count(t, "project_tools_status")).toBe(1);
    expect(count(t, "project_tools_plan")).toBe(0);
    expect(panel()).toBeNull();

    await click(button("Edit Impact Map Settings"));
    expect(t.toolsPlanRequests).toEqual([{ project_id: sampleProject.id, input: { edits: [] } }]);
    expect(panel()?.closest("[data-tool]")?.getAttribute("data-tool")).toBe("impact-map");
    expect(document.activeElement).toBe(panel());
    expect(field("tool-setting-impact-map-graph-file").value).toBe("graphify-out/graph.json");
    // The list follows the wiki folder until the box is ticked.
    expect(field<HTMLTextAreaElement>("tool-setting-impact-map-wiki-dirs").disabled).toBe(true);
    // Optional sections core does not offer are not shown, and the other cards wait.
    expect(document.querySelector(".tool-optional")).toBeNull();
    expect(hasButton("Edit Project Wiki Settings")).toBe(false);
    expect(hasButton("Edit Jev Settings")).toBe(false);
    expect(count(t, "project_tools_apply")).toBe(0);
  });

  it("previews the exact before and after, then saves the same edits under that plan and closes", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectToolsPlan = async ({ project_id, input }) =>
      samplePlan(project_id, input.edits, {
        kept: input.edits.length ? [{ key: "impact.wiki_dirs", value: ["docs/a", "docs/b"], reason: "explicit-wiki-dirs" }] : [],
      });
    await previewWikiFolder(await openPage(t));
    expect(t.toolsPlanRequests[1]).toEqual({ project_id: sampleProject.id, input: { edits: wikiEdit } });
    expect(panel()?.getAttribute("data-phase")).toBe("review");
    expect(panel()?.querySelector(".tool-review code")?.textContent).toBe(".oah/config.toml");
    const row = panel()!.querySelector('tr[data-key="wiki.dir"]')!;
    expect(Array.from(row.querySelectorAll("td")).map((cell) => cell.textContent)).toEqual(['"docs/wiki"', '"notes/wiki"']);
    expect(panel()?.querySelector(".tool-kept")?.textContent).toContain('impact.wiki_dirs stays ["docs/a", "docs/b"].');
    expect(panel()?.querySelector(".tool-upgrade")).toBeNull();
    expect(feedback()).toBe("Nothing is saved yet.");
    expect(count(t, "project_tools_apply")).toBe(0);

    await click(button("Save Changes"));
    expect(t.toolsApplyRequests).toEqual([
      { project_id: sampleProject.id, input: { edits: wikiEdit, plan_id: samplePlan(sampleProject.id, wikiEdit).plan_id } },
    ]);
    expect(panel()).toBeNull();
    expect(document.querySelector('[data-tool="wiki"] .tool-saved')?.textContent).toBe("Saved to .oah/config.toml.");
    expect(count(t, "project_tools_status")).toBe(2);
    expect(document.activeElement).toBe(button("Edit Project Wiki Settings"));
  });

  it("turning Jev on stages the flag apart from any key or permission and sends only the flag", async () => {
    const t = await openPage();
    await click(button("Edit Jev Settings"));
    expect(panel()?.querySelector(".tool-boundary")?.textContent).toMatch(/sends nothing, looks for no key and gives no permission/);
    await click(field("tool-setting-jev-enabled"));
    await click(button("Preview Changes"));
    expect(t.toolsPlanRequests[1]?.input.edits).toEqual([{ kind: "jev-enabled", value: true }]);
    const row = panel()!.querySelector('tr[data-key="rerank.enabled"]')!;
    expect(Array.from(row.querySelectorAll("td")).map((cell) => cell.textContent)).toEqual(["not set", "true"]);
  });

  it("Cancel, an edit after the preview and a late plan never lead to a save", async () => {
    const t = createFakeTransport({ project: sampleProject });
    await previewWikiFolder(await openPage(t));
    // Editing again drops the reviewed plan: there is nothing to save until a new preview.
    await click(button("Edit Again"));
    expect(hasButton("Save Changes")).toBe(false);
    await type(field("tool-setting-wiki-dir"), "notes/other");
    const late = deferred<ProjectToolsPlanDto>();
    t.override.projectToolsPlan = () => late.promise;
    await click(button("Preview Changes"));
    expect(feedback()).toMatch(/Nothing is saved yet/);
    await click(button("Cancel"));
    expect(panel()).toBeNull();
    await emit(() => late.resolve(samplePlan(sampleProject.id, [{ kind: "wiki-dir", value: "notes/other" }])));
    expect(panel()).toBeNull();
    expect(hasButton("Save Changes")).toBe(false);
    expect(count(t, "project_tools_apply")).toBe(0);
  });

  it("a plan for the previous project is dropped with its editor when the project changes", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectOpenDialog = async () => secondProject;
    await openPage(t);
    await click(button("Edit Project Wiki Settings"));
    await type(field("tool-setting-wiki-dir"), "notes/wiki");
    const late = deferred<ProjectToolsPlanDto>();
    t.override.projectToolsPlan = () => late.promise;
    await click(button("Preview Changes"));
    await click(button("+ Open…"));
    expect(panel()).toBeNull();
    await emit(() => late.resolve(samplePlan(sampleProject.id, wikiEdit)));
    expect(panel()).toBeNull();
    expect(hasButton("Edit Project Wiki Settings")).toBe(true);
    expect(count(t, "project_tools_apply")).toBe(0);
  });

  it("never reviews a plan that names another project", async () => {
    const t = createFakeTransport({ project: sampleProject });
    await openPage(t);
    await click(button("Edit Project Wiki Settings"));
    await type(field("tool-setting-wiki-dir"), "notes/wiki");
    t.override.projectToolsPlan = async ({ input }) => samplePlan("some-other-project", input.edits);
    await click(button("Preview Changes"));
    expect(feedback()).toContain("UI_PROJECT_CHANGED");
    expect(hasButton("Save Changes")).toBe(false);
    expect(hasButton("Preview Again")).toBe(true);
  });

  it("a legacy upgrade needs its own acknowledgement and sends that plan's fingerprint only then", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectToolsPlan = async ({ project_id, input }) =>
      samplePlan(
        project_id,
        input.edits,
        input.edits.length
          ? {
              upgrade: { message: "Saving converts .oah/config.toml to the OAH 1.0 format.", fingerprint: "f00d" },
              backup: { state: "write", path: ".oah/config.toml.v0.4.bak" },
            }
          : {},
      );
    t.override.projectToolsApply = async ({ project_id, input }) => ({
      project_id,
      plan_id: input.plan_id,
      saved: true,
      upgraded: true,
      backup: ".oah/config.toml.v0.4.bak",
    });
    await previewWikiFolder(await openPage(t));
    const upgrade = panel()!.querySelector(".tool-upgrade")!;
    expect(upgrade.textContent).toContain("Saving converts .oah/config.toml to the OAH 1.0 format.");
    expect(upgrade.textContent).toContain("will be kept as .oah/config.toml.v0.4.bak");
    const save = button("Upgrade and Save");
    expect(save.getAttribute("aria-disabled")).toBe("true");
    await click(save);
    expect(count(t, "project_tools_apply")).toBe(0);
    expect(feedback()).toBe("Tick the upgrade box to save.");

    await click(field("tool-upgrade-ack"));
    expect(button("Upgrade and Save").getAttribute("aria-disabled")).toBe("false");
    await click(button("Upgrade and Save"));
    expect(t.toolsApplyRequests[0]?.input).toEqual({
      edits: wikiEdit,
      plan_id: samplePlan(sampleProject.id, wikiEdit).plan_id,
      approve_upgrade: "f00d",
    });
    expect(document.querySelector('[data-tool="wiki"] .tool-saved')?.textContent).toBe(
      "Upgraded and saved .oah/config.toml. The original is kept as .oah/config.toml.v0.4.bak.",
    );
  });

  it("a backup conflict and a plan with nothing to change offer no save", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectToolsPlan = async ({ project_id, input }) =>
      samplePlan(
        project_id,
        input.edits,
        input.edits.length
          ? { upgrade: { message: "Upgrade.", fingerprint: "f00d" }, backup: { state: "conflict", path: ".oah/config.toml.v0.4.bak" } }
          : {},
      );
    await previewWikiFolder(await openPage(t));
    expect(panel()?.querySelector(".tool-upgrade")?.textContent).toContain("Another file is already at the backup path.");
    expect(hasButton("Upgrade and Save")).toBe(false);
    expect(document.getElementById("tool-upgrade-ack")).toBeNull();

    await click(button("Edit Again"));
    t.override.projectToolsPlan = async ({ project_id, input }) => samplePlan(project_id, input.edits, { changes: [] });
    await click(button("Preview Changes"));
    expect(panel()?.textContent).toContain("Nothing to save: the file already has these settings.");
    expect(hasButton("Save Changes")).toBe(false);
    expect(count(t, "project_tools_apply")).toBe(0);
  });

  it("read and preview failures say why in the one feedback region and can be retried", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectToolsPlan = async () => {
      throw { code: "OAH_CONFIG_INVALID", message: "wiki is not a table", hint: "Fix .oah/config.toml." };
    };
    await openPage(t);
    await click(button("Edit Project Wiki Settings"));
    expect(document.querySelectorAll(".tool-settings [role='status']")).toHaveLength(1);
    expect(feedback()).toContain("The saved settings could not be read. OAH_CONFIG_INVALID: wiki is not a table Fix .oah/config.toml.");
    // Only this card says so: the page and the other cards stay as they were.
    expect(document.querySelector(".project-tools .error-box")).toBeNull();
    expect(document.querySelectorAll("[data-tool]")).toHaveLength(3);
    t.override.projectToolsPlan = undefined;
    await click(button("Read Again"));
    expect(field("tool-setting-wiki-dir").value).toBe("docs/wiki");

    await click(button("Preview Changes"));
    expect(feedback()).toBe("Nothing to preview: the form matches the saved settings.");
    expect(count(t, "project_tools_plan")).toBe(2);
    await type(field("tool-setting-wiki-dir"), "  ");
    await click(button("Preview Changes"));
    expect(field("tool-setting-wiki-dir").getAttribute("aria-invalid")).toBe("true");
    expect(count(t, "project_tools_plan")).toBe(2);

    await type(field("tool-setting-wiki-dir"), "../outside");
    t.override.projectToolsPlan = async () => {
      throw { code: "OAH_CONFIG_INVALID", message: "wiki.dir cannot be set to \"../outside\"", hint: null };
    };
    await click(button("Preview Changes"));
    expect(feedback()).toContain("Nothing was saved. OAH_CONFIG_INVALID");
    expect(field("tool-setting-wiki-dir").value).toBe("../outside");
    expect(hasButton("Preview Again")).toBe(true);
  });

  it("a plan the file moved on from is dropped; another save failure keeps the plan for one more try", async () => {
    const t = createFakeTransport({ project: sampleProject });
    const answers: Array<() => Promise<ProjectToolsApplyDto>> = [
      async () => {
        throw { code: "OAH_FILE_IO", message: "the settings file is locked", hint: "Try again." };
      },
      async () => {
        throw { code: "OAH_PLAN_STALE", message: "the approved plan is not the plan for this file now", hint: null };
      },
    ];
    t.override.projectToolsApply = () => answers.shift()!();
    await previewWikiFolder(await openPage(t));
    await click(button("Save Changes"));
    expect(feedback()).toContain("Not saved. OAH_FILE_IO: the settings file is locked Try again.");
    expect(panel()?.getAttribute("data-phase")).toBe("review");
    await click(button("Try Saving Again"));
    expect(t.toolsApplyRequests).toHaveLength(2);
    expect(t.toolsApplyRequests[1]).toEqual(t.toolsApplyRequests[0]);
    expect(feedback()).toContain("the settings file changed after the preview");
    expect(panel()?.getAttribute("data-phase")).toBe("edit");
    expect(hasButton("Save Changes")).toBe(false);
    expect(field("tool-setting-wiki-dir").value).toBe("notes/wiki");
    await click(button("Preview Again"));
    expect(panel()?.getAttribute("data-phase")).toBe("review");
    expect(t.toolsApplyRequests).toHaveLength(2);
  });

  it("shows an optional section only when core offers it, as a request and not a permission", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectToolsPlan = async ({ project_id, input }) => samplePlan(project_id, input.edits, { optional: ["wiki-index"] });
    await openPage(t);
    await click(button("Edit Project Wiki Settings"));
    expect(panel()?.querySelector(".tool-optional")?.textContent).toMatch(/Nothing runs and nothing is sent/);
    await click(field("tool-setting-wiki-index-managed"));
    await type(field<HTMLTextAreaElement>("tool-setting-wiki-index-exclude"), "drafts/**\n\n private/** ");
    await click(button("Preview Changes"));
    expect(t.toolsPlanRequests[1]?.input.edits).toEqual([
      { kind: "wiki-index-managed", value: true },
      { kind: "wiki-index-exclude", value: ["drafts/**", "private/**"] },
    ]);
  });
});
