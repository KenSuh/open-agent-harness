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

describe("C2 guards: write lock above the views (G31)", () => {
  it("keeps the install lock across Explore to Installed to Explore and refreshes inventory when the write settles away from Explore", async () => {
    const t = await exploreApp();
    let reads = 0;
    t.override.skillsInstalled = async () => {
      reads += 1;
      return [];
    };
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await review(t, pass);
    await click(button("Confirm install"));
    await click(button("Installed"));
    const before = reads;
    await emit(() => pending.resolve({ status: "installed", review_id: "rec-new" }));
    expect(reads).toBeGreaterThan(before);
    expect(document.querySelector(".toast")?.textContent).toContain("installed");
    await click(button("Explore"));
    expect(button("Confirm install").disabled).toBe(true);
    expect(t.applyRequests).toHaveLength(1);
  });

  it("keeps a pending write locked through A to B to A and shows no success while on B", async () => {
    const t = await exploreApp();
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await review(t, pass);
    await click(button("Confirm install"));
    t.override.projectOpenDialog = async () => otherProject;
    await click(button("+ Open…"));
    await emit(() => pending.resolve({ status: "installed", review_id: "rec-new" }));
    expect(document.querySelector(".toast")).toBeNull();
    expect(document.body.textContent).not.toContain("Installed tidy");
    t.override.projectOpenDialog = async () => sampleProject;
    await click(button("+ Open…"));
    expect(t.applyRequests).toHaveLength(1);
    await review(t, pass);
    // A's own finished write belongs to A's destination and still blocks a duplicate there.
    expect(button("Confirm install").disabled).toBe(true);
    expect(t.applyRequests).toHaveLength(1);
  });

  it("blocks a second click on A after returning to A while the first write still runs", async () => {
    const t = await exploreApp();
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await review(t, pass);
    await click(button("Confirm install"));
    t.override.projectOpenDialog = async () => otherProject;
    await click(button("+ Open…"));
    t.override.projectOpenDialog = async () => sampleProject;
    await click(button("+ Open…"));
    await review(t, pass);
    expect(button(/Installing/).disabled).toBe(true);
    await act(async () => button(/Installing/).click());
    expect(t.applyRequests).toHaveLength(1);
    await emit(() => pending.resolve({ status: "installed", review_id: "rec-new" }));
    expect(panel()?.textContent).toContain("Installed tidy");
  });
});

describe("C2 guards: confirmed request and validated response (G32)", () => {
  it("keeps destination and engine after an agent list refresh while the request runs", async () => {
    const t = await exploreApp();
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await review(t, pass);
    await click(button("Confirm install"));
    t.agents = [agent("claude", false), agent("codex")];
    await emit(() => t.emitAgentsChanged({ agents: ["claude"] }));
    expect(panel()?.textContent).toContain(`${sampleProject.root}/.claude/skills/tidy`);
    expect(panel()?.textContent).not.toContain(".agents/skills/tidy");
    await emit(() => pending.resolve({ status: "installed", review_id: "rec-new" }));
    expect(panel()?.textContent).toContain(`${sampleProject.root}/.claude/skills/tidy`);
    expect(t.applyRequests[0]?.engine).toBe("claude");
  });

  it.each([
    ["a different review id", { status: "installed", review_id: "rec-other" } as const],
    ["an update status for an install", { status: "updated", review_id: "rec-new" } as const],
    ["an unchanged install for another review", { status: "unchanged", review_id: "rec-other" } as const],
  ])("rejects %s as a success for an install", async (_label, response) => {
    const t = await exploreApp();
    t.override.applyProjectSkill = async () => response;
    await review(t, pass);
    await click(button("Confirm install"));
    expect(alertText()).toContain("UI_APPLY_RESPONSE_MISMATCH");
    expect(panel()?.textContent).not.toContain("Installed tidy");
    expect(document.querySelector(".toast")).toBeNull();
  });

  it("distinguishes an exact duplicate install (unchanged) from an install", async () => {
    const t = await exploreApp();
    t.override.applyProjectSkill = async () => ({ status: "unchanged", review_id: "rec-new" });
    await review(t, pass);
    await click(button("Confirm install"));
    expect(panel()?.textContent).toContain("Already installed; nothing changed.");
    expect(panel()?.textContent).not.toContain("Installed tidy");
  });

  it.each([
    ["an install status", { status: "installed", review_id: "rec-new" } as const],
    ["another review id", { status: "updated", review_id: "rec-other" } as const],
    ["an unchanged update for the new review", { status: "unchanged", review_id: "rec-new" } as const],
  ])("rejects %s as a success for an update", async (_label, response) => {
    const { t } = await installedApp();
    t.override.applyProjectSkill = async () => response;
    await check(t, pass);
    await click(button("Apply update"));
    expect(alertText()).toContain("UI_APPLY_RESPONSE_MISMATCH");
    expect(document.querySelector(".toast")).toBeNull();
  });

  it("accepts an unchanged update that returns the prior expected receipt", async () => {
    const { t } = await installedApp();
    t.override.applyProjectSkill = async () => ({ status: "unchanged", review_id: "rec-old" });
    await check(t, pass);
    await click(button("Apply update"));
    expect(alertText()).toBe("");
    expect(document.querySelector(".toast")?.textContent).toContain("already current");
  });
});

