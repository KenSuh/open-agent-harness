// 한국어: apply_project_skill 호출 훅 — 중복 클릭 잠금, 프로젝트가 바뀐 뒤 늦게 온 결과 무시, 확인된 성공에서만 콜백.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ApplyProjectSkillDto, ApplyProjectSkillReq, ErrorDto } from "./contract";
import { toErrorDto, type Transport } from "./transport/types";

export type ApplyState =
  | { phase: "idle" }
  | { phase: "applying" }
  | { phase: "done"; result: ApplyProjectSkillDto }
  | { phase: "failed"; error: ErrorDto };

export function useApply(
  transport: Transport,
  projectId: string | null,
  onDone?: (result: ApplyProjectSkillDto) => void,
) {
  const [state, setState] = useState<ApplyState>({ phase: "idle" });
  const busy = useRef(false);
  const alive = useRef(true);
  const project = useRef(projectId);
  const done = useRef(onDone);
  project.current = projectId;
  done.current = onDone;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const apply = useCallback(
    async (req: Omit<ApplyProjectSkillReq, "project">) => {
      const target = project.current;
      if (busy.current || target === null) return; // one write at a time; needs an open project
      busy.current = true;
      setState({ phase: "applying" });
      const live = () => alive.current && project.current === target;
      try {
        const result = await transport.applyProjectSkill({ project: target, ...req });
        if (!live()) return;
        setState({ phase: "done", result });
        done.current?.(result);
      } catch (error) {
        if (live()) setState({ phase: "failed", error: toErrorDto(error) });
      } finally {
        busy.current = false;
      }
    },
    [transport],
  );

  return { state, apply };
}
