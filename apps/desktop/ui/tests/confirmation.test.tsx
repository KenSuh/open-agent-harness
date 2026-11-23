// 한국어: 확인 정체성 불변식 표 — 프로젝트·세대·엔진·이름·설치 영수증·후보 검토 중 하나만 바뀌어도 확인과 경고 확인이 달라지고, 소유자는 더는 성립하지 않는 확인을 보내지 않으며, 응답은 허용된 영수증만 받는지 본다.
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";
import {
  checkResponse,
  confirmInstall,
  judgeUpdate,
  requestFor,
  updateBasis,
  writeKey,
  type Acquired,
  type Confirmation,
  type Selection,
} from "../src/confirmation";
import type { ApplyProjectSkillDto, InstalledSkillDto, ProjectDto, ReviewResultDto } from "../src/contract";
import { useSkillConfirmations, type SkillConfirmations } from "../src/useApply";
import { mount, unmount } from "./support/dom";
import { createFakeTransport, sampleProject, type FakeTransport } from "./support/fakeTransport";

afterEach(unmount);

type Source = ReviewResultDto["record"]["source"];
const selection: Selection = { project: sampleProject.id, generation: 1 };
const receipt = { review_id: "rec-old", snapshot_id: "snap-old", repo: "example/skills", path: "skills/tidy" };
const row: InstalledSkillDto = {
  name: "tidy",
  engine: "claude",
  scope: "project",
  location: `${sampleProject.root}/.claude/skills/tidy`,
  managed: receipt,
};
const observed: Acquired = { acquisition: { ...receipt }, revision: 0 };
const candidate: ReviewResultDto = {
  record: {
    version: 1,
    id: "rec-new",
    // The wire record also carries the pinned snapshot.
    source: {
      provider: "github",
      locator: "https://github.com/example/skills",
      commit: "0123456789abcdef0123456789abcdef01234567",
      tree: null,
      path: "skills/tidy",
      snapshot: "snap-new",
    } as Source,
    frontmatter: { name: "tidy", description: "Tidies things." },
    license: { verdict: "recognized", expression: "MIT", eligibility: "eligible" },
    status: "reviewed",
    reason: null,
    checked_at: "2026-11-22T00:00:00Z",
  },
  gate: { gate: "oah-install-gate/v1", result: "pass", reasons: [], compatible: ["claude", "codex"] },
  unchanged: false,
};
const warned: ReviewResultDto = {
  ...candidate,
  gate: { ...candidate.gate, result: "warn", reasons: [{ kind: "screening", detail: "Runs a shell snippet.", lines: [4] }] },
};
const folder = `${sampleProject.root}/.claude/skills/tidy`;
const destination = writeKey(sampleProject.id, "claude", "tidy");

const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const withSource = (result: ReviewResultDto, patch: Record<string, unknown>): ReviewResultDto => ({
  ...result,
  record: { ...result.record, source: { ...result.record.source, ...patch } as Source },
});
const withId = (result: ReviewResultDto, id: string): ReviewResultDto => ({ ...result, record: { ...result.record, id } });
const named = (result: ReviewResultDto, name: string): ReviewResultDto => ({
  ...result,
  record: { ...result.record, frontmatter: { ...result.record.frontmatter, name } },
});

function update(at: Selection, skill: InstalledSkillDto, seen: Acquired, result: ReviewResultDto): Confirmation {
  const basis = updateBasis(at, skill, seen);
  if (!basis) throw new Error("No update basis");
  const verdict = judgeUpdate(basis, result);
  if (verdict.kind !== "reviewed") throw new Error(`Not a reviewed update: ${verdict.kind}`);
  return verdict.confirmation;
}

function install(at: Selection, engine: "claude" | "codex", result: ReviewResultDto): Confirmation {
  const confirmation = confirmInstall(at, engine, folder, result);
  if (!confirmation) throw new Error("No install confirmation");
  return confirmation;
}

/** A receipt with one field replaced, as both the row and the owner's observation show it. */
function replaced(patch: Partial<typeof receipt>) {
  const managed = { ...receipt, ...patch };
  return { skill: { ...row, managed }, seen: { acquisition: { ...managed }, revision: 0 } };
}

/** An update of a replaced receipt; the review is the same one pinned to that receipt's source, which an update must share. */
function receiptVariant(patch: Partial<typeof receipt>): Confirmation {
  const { skill, seen } = replaced(patch);
  return update(selection, skill, seen, withSource(candidate, { locator: `https://github.com/${seen.acquisition.repo}`, path: seen.acquisition.path }));
}

