// 한국어: Jev 기기 제어 테스트 — 열기 전에는 아무 호출도 없고, 미리보기 확인 뒤에만 동의·점검이 나가며, 키는 저장 호출에만 실리고 입력은 비워지고, 낡은·어긋난 응답은 성공으로 보이지 않는다 (가짜 값·독립 테스트 전송 계층).
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { JevReply } from "../src/jevContract";
import { button, byText, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleJevReply, sampleProject } from "./support/fakeTransport";

afterEach(async () => {
  await unmount();
});

const credentialFixture = "fake-key-0000";
const secondProject = { ...sampleProject, id: "second-project", root: "/example/second" };

async function openPage(t = createFakeTransport({ project: sampleProject })) {
  await mount(<App transport={t} />);
  await click(button("Team Workflow"));
  return t;
}
async function manage(t = createFakeTransport({ project: sampleProject })) {
  await openPage(t);
  await click(button("Manage Jev on this device"));
  return t;
}
const kinds = (t: { jevRequests: { input: { kind: string } }[] }) => t.jevRequests.map((r) => r.input.kind);
const panel = () => document.getElementById("project-tool-jev-controls");
const feedback = () => panel()?.querySelector(".tool-settings-feedback");
const has = (text: string | RegExp) => byText("button", text) !== null;

async function type(element: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(element, value);
  await emit(() => element.dispatchEvent(new Event("input", { bubbles: true })));
}

