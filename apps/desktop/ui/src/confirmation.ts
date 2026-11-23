// 한국어: 스킬 쓰기 확인의 단일 정의 — 선택 프로젝트·세대·엔진·스킬·설치 영수증·후보 검토를 하나의 불변 키로 묶고, 보낼 요청과 응답 검증을 그 확인에서만 만든다.
import type {
  ApplyProjectSkillDto,
  ApplyProjectSkillReq,
  ErrorDto,
  InstalledSkillDto,
  ManagedSkillDto,
  ProjectSkillEngine,
  ReviewResultDto,
} from "./contract";

export type ApplyOperation = "install" | "update";

const GITHUB_LOCATOR_PREFIX = "https://github.com/";

/** The project selection a confirmation is built under; a new project ID (A to B to A included) is a new generation. */
export interface Selection {
  readonly project: string;
  readonly generation: number;
}

/** The managed receipt an update replaces, exactly as the inventory reported it. */
export interface Acquisition {
  readonly review_id: string;
  readonly snapshot_id: string;
  readonly repo: string;
  readonly path: string;
}

/** What the inventory last showed at one project folder; `revision` counts every change, so a replaced receipt never becomes current again. */
export interface Acquired {
  readonly acquisition: Acquisition | null;
  readonly revision: number;
}

/** The immutable reviewed candidate; its review ID is a digest of the whole record, pinned snapshot included. */
export interface Candidate {
  readonly review_id: string;
  readonly snapshot: string | null;
  readonly repo: string;
  readonly path: string;
  readonly commit: string | null;
  readonly tree: string | null;
}

/** Everything one write needs, built once; nothing is re-read from live selections, rows or results afterwards. */
export interface Confirmation {
  /** Identity of everything below but the folder text; the acknowledgement belongs to this key. */
  readonly key: string;
  /** The project folder slot (`writeKey`): one write at a time and one outcome per slot. */
  readonly destination: string;
  readonly project: string;
  readonly generation: number;
  readonly engine: ProjectSkillEngine;
  readonly name: string;
  readonly operation: ApplyOperation;
  /** On update, the receipt it replaces and the inventory revision that showed it; `null` on install. */
  readonly acquired: { readonly acquisition: Acquisition; readonly revision: number } | null;
  readonly candidate: Candidate;
  /** The candidate's warnings must be acknowledged before the request can be sent. */
  readonly warnings: boolean;
  /** The folder shown to the user. */
  readonly folder: string;
}

export function writeKey(project: string, engine: ProjectSkillEngine, name: string): string {
  return JSON.stringify([project, engine, name]);
}

/** Warnings the user must acknowledge before an install or update can be confirmed. */
export function needsAcknowledgement(result: ReviewResultDto): boolean {
  return result.gate.result === "warn" || result.record.license.eligibility === "eligible-by-choice";
}

/** A result the gate did not block and the record accepted; only these can be written. */
export function isInstallable(result: ReviewResultDto): boolean {
  return result.gate.result !== "block" && result.record.status === "reviewed";
}

function compatible(result: ReviewResultDto, engine: ProjectSkillEngine): boolean {
  return result.gate.compatible === undefined || result.gate.compatible.includes(engine);
}

export function acquisitionOf(managed: ManagedSkillDto | undefined): Acquisition | null {
  return managed ? { review_id: managed.review_id, snapshot_id: managed.snapshot_id, repo: managed.repo, path: managed.path } : null;
}

export function sameAcquisition(a: Acquisition | null, b: Acquisition | null): boolean {
  if (a === null || b === null) return a === b;
  return a.review_id === b.review_id && a.snapshot_id === b.snapshot_id && a.repo === b.repo && a.path === b.path;
}

function candidateOf(result: ReviewResultDto): Candidate | null {
  const { id, source } = result.record;
  if (source.provider !== "github" || !source.locator.startsWith(GITHUB_LOCATOR_PREFIX)) return null;
  // The wire record carries its snapshot ID although the page's DTO type does not list it.
  const snapshot = "snapshot" in source && typeof source.snapshot === "string" ? source.snapshot : null;
  return {
    review_id: id,
    snapshot,
    repo: source.locator.slice(GITHUB_LOCATOR_PREFIX.length),
    path: source.path,
    commit: source.commit,
    tree: source.tree,
  };
}

function acquiredKey(acquired: Confirmation["acquired"]) {
  return acquired && [acquired.acquisition.review_id, acquired.acquisition.snapshot_id, acquired.acquisition.repo, acquired.acquisition.path, acquired.revision];
}

function confirmation(parts: Omit<Confirmation, "key" | "destination">): Confirmation {
  const { project, generation, engine, name, operation, acquired, candidate, warnings } = parts;
  const { review_id, snapshot, repo, path, commit, tree } = candidate;
  return {
    ...parts,
    destination: writeKey(project, engine, name),
    key: JSON.stringify([project, generation, engine, name, operation, acquiredKey(acquired), [review_id, snapshot, repo, path, commit, tree], warnings]),
  };
}

