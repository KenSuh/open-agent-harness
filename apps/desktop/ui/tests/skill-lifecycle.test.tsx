// 한국어: Explore 설치·Installed 업데이트 확인 흐름 테스트 — 컴포넌트와 전송 계층 경계에서 확인·실패·중복 클릭·프로젝트 전환을 본다.
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { ApplyProjectSkillDto, InstalledSkillDto, ProjectDto, ReviewResultDto } from "../src/contract";
import { button, byText, click, emit, flush, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleAgents, sampleProject, type FakeTransport } from "./support/fakeTransport";

afterEach(unmount);

const record = {
  version: 1,
  id: "rec-new",
  source: { provider: "github", locator: "example/skills", commit: "0123456789abcdef0123456789abcdef01234567", tree: null, path: "skills/tidy" },
  frontmatter: { name: "tidy", description: "Tidies things." },
  license: { verdict: "recognized" as const, expression: "MIT", eligibility: "eligible" as const },
  status: "reviewed" as const,
  reason: null,
  checked_at: "2026-11-22T00:00:00Z",
};
const pass: ReviewResultDto = {
  record,
  gate: { gate: "oah-install-gate/v1", result: "pass", reasons: [], compatible: ["claude", "codex"] },
  unchanged: false,
};
const warn: ReviewResultDto = {
  ...pass,
  gate: { ...pass.gate, result: "warn", reasons: [{ kind: "screening", detail: "Runs a shell snippet.", lines: [4] }] },
};
const block: ReviewResultDto = { ...pass, gate: { ...pass.gate, result: "block", reasons: [{ kind: "license", detail: "No license.", lines: [] }] } };

const otherProject: ProjectDto = { ...sampleProject, id: "other-project", root: "/work/other" };

function agent(id: "claude" | "codex", installed = true) {
  return { ...sampleAgents.codexSignedOut, id, name: id, installed, status: "ok" as const, sign_in: "signed-in" as const };
}

async function review(t: FakeTransport, result: ReviewResultDto) {
  const input = document.querySelector<HTMLInputElement>('input[type="text"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "example/skills/skills/tidy");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    document.querySelector<HTMLFormElement>("form.review-form")!.requestSubmit();
  });
  await flush();
  await emit(() => t.emitReview({ event: "done", data: result }));
}

async function exploreApp(opts: { project?: ProjectDto | null; agents?: ReturnType<typeof agent>[] } = {}) {
  const t = createFakeTransport({ project: opts.project === undefined ? sampleProject : opts.project });
  t.agents = opts.agents ?? [agent("claude"), agent("codex")];
  await mount(<App transport={t} />);
  return t;
}

const panel = () => document.querySelector('[aria-label="Install in project"]');
const alertText = () => document.querySelector('[role="alert"]')?.textContent ?? "";