const receiptChanges = [
  ["review id", { review_id: "rec-moved" }],
  ["snapshot", { snapshot_id: "snap-moved" }],
  ["source repo", { repo: "fork/skills" }],
  ["source path", { path: "skills/neat" }],
] as const;

const candidateChanges = [
  ["review id", (result: ReviewResultDto) => withId(result, "rec-newer")],
  ["snapshot", (result: ReviewResultDto) => withSource(result, { snapshot: "snap-newer" })],
  ["commit", (result: ReviewResultDto) => withSource(result, { commit: "1".repeat(40) })],
  ["tree", (result: ReviewResultDto) => withSource(result, { tree: "2".repeat(40) })],
  ["warnings", (result: ReviewResultDto) => ({ ...result, gate: warned.gate })],
] as const;

/** Facts an install may take from any review but an update must share with the receipt it replaces. */
const sourceChanges = [
  ["skill name", (result: ReviewResultDto) => named(result, "neat")],
  ["repo", (result: ReviewResultDto) => withSource(result, { locator: "https://github.com/fork/skills" })],
  ["path", (result: ReviewResultDto) => withSource(result, { path: "skills/tidy/v2" })],
] as const;

/** Every single-fact change of an update confirmation; each one must give another confirmation. */
const updateVariants: ReadonlyArray<readonly [string, () => Confirmation]> = [
  ["project", () => update({ ...selection, project: "other-project" }, row, observed, candidate)],
  ["project generation (A to B to A)", () => update({ ...selection, generation: 3 }, row, observed, candidate)],
  ["engine", () => update(selection, { ...row, engine: "codex" }, observed, candidate)],
  ["skill name (row and review)", () => update(selection, { ...row, name: "neat" }, observed, named(candidate, "neat"))],
  ...receiptChanges.map(([label, patch]) => [`receipt ${label}`, () => receiptVariant(patch)] as const),
  ["receipt revision (the same receipt seen again after a change)", () => update(selection, row, { ...observed, revision: 2 }, candidate)],
  ...candidateChanges.map(([label, change]) => [`candidate ${label}`, () => update(selection, row, observed, change(candidate))] as const),
];

/** Every single-fact change of an install confirmation. */
const installVariants: ReadonlyArray<readonly [string, () => Confirmation]> = [
  ["project", () => install({ ...selection, project: "other-project" }, "claude", candidate)],
  ["project generation (A to B to A)", () => install({ ...selection, generation: 3 }, "claude", candidate)],
  ["engine", () => install(selection, "codex", candidate)],
  ...[...sourceChanges, ...candidateChanges].map(([label, change]) => [`candidate ${label}`, () => install(selection, "claude", change(candidate))] as const),
];

describe("Confirmation identity (G38)", () => {
  it("builds the same update and install confirmation from the same facts, including a re-read copy of the receipt", () => {
    const first = update(selection, row, observed, candidate);
    const again = update({ ...selection }, { ...row, managed: { ...receipt } }, copy(observed), copy(candidate));
    expect(again.key).toBe(first.key);
    expect(requestFor(again, false)).toEqual(requestFor(first, false));
    expect(install({ ...selection }, "claude", copy(candidate)).key).toBe(install(selection, "claude", candidate).key);
  });

  it.each(updateVariants)("gives an update a new identity when its %s changes", (_label, variant) => {
    expect(variant().key).not.toBe(update(selection, row, observed, candidate).key);
  });

  it.each(installVariants)("gives an install a new identity when its %s changes", (_label, variant) => {
    expect(variant().key).not.toBe(install(selection, "claude", candidate).key);
  });

  it.each(receiptChanges)("never pairs a row whose receipt %s differs from the observed receipt with an update", (_label, patch) => {
    expect(updateBasis(selection, replaced(patch).skill, observed)).toBeNull();
  });

  it.each(sourceChanges)("refuses to build an update from a review whose %s differs from the captured receipt", (_label, change) => {
    const basis = updateBasis(selection, row, observed);
    expect(basis && judgeUpdate(basis, change(candidate)).kind).toBe("mismatch");
  });

  it("builds the native request from the confirmation alone", () => {
    expect(requestFor(update(selection, row, observed, warned), true)).toEqual({
      project: sampleProject.id,
      engine: "claude",
      review_id: "rec-new",
      expected_review_id: "rec-old",
      acknowledge_warnings: true,
    });
    expect(requestFor(install(selection, "codex", candidate), false)).toEqual({
      project: sampleProject.id,
      engine: "codex",
      review_id: "rec-new",
      expected_review_id: null,
      acknowledge_warnings: false,
    });
  });
});

