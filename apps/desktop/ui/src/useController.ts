// 한국어: 화면 상태와 동작(열기·새로고침·취소·설정 저장)을 한 곳에 모은 컨트롤러 훅. 제품 데이터는 전송 계층 응답만 쓴다.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AppInfoDto,
  ErrorDto,
  NoticeDto,
  Progress,
  ProjectDto,
  RefreshDto,
  SettingsDto,
  SettingsEdit,
  TaskEvent,
  TaskStateName,
  Theme,
} from "./contract";
import { toErrorDto, type Transport } from "./transport/types";
import { useAgents } from "./useAgents";

export type Load<T> =
  | { status: "loading" }
  | { status: "ready"; value: T }
  | { status: "error"; error: ErrorDto };

export interface RefreshTask {
  id: string | null;
  state: TaskStateName;
  progress: Progress | null;
}

export interface Toast {
  key: number;
  level: "info" | "warning" | "error";
  text: string;
  hint?: string | null;
}

const TOAST_MS = 6000;

/** One entry per notice id; a later list updates an earlier entry in place. */
function mergeNotices(...lists: ReadonlyArray<readonly NoticeDto[]>): NoticeDto[] {
  const byId = new Map<string, NoticeDto>();
  for (const list of lists) for (const notice of list) byId.set(notice.id, notice);
  return [...byId.values()];
}

