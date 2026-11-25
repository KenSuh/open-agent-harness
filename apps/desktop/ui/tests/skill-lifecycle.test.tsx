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
  source: {
    provider: "github",
    locator: "https://github.com/example/skills",
    commit: "0123456789abcdef0123456789abcdef01234567",
    tree: null,
    path: "skills/tidy",
    snapshot: "snap-new",
  },
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
const tidy = { name: "tidy", engine: "claude", scope: "project", location: `${sampleProject.root}/.claude/skills/tidy`, managed } satisfies InstalledSkillDto;
const updatedRow = { ...tidy, managed: { ...managed, review_id: record.id, snapshot_id: record.source.snapshot } };
const rows: InstalledSkillDto[] = [
  tidy,
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

/** Answers scan-triggered project reads inside one act and clicks the old button while it is still connected and enabled. */
async function clickAfterReads(t: FakeTransport, label: string, answers: readonly ProjectDto[]) {
  const stale = button(label);
  const reads = answers.map(() => deferred<ProjectDto>());
  const pending = [...reads];
  t.override.projectCurrent = () => {
    const next = pending.shift();
    if (!next) throw new Error("Unexpected project read");
    return next.promise;
  };
  await act(async () => {
    for (const [index, answer] of answers.entries()) {
      t.emitScan({ scope: "all", keys: [] });
      reads[index]!.resolve(answer);
      for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
    }
    expect(pending).toHaveLength(0);
    expect(stale.isConnected).toBe(true);
    expect(button(label)).toBe(stale);
    expect(stale.disabled).toBe(false);
    stale.click();
  });
  await flush();
}

const withSource = (result: ReviewResultDto, patch: Partial<ReviewResultDto["record"]["source"]>): ReviewResultDto => ({
  ...result,
  record: { ...result.record, source: { ...result.record.source, ...patch } },
});
const named = (result: ReviewResultDto, name: string, path: string): ReviewResultDto => ({
  ...result,
  record: { ...result.record, frontmatter: { ...result.record.frontmatter, name }, source: { ...result.record.source, path } },
});

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

  it("applies a same-source native GitHub review only after a click, with the exact old review id, then re-reads inventory (G39)", async () => {
    const { t, setInventory } = await installedApp();
    await check(t, pass);
    expect(t.applyRequests).toHaveLength(0);
    const reads = t.calls.filter((c) => c === "skills_installed").length;
    setInventory([updatedRow]);
    await click(button("Apply update"));
    expect(t.applyRequests).toEqual([
      { project: sampleProject.id, engine: "claude", review_id: "rec-new", expected_review_id: "rec-old", acknowledge_warnings: false },
    ]);
    expect(t.calls.filter((c) => c === "skills_installed").length).toBeGreaterThan(reads);
    expect(document.querySelector(".toast")?.textContent).toContain("Updated tidy");
    expect(document.body.textContent).not.toContain("hand-made");
  });

  it("keeps an update on its owning folder while a read-only alias and agent filters are present", async () => {
    const { t, setInventory } = await installedApp();
    const codex = {
      ...tidy,
      engine: "codex",
      compatible_agents: ["opencode", "antigravity", "gemini", "copilot", "cursor"],
      location: `${sampleProject.root}/.agents/skills/tidy`,
    } satisfies InstalledSkillDto;
    const legacy = {
      ...codex,
      compatible_agents: ["cursor"],
      location: `${sampleProject.root}/.codex/skills/tidy`,
      managed: undefined,
    } satisfies InstalledSkillDto;
    setInventory([legacy, codex]);
    await click(button("Refresh inventory"));

    await click(button("OpenCode 1"));
    expect(document.querySelector(".installed-list code")?.textContent).toBe(codex.location);
    expect(byText("button", "Check for update")).toBeNull();
    await click(button("Codex 2"));
    expect(button("Check for update").closest("li")?.querySelector("code")?.textContent).toBe(codex.location);
    await check(t, pass);

    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await click(button("Apply update"));
    expect(t.applyRequests).toEqual([
      { project: sampleProject.id, engine: "codex", review_id: "rec-new", expected_review_id: "rec-old", acknowledge_warnings: false },
    ]);

    await click(button("Claude Code 0"));
    await click(button("Refresh inventory"));
    setInventory([legacy, { ...codex, managed: updatedRow.managed }]);
    await emit(() => pending.resolve({ status: "updated", review_id: "rec-new" }));
    expect(document.querySelector(".toast")?.textContent).toContain("Updated tidy");
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

  it("keeps a pending write locked through A to B to A, shows no success while on B and starts the fresh A confirmation idle", async () => {
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
    // The write finished under an earlier selection: its outcome stays with that confirmation and the fresh one starts idle.
    expect(document.body.textContent).not.toContain("Installed tidy");
    expect(button("Confirm install").disabled).toBe(false);
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
    // It was confirmed under an earlier selection of A: no success toast and no outcome on the fresh confirmation.
    expect(document.querySelector(".toast")).toBeNull();
    expect(panel()?.textContent).not.toContain("Installed tidy");
    expect(button("Confirm install").disabled).toBe(false);
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
    ["an unchanged update for a different review", { status: "unchanged", review_id: "rec-other" } as const],
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

  it("recovers a lost update response by retrying the same confirmation against an already published receipt", async () => {
    const { t, setInventory } = await installedApp();
    t.override.applyProjectSkill = async () => {
      setInventory([updatedRow]);
      throw { code: "OAH_RETRY", message: "Response lost.", hint: null };
    };
    await check(t, pass);
    await click(button("Apply update"));
    expect(alertText()).toContain("OAH_RETRY");
    expect(button("Apply update").disabled).toBe(false);
    t.override.applyProjectSkill = async () => ({ status: "unchanged", review_id: "rec-new" });
    await click(button("Apply update"));
    expect(t.applyRequests.map(({ expected_review_id, review_id }) => [expected_review_id, review_id])).toEqual([
      ["rec-old", "rec-new"],
      ["rec-old", "rec-new"],
    ]);
    expect(alertText()).toBe("");
    expect(document.querySelector(".toast")?.textContent).toContain("already current");
    expect(document.querySelector('.managed-update [role="status"]')?.textContent).toBe("Already current; nothing changed.");
  });
});

describe("R4 update settlement ownership (G32)", () => {
  const renewed: ApplyProjectSkillDto = { status: "updated", review_id: "rec-new" };
  const sameOld: ApplyProjectSkillDto = { status: "unchanged", review_id: "rec-old" };
  const alreadyApplied: ApplyProjectSkillDto = { status: "unchanged", review_id: "rec-new" };
  const withReceipt = (patch: Partial<NonNullable<InstalledSkillDto["managed"]>>) => ({
    ...updatedRow,
    managed: { ...updatedRow.managed, ...patch },
  });
  const observed: ReadonlyArray<readonly [string, readonly (InstalledSkillDto | null)[], ApplyProjectSkillDto, string, string]> = [
    ["the unchanged prior receipt", [tidy], renewed, "Updated.", "Updated tidy."],
    ["the exact candidate before the response", [updatedRow], renewed, "Updated.", "Updated tidy."],
    ["the exact candidate before an idempotent response", [updatedRow], alreadyApplied, "Already current; nothing changed.", "tidy is already current."],
    ["the candidate when the response retained the old receipt", [updatedRow], sameOld, "", ""],
    ["an unrelated replacement", [withReceipt({ review_id: "rec-other" })], renewed, "", ""],
    ["the old receipt removed and restored", [null, tidy], renewed, "", ""],
    ["the candidate after an intervening removal", [null, updatedRow], renewed, "", ""],
    ["the requested review with another snapshot", [withReceipt({ snapshot_id: "snap-other" })], renewed, "", ""],
    ["the requested review with another repository", [withReceipt({ repo: "fork/skills" })], renewed, "", ""],
    ["the requested review with another folder", [withReceipt({ path: "skills/other" })], renewed, "", ""],
  ];

  it.each(observed)("settles an update after observing %s", async (_label, reads, response, announcement, notice) => {
    const { t, setInventory } = await installedApp();
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await check(t, pass);
    await click(button("Apply update"));
    for (const row of reads) {
      setInventory(row ? [row] : []);
      await click(button("Refresh inventory"));
    }
    const region = '[aria-label="Update status for tidy"]';
    expect(button("Check for update").disabled).toBe(true);
    expect(document.querySelector(region)?.textContent).toBe("Applying the update to tidy…");
    await emit(() => pending.resolve(response));
    expect(document.querySelector(region)?.textContent).toBe(announcement);
    expect(document.querySelector(".toast span")?.textContent ?? "").toBe(notice);
    expect(button("Check for update").disabled).toBe(false);
  });

  it("retires a visible outcome when its prior receipt is removed, even if that receipt returns", async () => {
    const { t, setInventory } = await installedApp();
    t.override.applyProjectSkill = async () => sameOld;
    await check(t, pass);
    await click(button("Apply update"));
    const region = '[aria-label="Update status for tidy"]';
    expect(document.querySelector(region)?.textContent).toBe("Already current; nothing changed.");
    expect(document.querySelector(".toast span")?.textContent).toBe("tidy is already current.");
    setInventory([]);
    await click(button("Refresh inventory"));
    expect(document.querySelector(".toast")).toBeNull();
    setInventory([tidy]);
    await click(button("Refresh inventory"));
    expect(document.querySelector(region)?.textContent).toBe("");
    expect(byText("button", "Apply update")).toBeNull();
    expect(document.querySelector(".toast")).toBeNull();
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
    expect(result.textContent).toContain("https://github.com/example/skills");
    expect(result.textContent).toContain("skills/tidy");
    expect(result.textContent).toContain(record.source.commit);
    await unmount();
    const { t: t2 } = await installedApp();
    await check(t2, pass);
    const candidate = document.querySelector('[aria-label="New revision reviewed"]')!;
    expect(facts(candidate)).toEqual(expect.arrayContaining(["GitHub repository", "Folder in repository", "Pinned commit"]));
    expect(candidate.textContent).toContain("https://github.com/example/skills");
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

  it("install: sends nothing for A to B to A with the old confirmation still connected", async () => {
    const t = await exploreApp();
    await review(t, pass);
    await clickAfterReads(t, "Confirm install", [otherProject, sampleProject]);
    expect(t.applyRequests).toHaveLength(0);
  });

  it("install: the same connected click still sends when both reads return A (control)", async () => {
    const t = await exploreApp();
    await review(t, pass);
    await clickAfterReads(t, "Confirm install", [{ ...sampleProject }, { ...sampleProject }]);
    expect(t.applyRequests.map((request) => request.project)).toEqual([sampleProject.id]);
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

  it("update: sends nothing for A to B to A with the old confirmation still connected", async () => {
    const { t } = await installedApp();
    await check(t, pass);
    await clickAfterReads(t, "Apply update", [otherProject, sampleProject]);
    expect(t.applyRequests).toHaveLength(0);
  });

  it("update: the same connected click still sends when both reads return A (control)", async () => {
    const { t } = await installedApp();
    await check(t, pass);
    await clickAfterReads(t, "Apply update", [{ ...sampleProject }, { ...sampleProject }]);
    expect(t.applyRequests.map((request) => [request.project, request.expected_review_id])).toEqual([[sampleProject.id, "rec-old"]]);
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
    ["reviewed", pass, /new revision reviewed/i],
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

describe("C2/C5/C6 guards from review R3 (G32 G35 G37)", () => {
  const prior = {
    name: "tidy",
    engine: "claude",
    scope: "project",
    location: `${sampleProject.root}/.claude/skills/tidy`,
    managed: { review_id: "rec-prior", snapshot_id: "snap-prior", repo: "example/skills", path: "skills/tidy" },
  } satisfies InstalledSkillDto;
  const replacement = { ...prior, managed: { ...prior.managed, review_id: "rec-replacement", snapshot_id: "snap-replacement" } };
  const reviewed: ReviewResultDto = {
    ...pass,
    record: { ...record, id: "rec-reviewed" },
    gate: { ...pass.gate, compatible: ["claude"] },
  };
  const warned: ReviewResultDto = {
    ...reviewed,
    gate: { ...reviewed.gate, result: "warn", reasons: [{ kind: "screening", detail: "Review this manual step.", lines: [4] }] },
  };
  const retryable = { code: "OAH_RETRY", message: "Try again.", hint: null };

  async function r3App(page: "Explore" | "Installed", initial: InstalledSkillDto[] = [prior]) {
    const t = createFakeTransport({ project: sampleProject });
    t.agents = [agent("claude")];
    let inventory = initial;
    t.override.skillsInstalled = async () => inventory;
    await mount(<App transport={t} />);
    if (page === "Installed") await click(button("Installed"));
    return { t, setRows: (next: InstalledSkillDto[]) => (inventory = next) };
  }

  const checkbox = () => {
    const element = document.querySelector<HTMLInputElement>('input[type="checkbox"]');
    if (!element) throw new Error("Warning acknowledgement is missing");
    return element;
  };

  const earlier: InstalledSkillDto = { name: "another", location: `${sampleProject.root}/.claude/skills/another`, scope: "project", engine: "claude" };
  const sameReceipt = { ...prior, managed: { ...prior.managed } };
  const reloads = [
    { label: "reading the same acquisition", initial: [prior], next: [sameReceipt] },
    { label: "a same-project scan refresh", initial: [prior], next: [sameReceipt], scan: true },
    { label: "inserting an earlier row", initial: [prior], next: [earlier, sameReceipt] },
    { label: "removing an earlier row", initial: [earlier, prior], next: [sameReceipt] },
  ];
  it.each(reloads)("keeps the confirmation and its live region mounted after $label (G35)", async ({ initial, next, scan }) => {
    const { t, setRows } = await r3App("Installed", initial);
    await check(t, reviewed);
    const apply = button("Apply update");
    const region = '[aria-label="Update status for tidy"]';
    const status = document.querySelector(region);
    expect(status?.textContent).toBe("New revision reviewed. Review passed.");
    const reads = t.calls.filter((call) => call === "skills_installed").length;
    setRows(next);
    if (scan) await emit(() => t.emitScan({ scope: "all", keys: [] }));
    else await click(button("Refresh inventory"));
    expect(t.calls.filter((call) => call === "skills_installed")).toHaveLength(reads + 1);
    expect(status?.isConnected).toBe(true);
    expect(document.querySelector(region)).toBe(status);
    expect(status?.textContent).toBe("New revision reviewed. Review passed.");
    expect(button("Apply update")).toBe(apply);
    await click(apply);
    expect(t.applyRequests).toEqual([
      { project: sampleProject.id, engine: "claude", review_id: "rec-reviewed", expected_review_id: "rec-prior", acknowledge_warnings: false },
    ]);
  });

  it("cannot rebind a warned retry to a replacement acquisition after inventory refresh", async () => {
    const { t, setRows } = await r3App("Installed");
    t.override.applyProjectSkill = async () => {
      throw retryable;
    };
    await check(t, warned);
    await click(checkbox());
    await click(button("Apply update"));
    expect(document.querySelector(".managed-update [role=alert]")?.textContent).toContain(retryable.code);
    expect(t.applyRequests[0]?.expected_review_id).toBe(prior.managed.review_id);
    setRows([replacement]);
    await click(button("Refresh inventory"));
    const stale = byText("button", "Apply update");
    if (stale instanceof HTMLButtonElement && !stale.disabled) await click(stale);
    expect(t.applyRequests).toEqual([
      { project: sampleProject.id, engine: "claude", review_id: "rec-reviewed", expected_review_id: "rec-prior", acknowledge_warnings: true },
    ]);
  });

  it("cannot apply a check that finishes after inventory replaces the acquisition", async () => {
    const { t, setRows } = await r3App("Installed");
    await click(button("Check for update"));
    expect(t.reviewRequests).toEqual([{ spec: { repo: prior.managed.repo, path: prior.managed.path } }]);
    setRows([replacement]);
    await click(button("Refresh inventory"));
    await emit(() => t.emitReview({ event: "done", data: reviewed }));
    const stale = byText("button", "Apply update");
    if (stale instanceof HTMLButtonElement && !stale.disabled) await click(stale);
    expect(t.applyRequests).toEqual([]);
  });

  it("does not apply acknowledgement from a failed update to a fresh review", async () => {
    const { t } = await r3App("Installed");
    t.override.applyProjectSkill = async () => {
      throw retryable;
    };
    await check(t, warned);
    await click(checkbox());
    await click(button("Apply update"));
    await check(t, { ...warned, record: { ...warned.record, id: "rec-later", source: { ...warned.record.source, commit: "1".repeat(40) } } });
    expect(checkbox().checked).toBe(false);
    expect(button("Apply update").disabled).toBe(true);
    expect(t.applyRequests).toHaveLength(1);
  });

  it("keeps an explicitly revoked install warning revoked across navigation", async () => {
    const { t } = await r3App("Explore");
    t.override.applyProjectSkill = async () => {
      throw retryable;
    };
    await review(t, warned);
    await click(checkbox());
    await click(button("Confirm install"));
    expect(t.applyRequests[0]?.acknowledge_warnings).toBe(true);
    await click(checkbox());
    expect(checkbox().checked).toBe(false);
    expect(button("Confirm install").disabled).toBe(true);
    await click(button("Installed"));
    await click(button("Explore"));
    const retry = button("Confirm install");
    if (!retry.disabled) await click(retry);
    expect(t.applyRequests.map((request) => request.acknowledge_warnings)).toEqual([true]);
    expect(checkbox().checked).toBe(false);
    expect(button("Confirm install").disabled).toBe(true);
  });

  it("announces completion without a simultaneous offer for the same update while inventory is pending", async () => {
    const { t } = await r3App("Installed");
    const refresh = deferred<InstalledSkillDto[]>();
    const status = document.querySelector('.managed-update [role="status"]');
    expect(status?.isConnected).toBe(true);
    await check(t, reviewed);
    expect(status?.textContent).toBe("New revision reviewed. Review passed.");
    t.override.skillsInstalled = () => refresh.promise;
    await click(button("Apply update"));
    expect(status?.isConnected).toBe(true);
    expect(status?.textContent).toBe("Updated.");
    await emit(() => refresh.resolve([{ ...updatedRow, managed: { ...updatedRow.managed, review_id: reviewed.record.id } }]));
    expect(status?.isConnected).toBe(true);
    expect(status?.textContent).toBe("Updated.");
    expect(byText("button", "Apply update")).toBeNull();
  });
});

describe("Confirmation identity invariant: both entry points (G38)", () => {
  const both = ["install", "update"] as const;
  type Entry = (typeof both)[number];
  const entries = {
    install: { label: "Confirm install", open: () => exploreApp(), review },
    update: { label: "Apply update", open: async () => (await installedApp()).t, review: check },
  } satisfies Record<Entry, unknown>;
  const ackBox = () => {
    const element = document.querySelector<HTMLInputElement>('input[type="checkbox"]');
    if (!element) throw new Error("Warning acknowledgement is missing");
    return element;
  };
  /** The entry's warned confirmation, acknowledged. */
  async function opened(entry: Entry) {
    const t = await entries[entry].open();
    await entries[entry].review(t, warn);
    await click(ackBox());
    return t;
  }
  const sent = (entry: Entry) => ({
    project: sampleProject.id,
    engine: "claude",
    review_id: "rec-new",
    expected_review_id: entry === "update" ? "rec-old" : null,
    acknowledge_warnings: true,
  });

  const selections: ReadonlyArray<readonly [string, boolean, readonly ProjectDto[]]> = [
    ["project A is read again", true, [{ ...sampleProject }]],
    ["project B", false, [otherProject]],
    ["project B then A", false, [otherProject, sampleProject]],
  ];
  it.each(both.flatMap((entry) => selections.map(([label, sends, answers]) => [entry, label, sends, answers] as const)))(
    "%s: a connected acknowledged click after %s arrives before commit is sent: %s",
    async (entry, _label, sends, answers) => {
      const t = await opened(entry);
      await clickAfterReads(t, entries[entry].label, answers);
      expect(t.applyRequests).toEqual(sends ? [sent(entry)] : []);
    },
  );

  const receiptPatches: ReadonlyArray<readonly [string, Partial<typeof managed>]> = [
    ["the same receipt", {}],
    ["a replaced review id", { review_id: "rec-moved" }],
    ["a replaced snapshot", { snapshot_id: "snap-2" }],
    ["a replaced source repo", { repo: "fork/skills" }],
    ["a replaced source path", { path: "skills/neat" }],
  ];
  // A plain review has no acknowledgement to lose, so only the receipt check can refuse its stale Apply.
  const flavours = [
    ["acknowledged warned", warn],
    ["plain", pass],
  ] as const;
  it.each(flavours.flatMap(([flavour, result]) => receiptPatches.map(([label, patch]) => [flavour, label, result, patch] as const)))(
    "update: a connected %s Apply after an inventory read with %s arrives before commit",
    async (_flavour, _label, result, patch) => {
      const { t } = await installedApp();
      await check(t, result);
      const warns = result === warn;
      if (warns) await click(ackBox());
      const stale = button("Apply update");
      const read = deferred<InstalledSkillDto[]>();
      t.override.skillsInstalled = () => read.promise;
      await click(button("Refresh inventory"));
      const next = { ...managed, ...patch };
      const inventory = [{ ...rows[0]!, managed: next }, ...rows.slice(1)];
      await act(async () => {
        read.resolve(inventory);
        for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
        expect(stale.isConnected).toBe(true);
        expect(button("Apply update")).toBe(stale);
        expect(stale.disabled).toBe(false);
        stale.click();
      });
      await flush();
      if (Object.keys(patch).length === 0) {
        expect(t.applyRequests).toEqual([{ ...sent("update"), acknowledge_warnings: warns }]);
        return;
      }
      expect(t.applyRequests).toEqual([]);
      // The replaced receipt needs its own check; a warned one starts unacknowledged.
      expect(byText("button", "Apply update")).toBeNull();
      t.override.skillsInstalled = async () => inventory;
      await check(t, withSource(result, { locator: `https://github.com/${next.repo}`, path: next.path }));
      if (warns) {
        expect(ackBox().checked).toBe(false);
        expect(button("Apply update").disabled).toBe(true);
      }
      expect(t.applyRequests).toEqual([]);
    },
  );

  it("update: a receipt that changes and changes back needs a fresh check and a fresh acknowledgement", async () => {
    const t = await opened("update");
    let inventory: InstalledSkillDto[] = [{ ...rows[0]!, managed: { ...managed, review_id: "rec-moved" } }, ...rows.slice(1)];
    t.override.skillsInstalled = async () => inventory;
    await click(button("Refresh inventory"));
    inventory = rows;
    await click(button("Refresh inventory"));
    expect(byText("button", "Apply update")).toBeNull();
    await check(t, warn);
    expect(ackBox().checked).toBe(false);
    expect(button("Apply update").disabled).toBe(true);
    await click(ackBox());
    await click(button("Apply update"));
    expect(t.applyRequests).toEqual([sent("update")]);
  });

  it.each(both)("%s: a committed A to B to A return starts a fresh acknowledgement", async (entry) => {
    const t = await opened(entry);
    for (const next of [otherProject, sampleProject]) {
      t.override.projectOpenDialog = async () => next;
      await click(button("+ Open…"));
    }
    await entries[entry].review(t, warn);
    expect(ackBox().checked).toBe(false);
    expect(button(entries[entry].label).disabled).toBe(true);
    await click(ackBox());
    await click(button(entries[entry].label));
    expect(t.applyRequests).toEqual([sent(entry)]);
  });

  it("install: each engine has its own acknowledgement", async () => {
    const t = await opened("install");
    const radios = () => Array.from(panel()?.querySelectorAll<HTMLInputElement>('input[type="radio"]') ?? []);
    await click(radios()[1]!);
    expect(ackBox().checked).toBe(false);
    expect(button("Confirm install").disabled).toBe(true);
    await click(radios()[0]!);
    expect(ackBox().checked).toBe(true);
    await click(button("Confirm install"));
    expect(t.applyRequests).toEqual([sent("install")]);
  });

  const secondRows: ReadonlyArray<readonly [string, InstalledSkillDto, ReviewResultDto]> = [
    ["engine", { ...rows[0]!, engine: "codex", location: `${sampleProject.root}/.agents/skills/tidy` }, warn],
    [
      "skill name",
      { ...rows[0]!, name: "neat", location: `${sampleProject.root}/.claude/skills/neat`, managed: { ...managed, path: "skills/neat" } },
      named(warn, "neat", "skills/neat"),
    ],
  ];
  it.each(secondRows)("update: a row with another %s has its own acknowledgement", async (_label, second, result) => {
    const { t, setInventory } = await installedApp();
    setInventory([rows[0]!, second]);
    await click(button("Refresh inventory"));
    const control = <T extends HTMLElement>(row: number, selector: string, text?: string) => {
      const group = document.querySelectorAll<HTMLElement>(".managed-update")[row];
      const found = Array.from(group?.querySelectorAll<T>(selector) ?? []).find((element) => text === undefined || element.textContent === text);
      if (!found) throw new Error(`Missing ${selector} ${text ?? ""} in row ${row}`);
      return found;
    };
    await click(control(0, "button", "Check for update"));
    await emit(() => t.emitReview({ event: "done", data: warn }));
    await click(control<HTMLInputElement>(0, 'input[type="checkbox"]'));
    await click(control(1, "button", "Check for update"));
    await emit(() => t.emitReview({ event: "done", data: result }));
    expect(control<HTMLInputElement>(0, 'input[type="checkbox"]').checked).toBe(true);
    expect(control<HTMLInputElement>(1, 'input[type="checkbox"]').checked).toBe(false);
    expect(control<HTMLButtonElement>(1, "button", "Apply update").disabled).toBe(true);
    await click(control(0, "button", "Apply update"));
    expect(t.applyRequests).toEqual([sent("update")]);
  });

  it.each(both)("%s: a different candidate starts unacknowledged", async (entry) => {
    const t = await opened(entry);
    await entries[entry].review(t, { ...warn, record: { ...warn.record, id: "rec-newer", source: { ...warn.record.source, commit: "1".repeat(40) } } });
    expect(ackBox().checked).toBe(false);
    expect(button(entries[entry].label).disabled).toBe(true);
    expect(t.applyRequests).toEqual([]);
  });

  const foreign: ReadonlyArray<readonly [string, ReviewResultDto]> = [
    ["skill name", named(pass, "renamed", record.source.path)],
    ["GitHub repo", withSource(pass, { locator: "https://github.com/fork/skills" })],
    ["GitHub provider", withSource(pass, { provider: "gitlab" })],
    ["GitHub locator form", withSource(pass, { locator: "example/skills" })],
    ["GitHub repository case", withSource(pass, { locator: "https://github.com/Example/skills" })],
    ["folder", withSource(pass, { path: "skills/Tidy" })],
  ];
  it.each(foreign)("update: offers no write when the review's %s differs from the captured receipt", async (_label, result) => {
    const { t } = await installedApp();
    await check(t, result);
    expect(byText("button", "Apply update")).toBeNull();
    expect(document.querySelector('[aria-label="Update not applicable"]')).not.toBeNull();
    expect(document.querySelector('.managed-update [role="status"]')?.textContent).toBe(
      "Update not applicable. The review does not match the installed copy. The installed copy is unchanged.",
    );
    expect(t.applyRequests).toEqual([]);
  });

  it("update: a write that finishes after an A to B to A return re-reads A but neither toasts nor attaches its outcome", async () => {
    const { t, setInventory } = await installedApp();
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await check(t, pass);
    await click(button("Apply update"));
    for (const next of [otherProject, sampleProject]) {
      t.override.projectOpenDialog = async () => next;
      await click(button("+ Open…"));
    }
    const status = () => document.querySelector('.managed-update [role="status"]')?.textContent ?? null;
    // The earlier write still locks the folder in the fresh view of A.
    expect(button("Check for update").disabled).toBe(true);
    expect(status()).toBe("Applying the update to tidy…");
    setInventory([updatedRow, ...rows.slice(1)]);
    const reads = t.calls.filter((call) => call === "skills_installed").length;
    await emit(() => pending.resolve({ status: "updated", review_id: "rec-new" }));
    expect(t.calls.filter((call) => call === "skills_installed").length).toBeGreaterThan(reads);
    expect(document.querySelector(".toast")).toBeNull();
    expect(status()).toBe("");
    expect(byText("button", "Apply update")).toBeNull();
    expect(button("Check for update").disabled).toBe(false);
  });
});

describe("C5 guard: one current announcement (G35)", () => {
  const lines = (selector: string) => Array.from(document.querySelector(selector)?.querySelectorAll("p") ?? [], (line) => line.textContent);

  it("update: exactly one line at each step from the check to the re-read inventory", async () => {
    const { t, setInventory } = await installedApp();
    const region = '.managed-update [role="status"]';
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    expect(lines(region)).toEqual([]);
    await click(button("Check for update"));
    expect(lines(region)).toEqual(["Checking tidy for updates…"]);
    await emit(() => t.emitReview({ event: "done", data: pass }));
    expect(lines(region)).toEqual(["New revision reviewed. Review passed."]);
    await click(button("Apply update"));
    expect(lines(region)).toEqual(["Applying the update to tidy…"]);
    setInventory([updatedRow, ...rows.slice(1)]);
    await emit(() => pending.resolve({ status: "updated", review_id: "rec-new" }));
    expect(lines(region)).toEqual(["Updated."]);
  });

  it("install: exactly one line while installing and after", async () => {
    const t = await exploreApp();
    const region = '[aria-label="Install in project"] [role="status"]';
    const pending = deferred<ApplyProjectSkillDto>();
    t.override.applyProjectSkill = () => pending.promise;
    await review(t, pass);
    expect(lines(region)).toEqual([]);
    await click(button("Confirm install"));
    expect(lines(region)).toEqual(["Installing tidy…"]);
    await emit(() => pending.resolve({ status: "installed", review_id: "rec-new" }));
    expect(lines(region)).toEqual(["Installed tidy. It now appears in Installed."]);
  });
});