describe("Explore install", () => {
  it("writes nothing on a review result alone and shows destination before confirmation", async () => {
    const t = await exploreApp();
    await review(t, pass);
    expect(t.applyRequests).toHaveLength(0);
    expect(panel()?.textContent).toContain(`${sampleProject.root}/.claude/skills/tidy`);
    expect(t.calls).not.toContain("apply_project_skill");
  });

  it("sends the exact request on confirm, then shows the success", async () => {
    const t = await exploreApp();
    await review(t, pass);
    await click(button("Confirm install"));
    expect(t.applyRequests).toEqual([
      { project: sampleProject.id, engine: "claude", review_id: "rec-new", expected_review_id: null, acknowledge_warnings: false },
    ]);
    expect(panel()?.textContent).toContain("Installed tidy");
    expect(button("Confirm install").disabled).toBe(true);
  });

  it("offers the Codex destination when chosen and only compatible installed engines", async () => {
    const t = await exploreApp({ agents: [agent("claude", false), agent("codex")] });
    await review(t, { ...pass, gate: { ...pass.gate, compatible: ["claude", "codex"] } });
    expect(panel()?.querySelectorAll('input[type="radio"]')).toHaveLength(1);
    expect(panel()?.textContent).toContain(".agents/skills/tidy");
    await click(button("Confirm install"));
    expect(t.applyRequests[0]?.engine).toBe("codex");
  });

  it("offers no install when no compatible agent is installed", async () => {
    const t = await exploreApp({ agents: [agent("codex")] });
    await review(t, { ...pass, gate: { ...pass.gate, compatible: ["claude"] } });
    expect(panel()?.textContent).toContain("No compatible");
    expect(byText("button", "Confirm install")).toBeNull();
  });

  it("requires the warnings to be acknowledged before confirming", async () => {
    const t = await exploreApp();
    await review(t, warn);
    expect(panel()?.textContent).toContain("Runs a shell snippet.");
    expect(button("Confirm install").disabled).toBe(true);
    await click(panel()!.querySelector<HTMLInputElement>('input[type="checkbox"]')!);
    await click(button("Confirm install"));
    expect(t.applyRequests[0]?.acknowledge_warnings).toBe(true);
  });

  it("offers no install for a blocked review or without an open project", async () => {
    const t = await exploreApp();
    await review(t, block);
    expect(panel()).toBeNull();
    await unmount();
    const t2 = await exploreApp({ project: null });
    await review(t2, pass);
    expect(panel()).toBeNull();
    expect(document.body.textContent).toContain("Open a project to install");
  });

  it("shows a failure, keeps the confirmation, and retries", async () => {
    const t = await exploreApp();
    t.override.applyProjectSkill = async () => {
      throw { code: "OAH_SKILL_OCCUPIED", message: "Location is occupied.", hint: "Rename it." };
    };
    await review(t, pass);
    await click(button("Confirm install"));
    expect(alertText()).toContain("OAH_SKILL_OCCUPIED");
    expect(button("Confirm install").disabled).toBe(false);
    t.override.applyProjectSkill = undefined;
    await click(button("Confirm install"));
    expect(t.applyRequests).toHaveLength(2);
    expect(alertText()).toBe("");
    expect(panel()?.textContent).toContain("Installed tidy");
  });

  it("ignores repeated clicks while a write is in flight", async () => {
    const t = await exploreApp();
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await review(t, pass);
    const confirm = button("Confirm install");
    await click(confirm);
    await act(async () => confirm.click());
    expect(t.applyRequests).toHaveLength(1);
    await emit(() => pending.resolve({ status: "installed", review_id: "rec-new" }));
    expect(panel()?.textContent).toContain("Installed tidy");
  });

  it("ignores a late result after the project changed", async () => {
    const t = await exploreApp();
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await review(t, pass);
    await click(button("Confirm install"));
    t.override.projectOpenDialog = async () => otherProject;
    await click(button("+ Open…"));
    await emit(() => pending.resolve({ status: "installed", review_id: "rec-new" }));
    expect(document.body.textContent).not.toContain("Installed tidy");
    expect(document.querySelector(".toast")).toBeNull();
    // The request carried the handle of the project that was open at click time.
    expect(t.applyRequests[0]?.project).toBe(sampleProject.id);
  });
});

const managed = { review_id: "rec-old", snapshot_id: "snap-1", repo: "example/skills", path: "skills/tidy" };
const rows: InstalledSkillDto[] = [
  { name: "tidy", engine: "claude", scope: "project", location: `${sampleProject.root}/.claude/skills/tidy`, managed },
  { name: "hand-made", engine: "claude", scope: "project", location: `${sampleProject.root}/.claude/skills/hand-made` },
  { name: "user-tidy", engine: "codex", scope: "user", location: "~/.agents/skills/user-tidy", managed },
];

async function installedApp() {
  const t = createFakeTransport({ project: sampleProject });
  t.agents = [agent("claude"), agent("codex")];
  let inventory = rows;
  t.override.skillsInstalled = async () => inventory;
  await mount(<App transport={t} />);
  await click(button("Installed"));
  return { t, setInventory: (next: InstalledSkillDto[]) => (inventory = next) };
}