export function useController(transport: Transport) {
  const [info, setInfo] = useState<Load<AppInfoDto>>({ status: "loading" });
  const [settings, setSettings] = useState<Load<SettingsDto>>({ status: "loading" });
  const [project, setProject] = useState<ProjectDto | null>(null);
  const [projectBusy, setProjectBusy] = useState(false);
  const [eventNotices, setEventNotices] = useState<NoticeDto[]>([]);
  const [refresh, setRefresh] = useState<RefreshTask | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastKey = useRef(0);
  const refreshRunning = useRef(false);
  const refreshRun = useRef(0);
  const projectRef = useRef<ProjectDto | null>(null);

  const pushToast = useCallback((toast: Omit<Toast, "key">) => {
    const key = ++toastKey.current;
    // An identical message already on screen is not stacked again.
    setToasts((current) =>
      current.some((item) => item.text === toast.text && item.level === toast.level)
        ? current
        : [...current, { ...toast, key }],
    );
    window.setTimeout(() => {
      setToasts((current) => current.filter((item) => item.key !== key));
    }, TOAST_MS);
  }, []);

  const dismissToast = useCallback((key: number) => {
    setToasts((current) => current.filter((item) => item.key !== key));
  }, []);

  const toastError = useCallback(
    (error: ErrorDto) => pushToast({ level: "error", text: `${error.code}: ${error.message}`, hint: error.hint }),
    [pushToast],
  );

  const agentNotify = useMemo(
    () => ({ info: (text: string) => pushToast({ level: "info", text }), error: toastError }),
    [pushToast, toastError],
  );
  const agents = useAgents(transport, agentNotify);

  const applyProject = useCallback((next: ProjectDto | null) => {
    projectRef.current = next;
    setProject(next);
  }, []);

  /**
   * Follows one refresh task through a channel until its terminal event. A new start and a
   * restored task both go through start_refresh: core joins a running refresh and returns its id.
   */
  const followRefresh = useCallback(
    async (initial: RefreshTask) => {
      const run = ++refreshRun.current;
      const current = () => refreshRun.current === run;
      let settled = false;
      refreshRunning.current = true;
      setRefresh(initial);
      const finish = (state: TaskStateName): boolean => {
        if (settled || !current()) return false;
        settled = true;
        refreshRunning.current = false;
        setRefresh((task) => (task ? { ...task, state } : task));
        return true;
      };
      const onEvent = (event: TaskEvent<RefreshDto>) => {
        switch (event.event) {
          case "progress":
            if (!settled && current()) setRefresh((task) => (task ? { ...task, progress: event.data } : task));
            break;
          case "done":
            if (finish("done")) {
              pushToast({
                level: "info",
                text: `Refreshed: ${event.data.projects} project(s), ${event.data.notices} notice(s).`,
              });
            }
            break;
          case "failed":
            if (finish("failed")) toastError(event.data);
            break;
          case "cancelled":
            if (finish("cancelled")) pushToast({ level: "info", text: "Refresh cancelled." });
            break;
        }
      };
      try {
        const id = await transport.startRefresh(onEvent);
        // The terminal event may arrive before this response; the id is still the task's id.
        if (current()) setRefresh((task) => (task ? { ...task, id } : task));
      } catch (error) {
        if (settled || !current()) return;
        settled = true;
        refreshRunning.current = false;
        setRefresh(null);
        toastError(toErrorDto(error));
      }
    },
    [transport, pushToast, toastError],
  );

  const loadInfo = useCallback(() => {
    setInfo({ status: "loading" });
    transport.appInfo().then(
      (value) => setInfo({ status: "ready", value }),
      (error) => setInfo({ status: "error", error: toErrorDto(error) }),
    );
  }, [transport]);

  const loadSettings = useCallback(() => {
    setSettings({ status: "loading" });
    transport.settingsGet().then(
      (value) => setSettings({ status: "ready", value }),
      (error) => setSettings({ status: "error", error: toErrorDto(error) }),
    );
  }, [transport]);

  useEffect(() => {
    loadInfo();
    loadSettings();
    transport.projectCurrent().then(applyProject, (error) => toastError(toErrorDto(error)));
    // A reloaded webview shows tasks that are still running (C4 tasks_list) and joins the
    // running refresh so this webview receives its remaining events, including the terminal one.
    transport.tasksList().then(
      (tasks) => {
        const running = tasks.find((task) => task.kind === "refresh" && task.state === "running");
        if (running && !refreshRunning.current) {
          void followRefresh({ id: running.id, state: "running", progress: running.progress });
        }
      },
      (error) => toastError(toErrorDto(error)),
    );
  }, [transport, loadInfo, loadSettings, applyProject, toastError, followRefresh]);

  useEffect(() => {
    let disposed = false;
    const unlisteners: Array<() => void> = [];
    const keep = (pending: Promise<() => void>) =>
      pending.then(
        (unlisten) => (disposed ? unlisten() : unlisteners.push(unlisten)),
        (error) => toastError(toErrorDto(error)),
      );
    keep(
      transport.onNotice((notice) => {
        setEventNotices((current) => mergeNotices(current, [notice]));
        pushToast({ level: notice.level === "warning" ? "warning" : "info", text: notice.message, hint: notice.hint });
      }),
    );
    keep(
      transport.onScanChanged(() => {
        // Invalidation signal only: answer by calling a read command.
        transport.projectCurrent().then(applyProject, (error) => toastError(toErrorDto(error)));
      }),
    );
    return () => {
      disposed = true;
      unlisteners.forEach((unlisten) => unlisten());
    };
  }, [transport, applyProject, pushToast, toastError]);

  const openProject = useCallback(async () => {
    setProjectBusy(true);
    try {
      const opened = await transport.projectOpenDialog();
      if (opened) applyProject(opened); // null = picker cancelled: nothing changes.
    } catch (error) {
      toastError(toErrorDto(error));
    } finally {
      setProjectBusy(false);
    }
  }, [transport, applyProject, toastError]);

  const startRefresh = useCallback(async () => {
    if (refreshRunning.current) return; // one running refresh at a time
    if (!projectRef.current) {
      pushToast({ level: "info", text: "Open a project folder first." });
      return;
    }
    await followRefresh({ id: null, state: "running", progress: null });
  }, [pushToast, followRefresh]);

  const cancelRefresh = useCallback(
    async (id: string) => {
      try {
        await transport.taskCancel(id);
      } catch (error) {
        toastError(toErrorDto(error));
      }
    },
    [transport, toastError],
  );

  const saveSettings = useCallback(
    async (edits: SettingsEdit[]) => {
      try {
        const next = await transport.settingsSet(edits);
        setSettings({ status: "ready", value: next });
      } catch (error) {
        toastError(toErrorDto(error));
      }
    },
    [transport, toastError],
  );

  const setTheme = useCallback((theme: Theme) => saveSettings([{ key: "ui.theme", value: theme }]), [saveSettings]);
  const dismissQuickStart = useCallback(
    () => saveSettings([{ key: "ui.quick_start_dismissed", value: true }]),
    [saveSettings],
  );

  // Startup notices from app_info stay visible across project reads; ids keep one entry each.
  const notices = mergeNotices(info.status === "ready" ? info.value.notices : [], project?.notices ?? [], eventNotices);

  return {
    info,
    settings,
    project,
    projectBusy,
    notices,
    refresh,
    toasts,
    agents,
    reloadInfo: loadInfo,
    reloadSettings: loadSettings,
    openProject,
    startRefresh,
    cancelRefresh,
    setTheme,
    dismissQuickStart,
    dismissToast,
  };
}