describe("C2 guards: row compatibility, stale outcomes, labelling (G33 G34 G35)", () => {
  it("explains an incompatible update from the fresh review's engine list and sends nothing", async () => {
    const { t } = await installedApp();
    await check(t, { ...pass, gate: { ...pass.gate, compatible: ["codex"] } });
    expect(document.querySelector('[aria-label="Update not compatible"]')?.textContent).toContain("not compatible with Claude Code");
    expect(byText("button", "Apply update")).toBeNull();
    expect(t.applyRequests).toHaveLength(0);
  });

  it("offers the update when the fresh review lists the row's engine", async () => {
    const { t } = await installedApp();
    await check(t, { ...pass, gate: { ...pass.gate, compatible: ["claude"] } });
    expect(button("Apply update").disabled).toBe(false);
  });

  it("clears the superseded apply error when a fresh check starts, and keeps it for the same candidate until then", async () => {
    const { t } = await installedApp();
    await check(t, pass);
    t.override.applyProjectSkill = async () => {
      throw { code: "OAH_LOCAL_EDIT", message: "Local edits found.", hint: null };
    };
    await click(button("Apply update"));
    expect(alertText()).toContain("OAH_LOCAL_EDIT");
    await click(button("Apply update"));
    expect(alertText()).toContain("OAH_LOCAL_EDIT");
    await click(button("Check for update"));
    expect(alertText()).toBe("");
    await emit(() => t.emitReview({ event: "done", data: pass }));
    expect(alertText()).toBe("");
    expect(button("Apply update").disabled).toBe(false);
  });

  it("shows repository, folder and pinned commit together for the review and the update candidate", async () => {
    const t = await exploreApp();
    await review(t, pass);
    const facts = (root: ParentNode) => Array.from(root.querySelectorAll("dt")).map((dt) => dt.textContent);
    const result = document.querySelector('[aria-label="Review result"]')!;
    expect(facts(result)).toEqual(expect.arrayContaining(["GitHub repository", "Folder in repository", "Pinned commit"]));
    expect(result.textContent).toContain("example/skills");
    expect(result.textContent).toContain("skills/tidy");
    expect(result.textContent).toContain(record.source.commit);
    await unmount();
    const { t: t2 } = await installedApp();
    await check(t2, pass);
    const candidate = document.querySelector('[aria-label="Update available"]')!;
    expect(facts(candidate)).toEqual(expect.arrayContaining(["GitHub repository", "Folder in repository", "Pinned commit"]));
    expect(candidate.textContent).toContain("example/skills");
    expect(candidate.textContent).toContain(record.source.commit);
  });

  it("associates controls with the skill and keeps one live status region across the whole update", async () => {
    const { t } = await installedApp();
    const group = document.querySelector<HTMLElement>(".managed-update")!;
    const nameId = group.getAttribute("aria-labelledby")!;
    expect(document.getElementById(nameId)?.textContent).toBe("tidy");
    expect(group.getAttribute("role")).toBe("group");
    expect(button("Check for update").getAttribute("aria-describedby")).toBe(nameId);
    const region = group.querySelector('[role="status"]')!;
    expect(region.getAttribute("aria-live")).toBe("polite");
    await check(t, pass);
    expect(button("Apply update").getAttribute("aria-describedby")).toBe(nameId);
    await click(button("Apply update"));
    expect(group.querySelector('[role="status"]')).toBe(region);
  });

  it("labels the install panel's controls with the skill and keeps its status region mounted", async () => {
    const t = await exploreApp();
    await review(t, pass);
    const region = panel()!.querySelector('[role="status"]')!;
    const skillId = button("Confirm install").getAttribute("aria-describedby")!;
    expect(document.getElementById(skillId)?.textContent).toBe("tidy");
    await click(button("Confirm install"));
    expect(panel()!.querySelector('[role="status"]')).toBe(region);
    expect(region.textContent).toContain("Installed tidy");
  });
});

