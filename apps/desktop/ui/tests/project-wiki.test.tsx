// 한국어: 프로젝트 위키 패널 테스트 — 버튼을 누르기 전에는 위키를 읽지 않고, 미리보기 계획 하나를 그대로 적용 요청에 묶으며, 중지·낡은 계획·부분 결과를 그대로 보이고, 프로젝트가 바뀐 뒤 온 응답과 이벤트를 버린다 (독립 테스트 전송 계층, 쓰기 없음).
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { ProjectWikiDto, WikiPlanDto, WikiSearchDto } from "../src/contract";
import { showBytes } from "../src/project-wiki";
import { button, byText, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleProject, sampleWiki, type FakeTransport } from "./support/fakeTransport";
import applyReqSetup from "../../../../crates/oah-core/tests/dto_samples/wiki/apply_req_setup.json";
import applyReqCopy from "../../../../crates/oah-core/tests/dto_samples/wiki/apply_req_copy.json";
import searchReq from "../../../../crates/oah-core/tests/dto_samples/wiki/search_req.json";

afterEach(async () => {
  await unmount();
});

const OPEN = "Wiki Setup & Search";
const id = sampleProject.id;
const secondProject = { ...sampleProject, id: "second-project", root: "/example/second" };
const forProject = <T extends { project_id: string }>(sample: T, projectId = id): T => ({ ...sample, project_id: projectId });

/** One folder found, not yet in the settings: the state the shared setup plan and apply request are about. */
const overviewNotes: ProjectWikiDto = {
  ...sampleWiki.overviewReady,
  project_id: id,
  configured: { dir: "docs/wiki", explicit: false },
  target: { state: "incomplete", dir: "notes", creates: 4, sets_dir: true, error: null },
};
const copyPlan: WikiPlanDto = { ...sampleWiki.planCopy, project_id: id, blocked: [], blocked_total: 0 };

async function openPage(t: FakeTransport = createFakeTransport({ project: sampleProject })) {
  await mount(<App transport={t} />);
  await click(button("Team Workflow"));
  return t;
}

async function openWiki(overview?: ProjectWikiDto, t: FakeTransport = createFakeTransport({ project: sampleProject })) {
  if (overview) t.override.projectWikiOverview = async () => overview;
  await openPage(t);
  await click(button(OPEN));
  return t;
}

async function type(element: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
  await emit(() => element.dispatchEvent(new Event("input", { bubbles: true })));
}

const panel = () => document.querySelector<HTMLElement>(".wiki-panel");
const text = () => panel()?.textContent ?? "";
const feedback = () => panel()?.querySelector(".tool-settings-feedback")?.textContent ?? "";
const count = (t: { calls: string[] }, name: string) => t.calls.filter((call) => call === name).length;
const wikiCalls = (t: { calls: string[] }) => t.calls.filter((call) => call.includes("wiki"));
const hasButton = (label: string | RegExp) => byText("button", label) !== null;
const radio = (dir: string) => panel()!.querySelector<HTMLInputElement>(`input[type="radio"][value="${dir}"]`)!;
const row = (file: string) => panel()!.querySelector<HTMLElement>(`tr[data-file="${file}"]`)!;
const query = () => document.getElementById("project-wiki-query") as HTMLInputElement;
const panelButtons = () => Array.from(panel()!.querySelectorAll("button")).map((node) => node.textContent);
const searchStatus = () => panel()?.querySelector(".wiki-search-status")?.textContent ?? "";

async function reviewCopy(t: FakeTransport) {
  t.override.projectWikiPlan = async () => copyPlan;
  await click(radio("notes"));
  await click(button("Preview Copy to docs/wiki"));
}