describe("Readback of the write response (G32)", () => {
  const responses: ReadonlyArray<readonly [string, "install" | "update", boolean, unknown]> = [
    ["installed with the requested review", "install", true, { status: "installed", review_id: "rec-new" }],
    ["installed with another review", "install", false, { status: "installed", review_id: "rec-other" }],
    ["updated for an install", "install", false, { status: "updated", review_id: "rec-new" }],
    ["unchanged with the requested review", "install", true, { status: "unchanged", review_id: "rec-new" }],
    ["unchanged with another review", "install", false, { status: "unchanged", review_id: "rec-other" }],
    ["an unknown status", "install", false, { status: "moved", review_id: "rec-new" }],
    ["no body", "install", false, null],
    ["updated with the requested review", "update", true, { status: "updated", review_id: "rec-new" }],
    ["updated with the replaced review", "update", false, { status: "updated", review_id: "rec-old" }],
    ["updated with another review", "update", false, { status: "updated", review_id: "rec-other" }],
    ["installed for an update", "update", false, { status: "installed", review_id: "rec-new" }],
    ["unchanged with the expected old review (identical files)", "update", true, { status: "unchanged", review_id: "rec-old" }],
    ["unchanged with the requested review (retry after a lost response)", "update", true, { status: "unchanged", review_id: "rec-new" }],
    ["unchanged with another review", "update", false, { status: "unchanged", review_id: "rec-other" }],
    ["an unknown status", "update", false, { status: "moved", review_id: "rec-new" }],
  ];

  it.each(responses)("%s for an %s is accepted: %s", (_label, operation, accepted, response) => {
    const confirmation = operation === "install" ? install(selection, "claude", candidate) : update(selection, row, observed, candidate);
    const problem = checkResponse(confirmation, response as ApplyProjectSkillDto);
    expect(problem === null).toBe(accepted);
    if (!accepted) expect(problem?.code).toBe("UI_APPLY_RESPONSE_MISMATCH");
  });
});

function Owner(props: {
  transport: FakeTransport;
  projectRef: { current: ProjectDto | null };
  selectionRef: { current: number };
  out: { current: SkillConfirmations | null };
}) {
  props.out.current = useSkillConfirmations(props.transport, props.projectRef, props.selectionRef, sampleProject.id, 1, () => {}).confirmations;
  return null;
}

async function owner() {
  const transport = createFakeTransport();
  const projectRef: { current: ProjectDto | null } = { current: sampleProject };
  const selectionRef = { current: 1 };
  const out: { current: SkillConfirmations | null } = { current: null };
  await mount(<Owner transport={transport} projectRef={projectRef} selectionRef={selectionRef} out={out} />);
  const api = () => {
    if (!out.current) throw new Error("Owner is not mounted");
    return out.current;
  };
  const observe = (rows: InstalledSkillDto[]) => act(async () => api().observe(sampleProject.id, 1, rows));
  const seen = () => {
    const folder = api().acquired[destination];
    if (!folder) throw new Error("Folder was not observed");
    return folder;
  };
  return { transport, projectRef, selectionRef, api, observe, seen };
}

type OwnerHarness = Awaited<ReturnType<typeof owner>>;
const moved = replaced({ review_id: "rec-moved" }).skill;
const sentUpdate = { project: sampleProject.id, engine: "claude", review_id: "rec-new", expected_review_id: "rec-old", acknowledge_warnings: true };
const otherProject: ProjectDto = { ...sampleProject, id: "other-project", root: "/work/other" };

