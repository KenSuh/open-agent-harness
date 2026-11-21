// 한국어: M1-S1·M1-S2·M2-S1 wire DTO 타입 (CONTRACTS C3/C4/C7/C8, packet §8). 백엔드 계약을 그대로 옮기며 UI에서 바꾸지 않는다.

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

/** M2-S1 Team Workflow shapes (core `dto::workflow`). */
export type SignInDto = SignInState;
export type ConfigFormat = "missing" | "legacy" | "current";
export type PlanAction =
  | "create"
  | "update"
  | "append"
  | "replace-block"
  | "link"
  | "unlink"
  | "switch"
  | "move"
  | "remove"
  | "keep";
export type ItemStatus = "done" | "stale" | "failed" | "unknown" | "not-run";

export interface SectionDto {
  id: string;
  label: string;
  agent: AgentId | null;
  /** `file`, `legacy:<key>`, `default` or `unassigned`. */
  source: string;
  independent_of: string[];
  skills: string[];
}

export interface AgentOptionDto {
  id: AgentId;
  name: string;
  /** `null` until the agent has been checked on this machine. */
  installed: boolean | null;
  sign_in: SignInState;
  disabled_reason: string | null;
}

export interface BannerSectionDto {
  id: string;
  label: string;
  agent: AgentId;
}

export interface BannerAgentDto {
  id: AgentId;
  name: string;
  login_hint: { text: string; is_command: boolean };
}

export interface BannerPairDto {
  section: string;
  other: string;
  agent: AgentId;
}

/** The one banner, chosen by core in the fixed order; the UI never re-derives it. */
export type BannerDto =
  | { kind: "not-installed"; message: string; sections: BannerSectionDto[]; suggestion: AgentId | null }
  | { kind: "not-signed-in"; message: string; agents: BannerAgentDto[] }
  | { kind: "sign-in-unknown"; message: string; agents: BannerAgentDto[] }
  | { kind: "not-independent"; message: string; pairs: BannerPairDto[] }
  | { kind: "ready"; message: string };

export interface PresetDto {
  id: string;
  label: string;
  stages: { section: string; agent: AgentId }[];
  disabled_reason: string | null;
}

export interface DriftDto {
  code: string;
  path: string;
  message: string;
}

export interface RulesStateDto {
  synced: boolean;
  drift: DriftDto[];
  writes: { path: string; action: PlanAction }[];
  error: ErrorDto | null;
}

export interface WorkflowDto {
  sections: SectionDto[];
  agents: AgentOptionDto[];
  banner: BannerDto;
  presets: PresetDto[];
  rules: RulesStateDto;
  config_format: ConfigFormat;
  config_sha256: string | null;
  upgrade_warning: string | null;
  advanced: { config_tables: string; block: string | null };
  notes: string[];
  notices: NoticeDto[];
}

export interface StagesSetReq {
  stages?: { section: string; agent: AgentId }[];
  independent_of?: { section: string; of: string[] }[];
  approve_upgrade?: string;
}

export interface PresetApplyReq {
  preset: string;
  approve_upgrade?: string;
}

export interface StagesSetDto {
  saved: boolean;
  upgraded: boolean;
  backup: string | null;
  synced: boolean;
  sync_error: ErrorDto | null;
  actions: string[];
  workflow: WorkflowDto;
}

export interface RulesSyncDto {
  synced: boolean;
  written: { path: string; action: PlanAction; status: ItemStatus }[];
  error: ErrorDto | null;
}