describe("Project Wiki panel (standalone test transport, nothing is written)", () => {
  it("reads nothing about the wiki until its button is pressed, then only the overview", async () => {
    const t = await openPage();
    expect(wikiCalls(t)).toEqual([]);
    await click(button(OPEN));
    expect(wikiCalls(t)).toEqual(["project_wiki_overview"]);
    expect(document.activeElement).toBe(panel());
    expect(panel()?.closest("[data-tool]")?.getAttribute("data-tool")).toBe("wiki");
    expect(text()).toContain("No wiki folder was found. Setup creates docs/wiki with 5 standard files.");
    // Nothing exists to search or copy yet.
    expect(query()).toBeNull();
    expect(hasButton(/^Preview Copy/)).toBe(false);
    await click(button("Preview Setup"));
    expect(t.wikiPlanRequests).toEqual([{ project_id: id, input: { action: "setup", selected: null, today: null } }]);
    expect(count(t, "start_project_wiki_apply")).toBe(0);
  });

  it("only one of the wiki panel and a settings editor is open", async () => {
    const t = await openPage();
    await click(button("Edit Jev Settings"));
    expect(hasButton(OPEN)).toBe(false);
    await click(button("Cancel"));
    await click(button(OPEN));
    expect(hasButton("Edit Project Wiki Settings")).toBe(false);
    expect(hasButton("Edit Jev Settings")).toBe(false);
    expect(document.querySelector('[data-tool="jev"] .tool-settings-wait')?.textContent).toBe("Close the wiki panel to edit these.");
    await click(button("Close"));
    expect(panel()).toBeNull();
    expect(document.activeElement).toBe(button(OPEN));
    expect(hasButton("Edit Jev Settings")).toBe(true);
    expect(count(t, "project_wiki_overview")).toBe(1);
  });

  it("shows one plan in full, applies exactly that plan with its date and upgrade approval, and reports the result", async () => {
    const t = await openWiki(overviewNotes);
    expect(text()).toContain("The wiki folder is notes. Setup adds 4 standard files it does not have and changes none that are there.");
    expect(text()).toContain("Found: notes (an Obsidian vault, has index.md)");
    await click(button("Preview Setup"));
    expect(t.wikiPlanRequests).toEqual([{ project_id: id, input: { action: "setup", selected: "notes", today: null } }]);
    expect(panel()?.getAttribute("data-phase")).toBe("review");
    expect(document.activeElement).toBe(panel());
    expect(panel()?.querySelector("h4")?.textContent).toBe("Review: Set Up the Wiki in notes");
    expect(text()).toContain("Folders to create: notes/templates");
    expect(row("index.md").getAttribute("data-action")).toBe("keep");
    expect(row("index.md").querySelector("td")?.textContent).toBe("Keep: already there, not changed");
    expect(row("templates/note.md").querySelector("td")?.textContent).toBe("Create");
    const setting = panel()!.querySelector('tr[data-key="wiki.dir"]')!;
    expect(Array.from(setting.querySelectorAll("td")).map((cell) => cell.textContent)).toEqual(["not set", '"notes"']);
    expect(text()).toContain("The file as it is now will be kept as .oah/config.toml.v0.4.bak");
    expect(feedback()).toContain("Nothing is written yet.");

    // The upgrade needs its own acknowledgement before anything is started.
    const applyButton = button("Create 4 Files");
    expect(applyButton.getAttribute("aria-disabled")).toBe("true");
    await click(applyButton);
    expect(count(t, "start_project_wiki_apply")).toBe(0);
    expect(feedback()).toContain("Tick the upgrade box to continue.");
    await click(document.getElementById("project-wiki-upgrade-ack")!);
    await click(button("Create 4 Files"));
    expect(t.wikiApplyRequests).toEqual([forProject(applyReqSetup)]);

    expect(panel()?.getAttribute("data-phase")).toBe("apply");
    expect(button("Close").getAttribute("aria-disabled")).toBe("true");
    await emit(() => t.emitWikiApply({ event: "progress", data: { phase: "write-files", done: 2, total: 4 } }));
    expect(panel()?.querySelector("progress")?.getAttribute("value")).toBe("2");
    expect(panel()?.querySelector(".wiki-progress")?.textContent).toBe("2 of 4");
    expect(feedback()).toBe("Writing files…");

    await emit(() => t.emitWikiApply({ event: "done", data: forProject(sampleWiki.applyDone) }));
    expect(panel()?.getAttribute("data-phase")).toBe("result");
    expect(panel()?.querySelector("h4")?.textContent).toBe("Wiki Result: Done");
    expect(text()).toContain("Done. The wiki is in notes.");
    expect(text()).toContain("4 files created, 1 kept.");
    expect(row("log.md").getAttribute("data-status")).toBe("created");
    expect(panel()?.querySelector(".wiki-settings-result")?.textContent).toBe(
      "Saved the wiki folder to .oah/config.toml. The file was upgraded; the original is kept as .oah/config.toml.v0.4.bak.",
    );
    expect(hasButton("Preview Again")).toBe(false);
    // The cards are checked again, and going back reads the folders again.
    expect(count(t, "project_tools_status")).toBe(2);
    await click(button("Back to the Wiki Overview"));
    expect(count(t, "project_wiki_overview")).toBe(2);
    expect(count(t, "start_project_wiki_apply")).toBe(1);
  });

  it("asks for a choice among several folders and previews a copy with what blocks it, what is left and what is kept", async () => {
    const t = await openWiki(forProject(sampleWiki.overviewChoose));
    expect(text()).toContain("More than one folder looks like the wiki. Choose the one to use.");
    expect(text()).toContain("Choose a folder to set up, copy or search.");
    expect(text()).toContain("knowledge/wiki: OAH_WIKI_PATH_UNSAFE");
    expect(hasButton("Preview Setup")).toBe(false);
    expect(query()).toBeNull();
    expect(radio("wiki").checked || radio("notes").checked).toBe(false);

    await click(radio("notes"));
    expect(radio("notes").checked).toBe(true);
    expect(hasButton("Preview Setup")).toBe(true);
    expect(query()).not.toBeNull();
    await click(button("Preview Copy to docs/wiki"));
    expect(t.wikiPlanRequests).toEqual([{ project_id: id, input: { action: "copy", source: "notes" } }]);
    expect(panel()?.querySelector("h4")?.textContent).toBe("Review: Copy notes to docs/wiki");
    expect(text()).toContain("notes stays as it is, and nothing in docs/wiki is replaced.");
    expect(row("a.md").querySelector("td")?.textContent).toBe("Keep: the same contents are already there");
    expect(row("deep/c.md").querySelectorAll("td")[1]?.textContent).toBe(showBytes(3_500_000));
    expect(text()).toContain("1 thing stops the copy.");
    expect(text()).toContain("link.md:7 A link in it leaves the wiki folder");
    expect(text()).toContain("Not copied on purpose.obsidian/workspace.json");
    expect(text()).toContain("1 link already leads nowhere.");
    expect(text()).toContain('impact.wiki_dirs stays ["notes", "adr"].');
    // A blocked plan has no apply button.
    expect(hasButton(/^Copy \d/)).toBe(false);
    expect(feedback()).toBe("This plan cannot be applied until what blocks it is fixed.");

    // Going back keeps the choice and reads nothing again.
    await click(button("Back to the Wiki Overview"));
    expect(radio("notes").checked).toBe(true);
    expect(count(t, "project_wiki_overview")).toBe(1);
    expect(count(t, "start_project_wiki_apply")).toBe(0);
  });

  it("stops before the next file on request, shows the partial result and previews the same request again", async () => {
    const t = await openWiki(forProject(sampleWiki.overviewChoose));
    await reviewCopy(t);
    await click(button("Copy 2 Files"));
    expect(t.wikiApplyRequests).toEqual([forProject(applyReqCopy)]);
    await click(button("Stop Before the Next File"));
    expect(t.calls).toContain("task_cancel:wiki-apply-1");
    expect(feedback()).toBe("Stopping before the next file…");
    await click(button("Stop Before the Next File"));
    expect(t.calls.filter((call) => call.startsWith("task_cancel"))).toHaveLength(1);

    await emit(() => t.emitWikiApply({ event: "done", data: forProject(sampleWiki.applyStopped) }));
    expect(panel()?.querySelector("h4")?.textContent).toBe("Wiki Result: Not Finished");
    expect(text()).toContain("Stopped at your request. The files already written stay; the rest were not started.");
    expect(text()).toContain("1 file created, 0 kept, 2 not started.");
    expect(row("b.md").querySelector("td")?.textContent).toBe("Not started");
    expect(panel()?.querySelector(".wiki-settings-result")?.textContent).toBe(
      "The project's settings were not saved, because not every file is in place.",
    );
    expect(count(t, "project_tools_status")).toBe(2);
    await click(button("Preview Again"));
    expect(t.wikiPlanRequests[1]).toEqual({ project_id: id, input: { action: "copy", source: "notes" } });
    expect(panel()?.getAttribute("data-phase")).toBe("review");
    expect(count(t, "start_project_wiki_apply")).toBe(1);
  });

  it("drops a plan the project moved on from and says what a refused or interrupted apply left", async () => {
    const t = await openWiki(forProject(sampleWiki.overviewChoose));
    await reviewCopy(t);
    await click(button("Copy 2 Files"));
    await emit(() =>
      t.emitWikiApply({ event: "failed", data: { code: "OAH_PLAN_STALE", message: "the approved plan is not the plan now", hint: null } }),
    );
    expect(panel()?.getAttribute("data-phase")).toBe("overview");
    expect(feedback()).toContain("Not applied: the project changed after the preview. Preview again. OAH_PLAN_STALE");
    expect(hasButton("Copy 2 Files")).toBe(false);
    // The old overview is not shown as if it were current.
    expect(panel()?.querySelector(".wiki-choice")).toBeNull();
    await click(button("Preview Again"));
    expect(t.wikiPlanRequests).toHaveLength(2);

    // A stop that arrives before the first file returns to the same review.
    await click(button("Copy 2 Files"));
    await emit(() => t.emitWikiApply({ event: "cancelled" }));
    expect(panel()?.getAttribute("data-phase")).toBe("review");
    expect(feedback()).toContain("Stopped before any file was written.");

    // Another apply already runs for this project.
    t.override.startProjectWikiApply = async () => {
      throw { code: "OAH_TASK_BUSY", message: "busy", hint: "Wait for it." };
    };
    await click(button("Copy 2 Files"));
    expect(panel()?.getAttribute("data-phase")).toBe("review");
    expect(feedback()).toContain("Not started. Nothing was written. OAH_TASK_BUSY: busy Wait for it.");
    delete t.override.startProjectWikiApply;

    // The files are in place but the settings file changed meanwhile.
    await click(button("Copy 2 Files"));
    await emit(() => t.emitWikiApply({ event: "done", data: forProject(sampleWiki.applySettingsStale) }));
    expect(panel()?.querySelector("h4")?.textContent).toBe("Wiki Result: Not Finished");
    expect(panel()?.querySelector(".wiki-settings-result")?.textContent).toContain(
      "The project's settings were not saved: the settings file changed after the preview. The files stay. OAH_PLAN_STALE",
    );
    expect(hasButton("Preview Again")).toBe(true);
  });

  it("does not show a result that names another plan", async () => {
    const t = await openWiki(forProject(sampleWiki.overviewChoose));
    await reviewCopy(t);
    await click(button("Copy 2 Files"));
    await emit(() => t.emitWikiApply({ event: "done", data: { ...forProject(sampleWiki.applyDone), plan_id: "sha256:other" } }));
    expect(panel()?.getAttribute("data-phase")).toBe("overview");
    expect(feedback()).toContain("UI_APPLY_RESPONSE_MISMATCH");
    expect(text()).not.toContain("Done.");
    await click(button("Look Again"));
    expect(count(t, "project_wiki_overview")).toBe(2);
  });

  it("says when there is nothing to do and when the default folder cannot be saved as the choice", async () => {
    const t = await openWiki(overviewNotes);
    t.override.projectWikiPlan = async () => ({
      ...forProject(sampleWiki.planSetup),
      files: sampleWiki.planSetup.files.map((file) => ({ ...file, action: "keep" as const })),
      folders: [],
      writes: 0,
      settings: null,
      choice_not_saved: true,
    });
    await click(button("Preview Setup"));
    expect(text()).toContain("notes is the default folder, and a default is never written to the file.");
    expect(feedback()).toBe("Nothing to do: every file is in place and the settings already lead to this folder.");
    expect(panelButtons()).toEqual(["Back to the Wiki Overview", "Close"]);
  });

  it("drops answers and events that arrive after the project changed or the panel closed", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectOpenDialog = async () => secondProject;
    const lateOverview = deferred<ProjectWikiDto>();
    t.override.projectWikiOverview = () => lateOverview.promise;
    await openPage(t);
    await click(button(OPEN));
    expect(feedback()).toBe("Looking for wiki folders… Nothing is changed.");
    await click(button("+ Open…"));
    expect(panel()).toBeNull();
    await emit(() => lateOverview.resolve(forProject(sampleWiki.overviewChoose)));
    expect(panel()).toBeNull();
    expect(hasButton(OPEN)).toBe(true);

    // An apply started under the second project reports after the first one is back: nothing is shown or re-checked.
    t.override.projectWikiOverview = async ({ project_id }) => forProject(sampleWiki.overviewChoose, project_id);
    t.override.projectWikiPlan = async ({ project_id }) => ({ ...copyPlan, project_id });
    await click(button(OPEN));
    await click(radio("notes"));
    await click(button("Preview Copy to docs/wiki"));
    await click(button("Copy 2 Files"));
    t.override.projectOpenDialog = async () => sampleProject;
    await click(button("+ Open…"));
    expect(panel()).toBeNull();
    const checks = count(t, "project_tools_status");
    await emit(() => t.emitWikiApply({ event: "done", data: forProject(sampleWiki.applyDone, secondProject.id) }));
    expect(panel()).toBeNull();
    expect(count(t, "project_tools_status")).toBe(checks);

    // A plan that answers after Close opens nothing, and the panel starts over.
    const latePlan = deferred<WikiPlanDto>();
    t.override.projectWikiPlan = () => latePlan.promise;
    await click(button(OPEN));
    await click(radio("notes"));
    await click(button("Preview Setup"));
    await click(button("Close"));
    await emit(() => latePlan.resolve(copyPlan));
    expect(panel()).toBeNull();
    expect(count(t, "start_project_wiki_apply")).toBe(1);
  });

  it("never uses an overview or a plan that names another project", async () => {
    const t = await openWiki(forProject(sampleWiki.overviewChoose, "some-other-project"));
    expect(feedback()).toContain("UI_PROJECT_CHANGED");
    expect(panel()?.querySelector(".wiki-choice")).toBeNull();
    t.override.projectWikiOverview = async () => forProject(sampleWiki.overviewChoose);
    await click(button("Look Again"));
    t.override.projectWikiPlan = async () => forProject(sampleWiki.planCopy, "some-other-project");
    await click(radio("notes"));
    await click(button("Preview Copy to docs/wiki"));
    expect(panel()?.getAttribute("data-phase")).toBe("overview");
    expect(feedback()).toContain("The preview could not be made. Nothing was written. UI_PROJECT_CHANGED");
    expect(count(t, "start_project_wiki_apply")).toBe(0);
  });

  it("searches the wiki text and shows each match as file, line and heading with its states", async () => {
    const t = await openWiki(forProject(sampleWiki.overviewReady));
    expect(text()).toContain("The wiki is in notes and has every standard file.");
    expect(hasButton("Preview Setup")).toBe(false);
    expect(panel()?.querySelector(`label[for="project-wiki-query"]`)?.textContent).toBe("Text to find in notes");

    await click(button("Search"));
    expect(searchStatus()).toBe("Enter the text to find.");
    expect(document.activeElement).toBe(query());
    expect(count(t, "project_wiki_search")).toBe(0);

    await type(query(), " release plan ");
    await click(button("Search"));
    expect(t.wikiSearchRequests).toEqual([{ project_id: id, input: { ...searchReq.input, max_hits: null } }]);
    expect(searchStatus()).toBe("2 matches for “release plan” in 42 files. More exist; add a word to narrow the search.");
    const hits = Array.from(panel()!.querySelectorAll(".wiki-hits li"));
    expect(hits.map((hit) => hit.querySelector("code")?.textContent)).toEqual(["decisions/2026-release.md:12", "index.md:4"]);
    expect(hits[0]?.textContent).toContain("under “Release plan”");
    expect(hits[0]?.querySelector(".wiki-hit-context")?.textContent).toBe("The release plan moves to the first week of March.");
    expect(text()).toContain("1 entry was not searched:attachments/big.md (too large)");

    t.override.projectWikiSearch = async ({ project_id }) => ({
      ...sampleWiki.search,
      project_id,
      hits: [],
      truncated: false,
      skipped: [],
      skipped_total: 0,
    });
    await type(query(), "zebra");
    await click(button("Search"));
    expect(searchStatus()).toBe("No match for “zebra” in 42 files.");
    expect(panel()?.querySelector(".wiki-hits")).toBeNull();

    t.override.projectWikiSearch = async () => {
      throw { code: "OAH_WIKI_MISSING", message: "notes is not there", hint: "Check again." };
    };
    await click(button("Search"));
    expect(panel()?.querySelector('.wiki-search [role="alert"]')?.textContent).toBe(
      "The search did not run. OAH_WIKI_MISSING: notes is not there Check again.",
    );
  });

  it("drops a search result for a folder that is no longer the chosen one", async () => {
    const t = await openWiki(forProject(sampleWiki.overviewChoose));
    await click(radio("wiki"));
    const late = deferred<WikiSearchDto>();
    t.override.projectWikiSearch = () => late.promise;
    await type(query(), "release");
    await click(button("Search"));
    expect(t.wikiSearchRequests[0]?.input.dir).toBe("wiki");
    expect(searchStatus()).toBe("Searching…");
    await click(radio("notes"));
    await emit(() => late.resolve({ ...forProject(sampleWiki.search), dir: "wiki" }));
    expect(panel()?.querySelector(".wiki-hits")).toBeNull();
    expect(searchStatus()).toBe("");
    expect(query().value).toBe("release");
  });
});