describe("The confirmation owner sends only a confirmation whose identity still holds (G38)", () => {
  const updateCases: ReadonlyArray<readonly [string, boolean, (o: OwnerHarness) => unknown]> = [
    ["nothing changed", true, () => undefined],
    ["the same receipt was read again", true, (o) => o.observe([{ ...row, managed: { ...receipt } }])],
    ...receiptChanges.map(([label, patch]) => [`the receipt ${label} was replaced`, false, (o: OwnerHarness) => o.observe([replaced(patch).skill])] as const),
    ["the receipt changed and changed back", false, async (o) => {
      await o.observe([moved]);
      await o.observe([row]);
    }],
    ["the managed copy disappeared", false, (o) => o.observe([])],
    ["another project was selected", false, (o) => {
      o.projectRef.current = otherProject;
      o.selectionRef.current = 2;
    }],
    ["the project went A to B to A", false, (o) => {
      o.selectionRef.current = 3;
    }],
  ];

  // A plain review has no acknowledgement to lose, so only the receipt and selection checks can refuse it.
  const flavours = [
    ["acknowledged warned", warned, true],
    ["plain", candidate, false],
  ] as const;
  it.each(flavours.flatMap(([flavour, result, warns]) => updateCases.map(([label, sent, change]) => [flavour, label, sent, result, warns, change] as const)))(
    "an %s update after %s is sent: %s",
    async (_flavour, _label, sent, result, warns, change) => {
      const o = await owner();
      await o.observe([row]);
      const confirmation = update(selection, row, o.seen(), result);
      if (warns) await act(async () => o.api().acknowledge(confirmation, true));
      await change(o);
      await act(async () => o.api().apply(confirmation));
      expect(o.transport.applyRequests).toEqual(sent ? [{ ...sentUpdate, acknowledge_warnings: warns }] : []);
    },
  );

  const installCases: ReadonlyArray<readonly [string, boolean, (o: OwnerHarness) => unknown]> = [
    ["nothing changed", true, () => undefined],
    ["another project was selected", false, (o) => {
      o.projectRef.current = otherProject;
      o.selectionRef.current = 2;
    }],
    ["the project went A to B to A", false, (o) => {
      o.selectionRef.current = 3;
    }],
    ["the acknowledgement was revoked", false, (o) => act(async () => o.api().acknowledge(install(selection, "claude", warned), false))],
  ];

  it.each(installCases)("an acknowledged install after %s is sent: %s", async (_label, sent, change) => {
    const o = await owner();
    const confirmation = install(selection, "claude", warned);
    await act(async () => o.api().acknowledge(confirmation, true));
    await change(o);
    await act(async () => o.api().apply(confirmation));
    expect(o.transport.applyRequests).toEqual(
      sent ? [{ project: sampleProject.id, engine: "claude", review_id: "rec-new", expected_review_id: null, acknowledge_warnings: true }] : [],
    );
  });

  it.each([...updateVariants.map(([label, variant]) => ["update", label, variant] as const), ...installVariants.map(([label, variant]) => ["install", label, variant] as const)])(
    "an acknowledged %s does not acknowledge the confirmation with another %s",
    async (operation, _label, variant) => {
      const o = await owner();
      const base = operation === "update" ? update(selection, row, observed, candidate) : install(selection, "claude", candidate);
      await act(async () => o.api().acknowledge(base, true));
      expect(o.api().acknowledged(base)).toBe(true);
      expect(o.api().acknowledged(variant())).toBe(false);
    },
  );

  it("never carries an acknowledgement to a replaced receipt, even when the same receipt comes back", async () => {
    const o = await owner();
    await o.observe([row]);
    const first = update(selection, row, o.seen(), warned);
    await act(async () => o.api().acknowledge(first, true));
    expect(o.api().acknowledged(update(selection, { ...row, managed: { ...receipt } }, o.seen(), copy(warned)))).toBe(true);
    await o.observe([moved]);
    await o.observe([row]);
    const again = update(selection, row, o.seen(), warned);
    expect(again.key).not.toBe(first.key);
    expect(o.api().acknowledged(again)).toBe(false);
    await act(async () => o.api().apply(again));
    await act(async () => o.api().apply(first));
    expect(o.transport.applyRequests).toEqual([]);
  });

  it("freezes the sent acknowledgement on the operation and does not restore it after a failure is revoked", async () => {
    const o = await owner();
    o.transport.override.applyProjectSkill = async () => {
      throw { code: "OAH_RETRY", message: "Try again.", hint: null };
    };
    await o.observe([row]);
    const confirmation = update(selection, row, o.seen(), warned);
    await act(async () => o.api().acknowledge(confirmation, true));
    await act(async () => o.api().apply(confirmation));
    expect(o.api().operations[destination]).toMatchObject({ phase: "failed", acknowledged: true });
    await act(async () => o.api().acknowledge(confirmation, false));
    expect(o.api().acknowledged(confirmation)).toBe(false);
    await act(async () => o.api().apply(confirmation));
    expect(o.transport.applyRequests).toEqual([sentUpdate]);
  });
});
