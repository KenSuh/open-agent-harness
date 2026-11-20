// 한국어: M1-S1·M1-S2 wire DTO 타입 (CONTRACTS C3/C4/C7/C8, packet §8). 백엔드 계약을 그대로 옮기며 UI에서 바꾸지 않는다.

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

/** An agent's config id (core `AgentId::as_str`), in core's display order. */
export type AgentId = "claude" | "codex" | "gemini" | "copilot" | "opencode" | "antigravity" | "cursor";

/** §4.2 statuses: v0.4.0's plus `sign-in-unknown`. */
export type AgentStatus =
  | "ok"
  | "missing"
  | "too-old"
  | "not-signed-in"
  | "sign-in-unknown"
  | "unsupported-shim"
  | "error";

export type SignInState = "signed-in" | "signed-out" | "unknown";
export type CapabilityState = "verified" | "unknown" | "none";

export interface Capabilities {
  detect: CapabilityState;
  sign_in: CapabilityState;
  skills_link: CapabilityState;
  native_switch: CapabilityState;
  model_write: CapabilityState;
  model_read: CapabilityState;
  hook_prompt: CapabilityState;
  pack_update: CapabilityState;
}

/** One agent as `agents_list` and `start_agents_probe` return it (C8; core `AgentStatusDto`). */
export interface AgentStatusDto {
  id: AgentId;
  name: string;
  kind: "cli" | "ide";
  installed: boolean;
  status: AgentStatus;
  /** The version line of the version run; never status-command text. */
  version: string | null;
  /** Where the program or bundle was found, home written as `~`. */
  source: string | null;
  sign_in: SignInState;
  login_hint: { text: string; is_command: boolean };
  install_url: string;
  can_take_section: boolean;
  capabilities: Capabilities;
  /** Why the status is `error` or `unsupported-shim`; `null` otherwise. */
  problem: ErrorDto | null;
  /** RFC 3339 UTC. */
  checked_at: string;
}

/** `start_agents_probe`'s request; every field may be left out (core `AgentsProbeReq`). */
export interface AgentsProbeReq {
  agents?: AgentId[];
  sign_in?: boolean;
  focus?: boolean;
}

/** The `agents-changed` event payload (C4). */
export interface AgentsChangedDto {
  agents: AgentId[];
}

/** Task channel event: JSON `{"event": "...", "data": ...}` (C4). */
export type TaskEvent<R> =
  | { event: "progress"; data: Progress }
  | { event: "done"; data: R }
  | { event: "failed"; data: ErrorDto }
  | { event: "cancelled" };
