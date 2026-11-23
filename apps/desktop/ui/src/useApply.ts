// 한국어: apply_project_skill 쓰기 잠금·결과 저장소 — 컨트롤러가 소유해 화면 전환·프로젝트 전환 중에도 유지되고, 확인된 응답만 성공으로 다룬다.
import { useCallback, useRef, useState, type RefObject } from "react";
import type { ApplyProjectSkillDto, ApplyProjectSkillReq, ErrorDto, ProjectDto, ProjectSkillEngine } from "./contract";
import { toErrorDto, type Transport } from "./transport/types";

export type ApplyOperation = "install" | "update";

/** What the user confirmed; copied once at click time and never rebuilt from live selections afterwards. */
export interface ApplyConfirmation {
  readonly project: string;
  readonly engine: ProjectSkillEngine;
  readonly name: string;
  readonly operation: ApplyOperation;
  readonly review_id: string;
  /** `null` on install; the previously installed review ID on update. */
  readonly expected_review_id: string | null;
  readonly acknowledge_warnings: boolean;
  readonly destination: string;
}

export type WriteEntry = { readonly confirmation: ApplyConfirmation } & (
  | { readonly phase: "applying" }
  | { readonly phase: "done"; readonly result: ApplyProjectSkillDto }
  | { readonly phase: "failed"; readonly error: ErrorDto }
);

export interface SkillWrites {
  /** One entry per project and destination, keyed by `writeKey`. */
  readonly entries: Readonly<Record<string, WriteEntry>>;
  apply(confirmation: Omit<ApplyConfirmation, "project">): Promise<void>;
  /** Drops a finished outcome; an entry that is still applying is kept. */
  clear(key: string): void;
}

export const NO_WRITES: SkillWrites = { entries: {}, apply: async () => undefined, clear: () => undefined };

export function writeKey(project: string, engine: ProjectSkillEngine, name: string): string {
  return JSON.stringify([project, engine, name]);
}

function mismatch(message: string): ErrorDto {
  return { code: "UI_APPLY_RESPONSE_MISMATCH", message, hint: "Refresh the inventory to see what is installed before trying again." };
}

/** Success is claimed only when status, operation and review ID agree with what was confirmed. */
export function checkResponse(confirmation: ApplyConfirmation, result: ApplyProjectSkillDto): ErrorDto | null {
  const status = (result as { status?: unknown } | null)?.status;
  const reviewId = (result as { review_id?: unknown } | null)?.review_id;
  if (status === "installed") {
    if (confirmation.operation !== "install") return mismatch("The core reported an install for an update request.");
    if (reviewId !== confirmation.review_id) return mismatch("The core reported a different review than the one confirmed.");
    return null;
  }
  if (status === "updated") {
    if (confirmation.operation !== "update") return mismatch("The core reported an update for an install request.");
    if (reviewId !== confirmation.review_id) return mismatch("The core reported a different review than the one confirmed.");
    return null;
  }
  if (status === "unchanged") {
    // An unchanged update answers with the prior installed receipt; an unchanged install with the requested review.
    const expected = confirmation.operation === "update" ? confirmation.expected_review_id : confirmation.review_id;
    if (reviewId !== expected) return mismatch("The core reported an unexpected review for an unchanged skill.");
    return null;
  }
  return mismatch("The core returned an unknown result.");
}

export function successText(confirmation: ApplyConfirmation, result: ApplyProjectSkillDto): string {
  if (confirmation.operation === "install") {
    return result.status === "unchanged" ? "Skill already installed; nothing changed." : "Skill installed in the project.";
  }
  return result.status === "unchanged" ? `${confirmation.name} is already current.` : `Updated ${confirmation.name}.`;
}

export function useSkillWrites(
  transport: Transport,
  projectRef: RefObject<ProjectDto | null>,
  notify: (text: string) => void,
) {
  const [entries, setEntries] = useState<Record<string, WriteEntry>>({});
  const [inventoryRevision, setInventoryRevision] = useState(0);
  const busy = useRef(new Set<string>());
  const notifyRef = useRef(notify);
  notifyRef.current = notify;

  const apply = useCallback(
    async (input: Omit<ApplyConfirmation, "project">) => {
      const target = projectRef.current?.id ?? null;
      if (target === null) return; // needs an open project
      const confirmation: ApplyConfirmation = { ...input, project: target };
      const key = writeKey(target, confirmation.engine, confirmation.name);
      if (busy.current.has(key)) return; // one write per destination at a time
      busy.current.add(key);
      const settle = (entry: WriteEntry) => setEntries((current) => ({ ...current, [key]: entry }));
      settle({ confirmation, phase: "applying" });
      const req: ApplyProjectSkillReq = {
        project: target,
        engine: confirmation.engine,
        review_id: confirmation.review_id,
        expected_review_id: confirmation.expected_review_id,
        acknowledge_warnings: confirmation.acknowledge_warnings,
      };
      try {
        const result = await transport.applyProjectSkill(req);
        const problem = checkResponse(confirmation, result);
        const live = projectRef.current?.id === target;
        if (problem) {
          settle({ confirmation, phase: "failed", error: problem });
          if (live) setInventoryRevision((n) => n + 1); // disk state is unknown now: re-read the real inventory
          return;
        }
        settle({ confirmation, phase: "done", result });
        if (live) {
          setInventoryRevision((n) => n + 1); // a different open project re-reads on its own project read
          notifyRef.current(successText(confirmation, result));
        }
      } catch (error) {
        settle({ confirmation, phase: "failed", error: toErrorDto(error) });
      } finally {
        busy.current.delete(key);
      }
    },
    [transport, projectRef],
  );

  const clear = useCallback((key: string) => {
    setEntries((current) => {
      const entry = current[key];
      if (!entry || entry.phase === "applying") return current;
      const { [key]: _dropped, ...rest } = current;
      return rest;
    });
  }, []);

  return { writes: { entries, apply, clear } satisfies SkillWrites, inventoryRevision };
}