describe("C3 guard: a visible confirmation never retargets another project (G36)", () => {
  /** Resolves the open dialog and clicks the still-mounted confirmation before React commits the new project. */
  async function clickDuringSwitch(open: (project: ProjectDto) => void, target: ProjectDto, stale: HTMLButtonElement) {
    await act(async () => {
      open(target);
      for (let i = 0; i < 6; i++) await Promise.resolve();
      stale.click(); // the old project's confirmation is still on screen
    });
    await flush();
  }

  function controlledOpen(t: FakeTransport) {
    const opens: Array<(p: ProjectDto) => void> = [];
    t.override.projectOpenDialog = () => new Promise<ProjectDto>((resolve) => opens.push(resolve));
    return (project: ProjectDto) => opens.shift()!(project);
  }

  it("install: sends nothing when project B arrives before the A confirmation unmounts", async () => {
    const t = await exploreApp();
    await review(t, pass);
    const stale = button("Confirm install");
    const open = controlledOpen(t);
    await click(button("+ Open…"));
    await clickDuringSwitch(open, otherProject, stale);
    expect(t.applyRequests).toHaveLength(0);
  });

  it("install: sends nothing for A to B to A with the old confirmation still mounted", async () => {
    const t = await exploreApp();
    await review(t, pass);
    const stale = button("Confirm install");
    const open = controlledOpen(t);
    await click(button("+ Open…"));
    await act(async () => open(otherProject));
    await click(button("+ Open…"));
    await clickDuringSwitch(open, sampleProject, stale);
    expect(t.applyRequests).toHaveLength(0);
  });

  it("update: sends nothing when project B arrives before the A confirmation unmounts", async () => {
    const { t } = await installedApp();
    await check(t, pass);
    const stale = button("Apply update");
    const open = controlledOpen(t);
    await click(button("+ Open…"));
    await clickDuringSwitch(open, otherProject, stale);
    expect(t.applyRequests).toHaveLength(0);
  });

  it("update: sends nothing for A to B to A with the old confirmation still mounted", async () => {
    const { t } = await installedApp();
    await check(t, pass);
    const stale = button("Apply update");
    const open = controlledOpen(t);
    await click(button("+ Open…"));
    await act(async () => open(otherProject));
    await click(button("+ Open…"));
    await clickDuringSwitch(open, sampleProject, stale);
    expect(t.applyRequests).toHaveLength(0);
  });

  it("still sends the confirmed project after a same-selection project refresh", async () => {
    const t = await exploreApp();
    await review(t, pass);
    const open = controlledOpen(t);
    await click(button("+ Open…"));
    await act(async () => open({ ...sampleProject }));
    await review(t, pass);
    await click(button("Confirm install"));
    expect(t.applyRequests.map((r) => r.project)).toEqual([sampleProject.id]);
  });
});

