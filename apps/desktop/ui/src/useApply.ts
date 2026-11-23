// 한국어: 스킬 확인 소유자 — 확인 키별 경고 확인 값·폴더별 쓰기 잠금과 결과·인벤토리 관찰을 컨트롤러가 소유하고, 보낼 요청을 그 확인에서 바로 만든다.
import { useCallback, useMemo, useRef, useState, type RefObject } from "react";
import type { ApplyProjectSkillDto, ErrorDto, InstalledSkillDto, ProjectDto } from "./contract";
import {
  acquisitionOf,
  checkResponse,
  requestFor,
  sameAcquisition,
  successText,
  writeKey,
  type Acquired,
  type Confirmation,
} from "./confirmation";
import { toErrorDto, type Transport } from "./transport/types";

/** A submitted confirmation with the acknowledgement frozen when it was sent. */
export type Operation = { readonly confirmation: Confirmation; readonly acknowledged: boolean } & (
  | { readonly phase: "applying" }
  | { readonly phase: "done"; readonly result: ApplyProjectSkillDto }
  | { readonly phase: "failed"; readonly error: ErrorDto }
);

export interface SkillConfirmations {
  /** The committed selection confirmations are built under; `project` is `null` when none is open. */
  readonly selection: { readonly project: string | null; readonly generation: number };
  /** One operation per project folder (`writeKey`); an applying one is the folder's write lock. */
  readonly operations: Readonly<Record<string, Operation>>;
  /** What the newest inventory read of the current selection showed at each project folder. */
  readonly acquired: Readonly<Record<string, Acquired>>;
  /** The one editable acknowledgement of this exact confirmation; unchecked until the user sets it. */
  acknowledged(confirmation: Confirmation): boolean;
  acknowledge(confirmation: Confirmation, value: boolean): void;
  /** Sends the confirmation's own request unless its selection, receipt, lock or acknowledgement no longer holds. */
  apply(confirmation: Confirmation): Promise<void>;
  /** Drops a finished outcome; an operation that is still applying is kept. */
  clear(destination: string): void;
  /** Records an inventory read made for this project and generation; a changed receipt voids every confirmation built on the old one. */
  observe(project: string, generation: number, rows: readonly InstalledSkillDto[]): void;
}

interface Observed {
  readonly project: string | null;
  readonly generation: number;
  readonly folders: Readonly<Record<string, Acquired>>;
}

interface Acknowledgement {
  readonly value: boolean;
  readonly destination: string;
  readonly generation: number;
  /** An update acknowledgement goes with the receipt it was given for. */
  readonly update: boolean;
}

const NOTHING_OBSERVED: Observed = { project: null, generation: -1, folders: {} };
const NO_FOLDERS: Readonly<Record<string, Acquired>> = {};

