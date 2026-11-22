// 한국어: Explore 검토 상태 훅 — owner/repo/folder 입력을 start_review 로 보내고 작업 채널의 이벤트를 한 실행에만 귀속시킨다.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ErrorDto, Progress, ReviewResultDto, TaskEvent } from "./contract";
import { toErrorDto, type Transport } from "./transport/types";

export interface ReviewTarget {
  repo: string;
  path: string;
}

export type ReviewState =
  | { phase: "idle" }
  | { phase: "running"; target: ReviewTarget; id: string | null; progress: Progress | null; cancelling: boolean }
  | { phase: "done"; target: ReviewTarget; result: ReviewResultDto }
  | { phase: "failed"; target: ReviewTarget; error: ErrorDto }
  | { phase: "cancelled"; target: ReviewTarget };

/** `owner/repo/folder` (the folder may be nested), or the reason the text is not that shape. */
export function parseTarget(input: string): { ok: true; target: ReviewTarget } | { ok: false; message: string } {
  const text = input.trim();
  const parts = text.split("/");
  if (parts.length < 3 || parts.some((part) => part === "" || /\s/.test(part))) {
    return { ok: false, message: "Enter a GitHub skill folder as owner/repo/folder, for example owner/repo/skills/my-skill." };
  }
  return { ok: true, target: { repo: parts.slice(0, 2).join("/"), path: parts.slice(2).join("/") } };
}

export function useReview(transport: Transport) {
  const [state, setState] = useState<ReviewState>({ phase: "idle" });
  const [cancelError, setCancelError] = useState<ErrorDto | null>(null);
  const run = useRef(0);
  const running = useRef(false);

  // Events that arrive after the page is gone belong to no run.
  useEffect(
    () => () => {
      run.current += 1;
    },
    [],
  );

  const submit = useCallback(
    async (target: ReviewTarget) => {
      if (running.current) return; // one review at a time
      const mine = ++run.current;
      const owns = () => run.current === mine;
      let settled = false;
      running.current = true;
      setState({ phase: "running", target, id: null, progress: null, cancelling: false });
      const finish = (next: ReviewState): void => {
        if (settled || !owns()) return;
        settled = true;
        running.current = false;
        setState(next);
      };
      const onEvent = (event: TaskEvent<ReviewResultDto>) => {
        switch (event.event) {
          case "progress":
            if (!settled && owns()) {
              setState((s) => (s.phase === "running" ? { ...s, progress: event.data } : s));
            }
            break;
          case "done":
            finish({ phase: "done", target, result: event.data });
            break;
          case "failed":
            finish({ phase: "failed", target, error: event.data });
            break;
          case "cancelled":
            finish({ phase: "cancelled", target });
            break;
        }
      };
      try {
        const id = await transport.startReview({ spec: { repo: target.repo, path: target.path } }, onEvent);
        // The terminal event may arrive before this response; the id is still the task's id.
        if (!settled && owns()) setState((s) => (s.phase === "running" ? { ...s, id } : s));
      } catch (error) {
        finish({ phase: "failed", target, error: toErrorDto(error) });
      }
    },
    [transport],
  );

  const cancel = useCallback(async () => {
    if (state.phase !== "running" || state.id === null || state.cancelling) return;
    const { id } = state;
    const mine = run.current;
    setState((s) => (s.phase === "running" ? { ...s, cancelling: true } : s));
    try {
      await transport.taskCancel(id);
    } catch (error) {
      if (run.current !== mine || !running.current) return; // a rejection belongs to the review that started it
      setState((s) => (s.phase === "running" ? { ...s, cancelling: false } : s));
      // A failed cancel leaves the review running; say why next to it.
      setCancelError(toErrorDto(error));
    }
  }, [state, transport]);

  useEffect(() => {
    if (state.phase !== "running") setCancelError(null);
  }, [state.phase]);

  return { state, submit, cancel, cancelError };
}