/** An install of the reviewed candidate for `engine` in the selected project; `null` when this review cannot be written there. */
export function confirmInstall(selection: Selection, engine: ProjectSkillEngine, folder: string, result: ReviewResultDto): Confirmation | null {
  const name = result.record.frontmatter.name;
  const candidate = candidateOf(result);
  if (!name || candidate === null || !isInstallable(result) || !compatible(result, engine)) return null;
  return confirmation({
    project: selection.project,
    generation: selection.generation,
    engine,
    name,
    operation: "install",
    acquired: null,
    candidate,
    warnings: needsAcknowledgement(result),
    folder,
  });
}

/** Where an update check starts: the selected project and the exact receipt the newest inventory read showed at that folder. */
export interface UpdateBasis {
  readonly key: string;
  readonly destination: string;
  readonly selection: Selection;
  readonly engine: ProjectSkillEngine;
  readonly name: string;
  readonly folder: string;
  readonly acquired: { readonly acquisition: Acquisition; readonly revision: number };
}

/** `null` unless the row is a managed project copy whose receipt is the one last observed at its folder. */
export function updateBasis(selection: Selection, row: InstalledSkillDto, observed: Acquired | undefined): UpdateBasis | null {
  const acquisition = acquisitionOf(row.managed);
  if (row.scope !== "project" || acquisition === null || observed === undefined || !sameAcquisition(observed.acquisition, acquisition)) {
    return null;
  }
  const acquired = { acquisition, revision: observed.revision };
  return {
    key: JSON.stringify([selection.project, selection.generation, row.engine, row.name, acquiredKey(acquired)]),
    destination: writeKey(selection.project, row.engine, row.name),
    selection,
    engine: row.engine,
    name: row.name,
    folder: row.location,
    acquired,
  };
}

export type UpdateVerdict =
  | { readonly kind: "current" }
  | { readonly kind: "mismatch" }
  | { readonly kind: "blocked" }
  | { readonly kind: "incompatible" }
  | { readonly kind: "reviewed"; readonly confirmation: Confirmation };

/** What a finished check means for the receipt it started from; only an eligible new review of the same skill and source becomes a confirmation. */
export function judgeUpdate(basis: UpdateBasis, result: ReviewResultDto): UpdateVerdict {
  const { acquisition } = basis.acquired;
  // A review of another skill name, repository or folder cannot replace this receipt; the writer refuses it as well.
  const candidate = candidateOf(result);
  if (
    candidate === null ||
    result.record.frontmatter.name !== basis.name ||
    candidate.repo !== acquisition.repo ||
    candidate.path !== acquisition.path
  ) {
    return { kind: "mismatch" };
  }
  if (candidate.review_id === acquisition.review_id) return { kind: "current" };
  if (!isInstallable(result)) return { kind: "blocked" };
  if (!compatible(result, basis.engine)) return { kind: "incompatible" };
  return {
    kind: "reviewed",
    confirmation: confirmation({
      project: basis.selection.project,
      generation: basis.selection.generation,
      engine: basis.engine,
      name: basis.name,
      operation: "update",
      acquired: basis.acquired,
      candidate,
      warnings: needsAcknowledgement(result),
      folder: basis.folder,
    }),
  };
}

/** The native request, built from the confirmation alone. */
export function requestFor(confirmation: Confirmation, acknowledged: boolean): ApplyProjectSkillReq {
  return {
    project: confirmation.project,
    engine: confirmation.engine,
    review_id: confirmation.candidate.review_id,
    expected_review_id: confirmation.acquired?.acquisition.review_id ?? null,
    acknowledge_warnings: acknowledged,
  };
}

function mismatch(message: string): ErrorDto {
  return { code: "UI_APPLY_RESPONSE_MISMATCH", message, hint: "Refresh the inventory to see what is installed before trying again." };
}

/** Success is claimed only for a status the operation can produce, reporting the exact receipt that status allows. */
export function checkResponse(confirmation: Confirmation, result: ApplyProjectSkillDto): ErrorDto | null {
  const status = (result as { status?: unknown } | null)?.status;
  const reviewId = (result as { review_id?: unknown } | null)?.review_id;
  const requested = confirmation.candidate.review_id;
  switch (status) {
    case "installed":
      if (confirmation.operation !== "install") return mismatch("The core reported an install for an update request.");
      return reviewId === requested ? null : mismatch("The core reported a different review than the one confirmed.");
    case "updated":
      if (confirmation.operation !== "update") return mismatch("The core reported an update for an install request.");
      return reviewId === requested ? null : mismatch("The core reported a different review than the one confirmed.");
    case "unchanged":
      // An unchanged install keeps the requested review. An unchanged update keeps the replaced receipt when the new
      // review has identical files, or reports the requested review when a retry finds it already published.
      if (reviewId === requested) return null;
      if (confirmation.operation === "update" && reviewId === confirmation.acquired?.acquisition.review_id) return null;
      return mismatch("The core reported an unexpected review for an unchanged skill.");
    default:
      return mismatch("The core returned an unknown result.");
  }
}

export function successText(confirmation: Confirmation, result: ApplyProjectSkillDto): string {
  if (confirmation.operation === "install") {
    return result.status === "unchanged" ? "Skill already installed; nothing changed." : "Skill installed in the project.";
  }
  return result.status === "unchanged" ? `${confirmation.name} is already current.` : `Updated ${confirmation.name}.`;
}