export function useSkillConfirmations(
  transport: Transport,
  projectRef: RefObject<ProjectDto | null>,
  selectionRef: RefObject<number>,
  project: string | null,
  generation: number,
  notify: (text: string) => void,
) {
  const [operations, setOperations] = useState<Record<string, Operation>>({});
  const [inventoryRevision, setInventoryRevision] = useState(0);
  const [observed, setObserved] = useState<Observed>(NOTHING_OBSERVED);
  const [acknowledgements, setAcknowledgements] = useState<Readonly<Record<string, Acknowledgement>>>({});
  // Refs mirror what dispatch must read synchronously, before React commits a newer render.
  const observedRef = useRef<Observed>(NOTHING_OBSERVED);
  const acknowledgementsRef = useRef<Readonly<Record<string, Acknowledgement>>>({});
  const busy = useRef(new Set<string>());
  const notifyRef = useRef(notify);
  notifyRef.current = notify;

  const saveAcknowledgements = useCallback((next: Readonly<Record<string, Acknowledgement>>) => {
    acknowledgementsRef.current = next;
    setAcknowledgements(next);
  }, []);

  const selected = useCallback(
    (confirmation: Confirmation) => projectRef.current?.id === confirmation.project && selectionRef.current === confirmation.generation,
    [projectRef, selectionRef],
  );

  const observe = useCallback(
    (projectId: string, at: number, rows: readonly InstalledSkillDto[]) => {
      if (projectRef.current?.id !== projectId || selectionRef.current !== at) return; // a read made for an earlier selection
      const previous = observedRef.current;
      const before = previous.project === projectId && previous.generation === at ? previous.folders : NO_FOLDERS;
      const folders: Record<string, Acquired> = { ...before };
      const changed = new Set<string>();
      const seen = new Set<string>();
      for (const row of rows) {
        if (row.scope !== "project") continue;
        const destination = writeKey(projectId, row.engine, row.name);
        if (seen.has(destination)) continue;
        seen.add(destination);
        const acquisition = acquisitionOf(row.managed);
        const prior = before[destination];
        if (prior === undefined) {
          folders[destination] = { acquisition, revision: 0 };
        } else if (!sameAcquisition(prior.acquisition, acquisition)) {
          folders[destination] = { acquisition, revision: prior.revision + 1 };
          changed.add(destination);
        }
      }
      for (const [destination, prior] of Object.entries(before)) {
        if (seen.has(destination) || prior.acquisition === null) continue;
        folders[destination] = { acquisition: null, revision: prior.revision + 1 };
        changed.add(destination);
      }
      const next: Observed = { project: projectId, generation: at, folders };
      observedRef.current = next;
      setObserved(next);
      // A replaced receipt takes the acknowledgements of its update confirmations with it.
      const current = acknowledgementsRef.current;
      const kept = Object.entries(current).filter(
        ([, ack]) => ack.generation === at && !(ack.update && changed.has(ack.destination)),
      );
      if (kept.length !== Object.keys(current).length) saveAcknowledgements(Object.fromEntries(kept));
    },
    [projectRef, selectionRef, saveAcknowledgements],
  );

  const acknowledge = useCallback(
    (confirmation: Confirmation, value: boolean) => {
      if (!selected(confirmation)) return;
      saveAcknowledgements({
        ...acknowledgementsRef.current,
        [confirmation.key]: {
          value,
          destination: confirmation.destination,
          generation: confirmation.generation,
          update: confirmation.acquired !== null,
        },
      });
    },
    [selected, saveAcknowledgements],
  );

  const apply = useCallback(
    async (confirmation: Confirmation) => {
      if (!selected(confirmation)) return; // the selection it was built under is gone
      const { acquired, destination } = confirmation;
      if (acquired !== null) {
        const seen = observedRef.current;
        const now = seen.project === confirmation.project && seen.generation === confirmation.generation ? seen.folders[destination] : undefined;
        // Only the exact receipt the check started from, unchanged since then, may be replaced.
        if (now === undefined || now.revision !== acquired.revision || !sameAcquisition(now.acquisition, acquired.acquisition)) return;
      }
      const acknowledged = confirmation.warnings && acknowledgementsRef.current[confirmation.key]?.value === true;
      if (confirmation.warnings && !acknowledged) return;
      if (busy.current.has(destination)) return; // one write per destination at a time
      busy.current.add(destination);
      const settle = (operation: Operation) => setOperations((current) => ({ ...current, [destination]: operation }));
      settle({ confirmation, acknowledged, phase: "applying" });
      try {
        const result = await transport.applyProjectSkill(requestFor(confirmation, acknowledged));
        const problem = checkResponse(confirmation, result);
        const open = projectRef.current?.id === confirmation.project;
        if (problem) {
          settle({ confirmation, acknowledged, phase: "failed", error: problem });
          if (open) setInventoryRevision((n) => n + 1); // disk state is unknown now: re-read the real inventory
          return;
        }
        settle({ confirmation, acknowledged, phase: "done", result });
        // Any selection of the written project re-reads it; a different open project re-reads on its own project read.
        if (open) setInventoryRevision((n) => n + 1);
        // Only the selection that confirmed the write hears its success; an A to B to A return starts fresh.
        if (selected(confirmation)) notifyRef.current(successText(confirmation, result));
      } catch (error) {
        settle({ confirmation, acknowledged, phase: "failed", error: toErrorDto(error) });
      } finally {
        busy.current.delete(destination);
      }
    },
    [transport, projectRef, selected],
  );

  const clear = useCallback((destination: string) => {
    setOperations((current) => {
      const operation = current[destination];
      if (!operation || operation.phase === "applying") return current;
      const { [destination]: _dropped, ...rest } = current;
      return rest;
    });
  }, []);

  const acknowledged = useCallback(
    (confirmation: Confirmation) => acknowledgements[confirmation.key]?.value === true,
    [acknowledgements],
  );

  const selection = useMemo(() => ({ project, generation }), [project, generation]);
  const acquired = observed.project === project && observed.generation === generation ? observed.folders : NO_FOLDERS;
  return {
    confirmations: { selection, operations, acquired, acknowledged, acknowledge, apply, clear, observe } satisfies SkillConfirmations,
    inventoryRevision,
  };
}