async function check(t: FakeTransport, result: ReviewResultDto) {
  await click(button("Check for update"));
  await emit(() => t.emitReview({ event: "done", data: result }));
}

describe("Installed update", () => {
  it("offers a check only for managed project rows, using that receipt's repo and path", async () => {
    const { t } = await installedApp();
    expect(document.querySelectorAll("button").length).toBeGreaterThan(0);
    expect(Array.from(document.querySelectorAll("button")).filter((b) => b.textContent === "Check for update")).toHaveLength(1);
    expect(document.body.textContent).toContain("hand-made");
    expect(document.body.textContent).toContain("user-tidy");
    await click(button("Check for update"));
    expect(t.reviewRequests).toEqual([{ spec: { repo: "example/skills", path: "skills/tidy" } }]);
    expect(t.applyRequests).toHaveLength(0);
  });

  it("reports an unchanged review as up to date with no apply", async () => {
    const { t } = await installedApp();
    await check(t, { ...pass, record: { ...record, id: "rec-old" }, unchanged: true });
    expect(document.body.textContent).toContain("Up to date");
    expect(byText("button", "Apply update")).toBeNull();
  });

  it("applies a changed review only after a click, with the exact old review id, then re-reads inventory", async () => {
    const { t, setInventory } = await installedApp();
    await check(t, pass);
    expect(t.applyRequests).toHaveLength(0);
    const reads = t.calls.filter((c) => c === "skills_installed").length;
    setInventory([{ ...rows[0]!, managed: { ...managed, review_id: "rec-new" } }]);
    await click(button("Apply update"));
    expect(t.applyRequests).toEqual([
      { project: sampleProject.id, engine: "claude", review_id: "rec-new", expected_review_id: "rec-old", acknowledge_warnings: false },
    ]);
    expect(t.calls.filter((c) => c === "skills_installed").length).toBeGreaterThan(reads);
    expect(document.querySelector(".toast")?.textContent).toContain("Updated tidy");
    expect(document.body.textContent).not.toContain("hand-made");
  });

  it("requires warning acknowledgement before applying", async () => {
    const { t } = await installedApp();
    await check(t, warn);
    expect(button("Apply update").disabled).toBe(true);
    await click(document.querySelector<HTMLInputElement>('.managed-update input[type="checkbox"]')!);
    await click(button("Apply update"));
    expect(t.applyRequests[0]?.acknowledge_warnings).toBe(true);
  });

  it("cannot apply a blocked result", async () => {
    const { t } = await installedApp();
    await check(t, block);
    expect(document.querySelector('[aria-label="Update blocked"]')?.textContent).toContain("No license.");
    expect(byText("button", "Apply update")).toBeNull();
    expect(t.applyRequests).toHaveLength(0);
  });

  it("shows a check failure and an apply failure, and allows retry", async () => {
    const { t } = await installedApp();
    await click(button("Check for update"));
    await emit(() => t.emitReview({ event: "failed", data: { code: "OAH_NETWORK", message: "Offline.", hint: null } }));
    expect(alertText()).toContain("OAH_NETWORK");
    await check(t, pass);
    t.override.applyProjectSkill = async () => {
      throw { code: "OAH_LOCAL_EDIT", message: "Local edits found.", hint: null };
    };
    await click(button("Apply update"));
    expect(alertText()).toContain("OAH_LOCAL_EDIT");
    expect(button("Apply update").disabled).toBe(false);
  });

  it("ignores a repeated Apply click while the write runs", async () => {
    const { t } = await installedApp();
    await check(t, pass);
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    const apply = button("Apply update");
    await click(apply);
    await act(async () => apply.click());
    expect(t.applyRequests).toHaveLength(1);
    await emit(() => pending.resolve({ status: "updated", review_id: "rec-new" }));
  });
});
