import type {
  AppInfoDto,
  NoticeDto,
  ProjectDto,
  RefreshDto,
  SettingsDto,
  SettingsEdit,
  TaskEvent,
  TaskSnapshotDto,
} from "../../src/contract";
import type { ScanChanged, Transport } from "../../src/transport/types";
import appInfo from "../../../../../crates/oah-core/tests/dto_samples/app/app_info.json";
import settings from "../../../../../crates/oah-core/tests/dto_samples/app/settings.json";
import project from "../../../../../crates/oah-core/tests/dto_samples/project/git_legacy.json";

export const sampleAppInfo = appInfo as AppInfoDto;
export const sampleProject = project as ProjectDto;
export const sampleSettings = settings as SettingsDto;

export interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}

export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

export interface FakeTransport extends Transport {
  calls: string[];
  settingsEdits: SettingsEdit[][];
  /** Replace a handler to control timing or failure of one command. */
  override: Partial<Transport>;
  emitRefresh(event: TaskEvent<RefreshDto>): void;
  emitNotice(notice: NoticeDto): void;
  emitScan(change: ScanChanged): void;
}

export function createFakeTransport(initial: { project?: ProjectDto | null; tasks?: TaskSnapshotDto[] } = {}): FakeTransport {
  let current: SettingsDto = {
    ...sampleSettings,
    ui: { theme: "auto", quick_start_dismissed: false },
  };
  // Like core: start_refresh while a refresh runs joins it and returns the same id.
  let runningRefresh: string | null =
    initial.tasks?.find((task) => task.kind === "refresh" && task.state === "running")?.id ?? null;
  let refreshSink: ((event: TaskEvent<RefreshDto>) => void) | null = null;
  let noticeSink: ((notice: NoticeDto) => void) | null = null;
  let scanSink: ((change: ScanChanged) => void) | null = null;
  const fake: FakeTransport = {
    calls: [],
    settingsEdits: [],
    override: {},
    async appInfo() {
      fake.calls.push("app_info");
      return fake.override.appInfo ? fake.override.appInfo() : sampleAppInfo;
    },
    async projectOpenDialog() {
      fake.calls.push("project_open_dialog");
      return fake.override.projectOpenDialog ? fake.override.projectOpenDialog() : sampleProject;
    },
    async projectCurrent() {
      fake.calls.push("project_current");
      return fake.override.projectCurrent ? fake.override.projectCurrent() : (initial.project ?? null);
    },
    async startRefresh(onEvent) {
      fake.calls.push("start_refresh");
      refreshSink = onEvent;
      if (fake.override.startRefresh) return fake.override.startRefresh(onEvent);
      runningRefresh ??= "task-1";
      return runningRefresh;
    },
    async taskCancel(id) {
      fake.calls.push(`task_cancel:${id}`);
      if (fake.override.taskCancel) await fake.override.taskCancel(id);
    },
    async tasksList() {
      fake.calls.push("tasks_list");
      return initial.tasks ?? [];
    },
    async settingsGet() {
      fake.calls.push("settings_get");
      return fake.override.settingsGet ? fake.override.settingsGet() : current;
    },
    async settingsSet(edits) {
      fake.calls.push("settings_set");
      fake.settingsEdits.push(edits);
      if (fake.override.settingsSet) return fake.override.settingsSet(edits);
      const ui = { ...current.ui };
      for (const edit of edits) {
        if (edit.key === "ui.theme") ui.theme = edit.value as typeof ui.theme;
        if (edit.key === "ui.quick_start_dismissed") ui.quick_start_dismissed = edit.value as boolean;
      }
      current = { ...current, ui };
      return current;
    },
    async onNotice(handler) {
      noticeSink = handler;
      return () => undefined;
    },
    async onScanChanged(handler) {
      scanSink = handler;
      return () => undefined;
    },
    emitRefresh: (event) => {
      if (event.event !== "progress") runningRefresh = null;
      refreshSink?.(event);
    },
    emitNotice: (notice) => noticeSink?.(notice),
    emitScan: (change) => scanSink?.(change),
  };
  return fake;
}