describe("C2/C5/C6 guards from review R2 (G32 G35 G37)", () => {
  const updatedRow = { name: "tidy", engine: "claude", scope: "project", location: `${sampleProject.root}/.claude/skills/tidy`, managed: { ...managed, review_id: "rec-new" } } satisfies InstalledSkillDto;
  const retryable = { code: "OAH_RETRY", message: "Try again.", hint: null } as const;

  it("never retargets a visible project A confirmation during a project B response", async () => {
    const t = await exploreApp();
    const opening = deferred<ProjectDto>();
    t.override.projectOpenDialog = () => opening.promise;
    await review(t, pass);
    await click(button("+ Open…"));

    await act(async () => {
      opening.resolve(otherProject);
      for (let step = 0; step < 5; step++) await Promise.resolve();
      expect(panel()?.textContent).toContain(updatedRow.location);
      button("Confirm install").click();
    });

    expect(t.applyRequests.map((request) => request.project)).not.toContain(otherProject.id);
  });

  it("never retargets a visible project A update during a project B response", async () => {
    const { t } = await installedApp();
    const opening = deferred<ProjectDto>();
    t.override.projectOpenDialog = () => opening.promise;
    await check(t, pass);
    await click(button("+ Open…"));

    await act(async () => {
      opening.resolve(otherProject);
      for (let step = 0; step < 5; step++) await Promise.resolve();
      expect(byText(".installed-list code", updatedRow.location)?.textContent).toBe(updatedRow.location);
      button("Apply update").click();
    });

    expect(t.applyRequests.map((request) => request.project)).not.toContain(otherProject.id);
  });

  it.each(["installed", "unchanged"] as const)("keeps an Explore %s result out of the Installed row's update status", async (status) => {
    const t = await exploreApp();
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    t.override.skillsInstalled = async () => status === "unchanged" ? [updatedRow] : [];
    await review(t, pass);
    await click(button("Confirm install"));
    await click(button("Installed"));

    t.override.skillsInstalled = async () => [updatedRow];
    await emit(() => pending.resolve({ status, review_id: "rec-new" }));

    expect(document.querySelector(".managed-update")?.getAttribute("role")).toBe("group");
    expect(document.querySelector(".managed-update [role=status]")?.textContent).toBe("");
  });

  it("sends the displayed warning acknowledgement when a failed install is retried after navigation", async () => {
    const t = await exploreApp();
    t.override.applyProjectSkill = () => Promise.reject(retryable);
    await review(t, warn);
    const check = panel()?.querySelector<HTMLInputElement>('input[type="checkbox"]');
    if (!check) throw new Error("The review warning acknowledgement is missing");
    await click(check);
    await click(button("Confirm install"));
    expect(alertText()).toContain(retryable.code);

    await click(button("Installed"));
    await click(button("Explore"));
    expect(panel()?.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(true);
    t.override.applyProjectSkill = undefined;
    await click(button("Confirm install"));

    expect(t.applyRequests.map((request) => request.acknowledge_warnings)).toEqual([true, true]);
  });

  it("allows revoking the warning acknowledgement after a failed update", async () => {
    const { t } = await installedApp();
    t.override.applyProjectSkill = () => Promise.reject(retryable);
    await check(t, warn);
    const checkbox = document.querySelector<HTMLInputElement>('.managed-update input[type="checkbox"]');
    if (!checkbox) throw new Error("The update warning acknowledgement is missing");
    await click(checkbox);
    await click(button("Apply update"));
    expect(alertText()).toContain(retryable.code);

    await click(checkbox);

    expect(checkbox.checked).toBe(false);
    expect(button("Apply update").disabled).toBe(true);
  });

  it.each([
    ["available", pass, /available/i],
    ["blocked", block, /blocked/i],
    ["incompatible", { ...pass, gate: { ...pass.gate, compatible: ["codex"] } }, /compatib/i],
  ] satisfies readonly (readonly [string, ReviewResultDto, RegExp])[])("announces when an update check is %s", async (_label, result, announcement) => {
    const { t } = await installedApp();

    await check(t, result);

    const region = document.querySelector('.managed-update [role="status"]');
    expect(region?.textContent).toMatch(announcement);
  });

  it("retains the live update status during the inventory read triggered by success", async () => {
    const { t } = await installedApp();
    const refresh = deferred<InstalledSkillDto[]>();
    const status = document.querySelector('.managed-update [role="status"]');
    expect(status?.isConnected).toBe(true);
    await check(t, pass);
    t.override.skillsInstalled = () => refresh.promise;

    await click(button("Apply update"));

    expect(status?.isConnected).toBe(true);
    expect(status?.textContent).toMatch(/updated/i);
    await emit(() => refresh.resolve([updatedRow]));
  });

  it("replaces a stale A inventory read after a pending install settles through A to B to A", async () => {
    const t = await exploreApp();
    const pending = deferred<ApplyProjectSkillDto>();
    const stale = deferred<InstalledSkillDto[]>();
    const bRow = { ...updatedRow, name: "b-only", location: `${otherProject.root}/.claude/skills/b-only` };
    t.override.applyProjectSkill = () => pending.promise;
    await review(t, pass);
    await click(button("Confirm install"));
    t.override.projectOpenDialog = async () => otherProject;
    await click(button("+ Open…"));
    t.override.skillsInstalled = async () => [bRow];
    await click(button("Installed"));
    expect(document.querySelector('[data-skill="b-only"]')).not.toBeNull();

    t.override.projectOpenDialog = async () => sampleProject;
    t.override.skillsInstalled = () => stale.promise;
    await click(button("+ Open…"));
    expect(document.querySelector('[data-skill="b-only"]')).toBeNull();
    t.override.skillsInstalled = async () => [updatedRow];
    await emit(() => pending.resolve({ status: "installed", review_id: "rec-new" }));
    await emit(() => stale.resolve([]));

    expect(document.querySelector('[data-skill="tidy"] code')?.textContent).toBe(updatedRow.location);
  });
});
