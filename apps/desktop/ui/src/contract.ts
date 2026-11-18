// 한국어: M1-S1 wire DTO 타입 (CONTRACTS C3/C4/C7, packet §8). 백엔드 계약을 그대로 옮기며 UI에서 바꾸지 않는다.

export interface ErrorDto {
  code: string;
  message: string;
  /** Always present on the wire; `null` when there is no hint. */
  hint: string | null;
}

export type NoticeLevel = "info" | "warning";

export interface NoticeDto {
  id: string;
  level: NoticeLevel;
  code: string;
  message: string;
  hint: string | null;
  actions: string[];
}

export type CliState = "copied" | "unchanged" | "kept-previous" | "unknown";

export interface CliDto {
  state: CliState;
  path: string;
  version: string | null;
}

export interface AppInfoDto {
  version: string;
  os: string;
  oah_home: string;
  /** `null` until the shell has run the startup CLI copy. */
  cli: CliDto | null;
  /** Startup notices (CLI copy) kept so a webview that subscribes late still sees them. */
  notices: NoticeDto[];
}

export type ProjectKind = "git" | "folder";
export type ProjectConfigState =
  | { state: "missing" }
  | { state: "legacy" }
  | { state: "current" }
  | ({ state: "invalid" } & { error: ErrorDto });

export interface ProjectDto {
  id: string;
  root: string;
  kind: ProjectKind;
  config: ProjectConfigState;
  notices: NoticeDto[];
}

export type TaskId = string;
export type TaskStateName = "running" | "done" | "failed" | "cancelled";

export interface Progress {
  phase: string;
  done: number;
  total: number | null;
}

export interface TaskSnapshotDto {
  id: TaskId;
  kind: string;
  state: TaskStateName;
  progress: Progress | null;
}

export interface RefreshDto {
  projects: number;
  notices: number;
}

export type Theme = "auto" | "light" | "dark";

export interface SettingsDto {
  automation_level: string;
  ui: { theme: Theme; quick_start_dismissed: boolean };
}

export interface SettingsEdit {
  key: string;
  /** Untagged on the wire: bool, integer or string. */
  value: string | boolean | number;
}

/** Task channel event: JSON `{"event": "...", "data": ...}` (C4). */
export type TaskEvent<R> =
  | { event: "progress"; data: Progress }
  | { event: "done"; data: R }
  | { event: "failed"; data: ErrorDto }
  | { event: "cancelled" };
