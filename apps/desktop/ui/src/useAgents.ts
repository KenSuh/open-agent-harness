// 한국어: 에이전트 목록 상태 훅 — 세션 캐시 읽기, 첫 확인·다시 확인 작업 추적, agents-changed 구독만 하고 판단은 코어에 맡긴다.
import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentStatusDto, AgentsProbeReq, ErrorDto, Progress, TaskEvent, TaskStateName } from "./contract";
import { toErrorDto, type Transport } from "./transport/types";

export type AgentsLoad =
  | { status: "loading" }
  | { status: "ready"; value: AgentStatusDto[] }
  | { status: "error"; error: ErrorDto };

/** The agents probe this window started and follows through its task channel. */
export interface AgentCheck {
  id: string | null;
  origin: "initial" | "rescan";
  state: TaskStateName;
  progress: Progress | null;
}

/**
 * A window that loads is a window that became active: every agent is detected again and core checks sign-in
 * only where its own 60-second limit says it is due, exactly as the native focus probe does. Both send the same
 * request, so core runs one and refuses the other (OAH_TASK_BUSY) instead of running sign-in twice.
 */
export const INITIAL_PROBE: AgentsProbeReq = { sign_in: true, focus: true };
/** A manual rescan: all probes, sign-in included, regardless of when it was last checked. */
export const RESCAN_PROBE: AgentsProbeReq = { sign_in: true, focus: false };

const TASK_BUSY = "OAH_TASK_BUSY";

/** Stable callbacks that show a message; the controller's toasts. */
interface Notify {
  info: (text: string) => void;
  error: (error: ErrorDto) => void;
}

export function useAgents(transport: Transport, notify: Notify) {
  const [agents, setAgents] = useState<AgentsLoad>({ status: "loading" });
  const [check, setCheck] = useState<AgentCheck | null>(null);
  // A probe started elsewhere (the native focus probe) holds the same request; its agents-changed ends the wait.
  const [waiting, setWaiting] = useState(false);
  const readSeq = useRef(0);
  const agentsChangeSeq = useRef(0);
  const listening = useRef(false);
  const checkRun = useRef(0);
  const checkRunning = useRef(false);
  const hasList = useRef(false);
  const { info, error: showError } = notify;

  /** Reads the session cache. Only the newest read may publish, so an older answer never replaces a newer one. */
  const reload = useCallback(() => {
    const seq = ++readSeq.current;
    transport.agentsList().then(
      (value) => {
        if (seq !== readSeq.current) return;
        hasList.current = true;
        setAgents({ status: "ready", value });
      },
      (error) => {
        if (seq !== readSeq.current) return;
        const dto = toErrorDto(error);
        if (hasList.current) showError(dto);
        else setAgents({ status: "error", error: dto });
      },
    );
  }, [transport, showError]);

  const follow = useCallback(
    async (req: AgentsProbeReq, origin: AgentCheck["origin"]) => {
      const run = ++checkRun.current;
      const changesAtStart = agentsChangeSeq.current;
      const current = () => checkRun.current === run;
      let settled = false;
      checkRunning.current = true;
      setCheck({ id: null, origin, state: "running", progress: null });
      const finish = (state: TaskStateName): boolean => {
        if (settled || !current()) return false;
        settled = true;
        checkRunning.current = false;
        setCheck((task) => (task ? { ...task, state } : task));
        return true;
      };
      const onEvent = (event: TaskEvent<AgentStatusDto[]>) => {
        switch (event.event) {
          case "progress":
            if (!settled && current()) setCheck((task) => (task ? { ...task, progress: event.data } : task));
            break;
          case "done":
            if (finish("done")) {
              setWaiting(false);
              reload();
              if (origin === "rescan") info(`Agents checked: ${event.data.length} agent(s).`);
            }
            break;
          case "failed":
            if (finish("failed")) showError(event.data);
            break;
          case "cancelled":
            if (finish("cancelled")) info("Agent check cancelled. Nothing changed.");
            break;
        }
      };
      try {
        const id = await transport.startAgentsProbe(req, onEvent);
        if (current()) setCheck((task) => (task ? { ...task, id } : task));
      } catch (error) {
        if (settled || !current()) return;
        settled = true;
        checkRunning.current = false;
        setCheck(null);
        const dto = toErrorDto(error);
        if (dto.code !== TASK_BUSY) {
          showError(dto);
        } else if (origin === "initial") {
          setWaiting(listening.current && agentsChangeSeq.current === changesAtStart);
        } else {
          info("An agent check like this one is already running; the list updates when it ends.");
        }
      }
    },
    [transport, reload, info, showError],
  );

  useEffect(() => {
    reload();
    let disposed = false;
    let unlisten: (() => void) | null = null;
    transport
      .onAgentsChanged(() => {
        if (disposed) return;
        // Invalidation signal only: answer by reading the cache again.
        ++agentsChangeSeq.current;
        setWaiting(false);
        reload();
      })
      .then(
        (stop) => {
          if (disposed) {
            stop();
            return;
          }
          unlisten = stop;
          listening.current = true;
          void follow(INITIAL_PROBE, "initial");
        },
        (error) => {
          if (disposed) return;
          showError(toErrorDto(error));
          void follow(INITIAL_PROBE, "initial");
        },
      );
    return () => {
      disposed = true;
      listening.current = false;
      unlisten?.();
    };
  }, [transport, reload, follow, showError]);

  const rescan = useCallback(async () => {
    if (checkRunning.current) return; // one rescan from this window at a time
    await follow(RESCAN_PROBE, "rescan");
  }, [follow]);

  const cancelCheck = useCallback(
    async (id: string) => {
      try {
        await transport.taskCancel(id);
      } catch (error) {
        showError(toErrorDto(error));
      }
    },
    [transport, showError],
  );

  const checking = check?.state === "running" || waiting;
  return { agents, check, checking, reload, rescan, cancelCheck };
}