describe("Jev device controls (fake transport, no real key or network)", () => {
  it("reads nothing until Manage Jev is pressed, then reads status and key state only", async () => {
    const t = await openPage();
    expect(kinds(t)).toEqual([]);
    expect(has("Manage Jev on this device")).toBe(true);
    await click(button("Manage Jev on this device"));
    expect(t.jevRequests).toEqual([
      { project_id: sampleProject.id, input: { kind: "status" } },
      { project_id: sampleProject.id, input: { kind: "key-state" } },
    ]);
    expect(document.activeElement).toBe(panel());
    expect(panel()?.textContent).toContain("Not approved on this device.");
    expect(panel()?.textContent).toContain("https://api.typesafe.ai/v1/systemone");
    expect(panel()?.textContent).toContain("No key is stored on this device.");
    expect(has("Disable on this device")).toBe(false);
  });

  it("sends nothing to approve until the preview is confirmed, and shows success only for the matching answer", async () => {
    const t = await manage();
    await click(button("Review Impact Opt-in"));
    expect(kinds(t)).toEqual(["status", "key-state", "prepare"]);
    const region = document.querySelector<HTMLElement>(".jev-preview")!;
    expect(document.activeElement).toBe(region);
    expect(region.textContent).toContain("https://api.typesafe.ai/v1/systemone");
    expect(region.textContent).toContain("At most one request");
    expect(region.textContent).toContain("may charge");
    expect(region.textContent).toContain("No file contents");
    expect(region.querySelector("a")).toBeNull();
    await click(button("Approve on This Device"));
    expect(t.jevRequests.at(-1)).toEqual({ project_id: sampleProject.id, input: { kind: "enable", token: "v1:impact:fake-token" } });
    expect(feedback()?.getAttribute("data-state")).toBe("saved");
    expect(has("Disable on this device")).toBe(true);
    expect(document.activeElement).toBe(feedback());
  });

  it("Disable on this device is available for an active approval and sends only disable", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectJevAction = async (req) =>
      req.input.kind === "status"
        ? { ...(sampleJevReply(req.project_id, "status") as JevReply), consent: "enabled" } as JevReply
        : sampleJevReply(req.project_id, req.input.kind);
    await manage(t);
    await click(button("Disable on this device"));
    expect(kinds(t)).toEqual(["status", "key-state", "disable"]);
    expect(feedback()?.getAttribute("data-state")).toBe("disabled");
    expect(has("Disable on this device")).toBe(false);
  });

  it("keeps the key in a temporary password field, sends it only in store-key and clears it", async () => {
    const t = await manage();
    await click(button("Save Key"));
    const input = document.getElementById("jev-key-input") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.autocomplete).toBe("off");
    expect(document.activeElement).toBe(input);
    await type(input, credentialFixture);
    await click(button("Save Key on This Device"));
    const keyed = t.jevRequests.filter((r) => JSON.stringify(r).includes(credentialFixture));
    expect(keyed).toEqual([{ project_id: sampleProject.id, input: { kind: "store-key", key: credentialFixture } }]);
    expect(input.value).toBe("");
    expect(document.getElementById("jev-key-input")).toBeNull();
    expect(document.body.innerHTML).not.toContain(credentialFixture);
    expect(panel()?.textContent).toContain("A key is stored on this device.");
    expect(has("Replace Key")).toBe(true);
  });

  it("Cancel and Escape empty the key field; deleting asks first", async () => {
    const t = await manage();
    await click(button("Save Key"));
    const input = document.getElementById("jev-key-input") as HTMLInputElement;
    await type(input, credentialFixture);
    await emit(() => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(input.value).toBe("");
    expect(document.getElementById("jev-key-input")).toBeNull();
    expect(kinds(t)).not.toContain("store-key");

    await click(button("Save Key"));
    await type(document.getElementById("jev-key-input") as HTMLInputElement, credentialFixture);
    await click(button("Save Key on This Device"));
    await click(button("Delete Key…"));
    expect(kinds(t)).not.toContain("delete-key");
    await click(button("Keep Key"));
    expect(kinds(t)).not.toContain("delete-key");
    await click(button("Delete Key…"));
    await click(button("Delete Key"));
    expect(kinds(t).at(-1)).toBe("delete-key");
    expect(panel()?.textContent).toContain("No key is stored on this device.");
  });

  it("the connection test has its own preview, sends one test and approves nothing", async () => {
    const t = await manage();
    await click(button("Review Connection Test"));
    expect(kinds(t).at(-1)).toBe("prepare-test");
    expect(document.querySelector(".jev-preview")?.textContent).toMatch(/no project data/);
    await click(button("Send Test Request"));
    expect(t.jevRequests.at(-1)).toEqual({ project_id: sampleProject.id, input: { kind: "test", token: "v1:probe:fake-token" } });
    expect(kinds(t)).not.toContain("enable");
    expect(feedback()?.textContent).toMatch(/Nothing was turned on/);
  });

  it("shows a refusal word, not success, when core refuses", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectJevAction = async (req) =>
      req.input.kind === "test"
        ? ({ project_id: req.project_id, action: "test", ok: false, status: "key-locked" } as JevReply)
        : sampleJevReply(req.project_id, req.input.kind);
    await manage(t);
    await click(button("Review Connection Test"));
    await click(button("Send Test Request"));
    expect(feedback()?.getAttribute("data-state")).toBe("refusal");
    expect(feedback()?.textContent).toMatch(/locked/);
  });

  it("blocks a custom destination from the device key and from approval", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectJevAction = async (req) =>
      req.input.kind === "status"
        ? ({ ...(sampleJevReply(req.project_id, "status") as JevReply), destination: { kind: "custom", url: "https://example.test/v1", model: "m" } } as JevReply)
        : sampleJevReply(req.project_id, req.input.kind);
    await manage(t);
    expect(panel()?.querySelector("[role=note]")?.textContent).toMatch(/never sent there/);
    expect(panel()?.querySelector("a")).toBeNull();
    expect(has("Review Impact Opt-in")).toBe(false);
    expect(has("Save Key")).toBe(false);
    expect(has("Review Connection Test")).toBe(false);
  });

  it("drops an answer for another project or another action, and an unknown field such as a key", async () => {
    for (const bad of [
      (req: { project_id: string }) => ({ ...sampleJevReply("other-project", "status") }),
      (req: { project_id: string }) => ({ ...sampleJevReply(req.project_id, "disable") }),
      (req: { project_id: string }) => ({ ...sampleJevReply(req.project_id, "status"), key: credentialFixture }),
    ]) {
      const t = createFakeTransport({ project: sampleProject });
      t.override.projectJevAction = async (req) => (req.input.kind === "status" ? (bad(req) as unknown as JevReply) : sampleJevReply(req.project_id, req.input.kind));
      await manage(t);
      expect(feedback()?.getAttribute("data-state")).toBe("unavailable");
      expect(panel()?.textContent).not.toContain("Approval on this device");
      expect(document.body.innerHTML).not.toContain(credentialFixture);
      await unmount();
    }
  });

  it("a transport failure is its own unavailable state", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectJevAction = async () => { throw { code: "FAKE_DOWN", message: "The core did not answer.", hint: null }; };
    await manage(t);
    expect(feedback()?.getAttribute("data-state")).toBe("unavailable");
    expect(feedback()?.textContent).toBe("The core did not answer.");
  });

  it("an answer that arrives after the project changed is dropped", async () => {
    const t = createFakeTransport({ project: sampleProject });
    t.override.projectOpenDialog = async () => secondProject;
    const late = deferred<JevReply>();
    t.override.projectJevAction = (req) => (req.input.kind === "status" ? late.promise : Promise.resolve(sampleJevReply(req.project_id, req.input.kind)));
    await manage(t);
    expect(feedback()?.getAttribute("data-state")).toBe("loading");
    await click(button("+ Open…"));
    expect(panel()).toBeNull();
    await emit(() => late.resolve(sampleJevReply(sampleProject.id, "status")));
    expect(panel()).toBeNull();
    expect(has("Manage Jev on this device")).toBe(true);
    expect(kinds(t)).not.toContain("enable");
  });

  it("Close Jev Controls returns focus to Manage and stops a late answer from reopening it", async () => {
    const t = createFakeTransport({ project: sampleProject });
    const late = deferred<JevReply>();
    t.override.projectJevAction = (req) => (req.input.kind === "status" ? late.promise : Promise.resolve(sampleJevReply(req.project_id, req.input.kind)));
    await manage(t);
    await click(button("Close Jev Controls"));
    expect(document.activeElement).toBe(button("Manage Jev on this device"));
    await emit(() => late.resolve(sampleJevReply(sampleProject.id, "status")));
    expect(panel()).toBeNull();
  });
});
