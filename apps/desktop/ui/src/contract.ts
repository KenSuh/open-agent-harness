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
  /** Device choices for the local wiki index tools; absent in an older core. Keys: `wiki_index.gbrain`, `.bun_entry`, `.ollama_port`. */
  wiki_index?: { gbrain: string; bun_entry: string; ollama_port: number };
}

export interface SettingsEdit {
  key: string;
  /** Untagged on the wire: bool, integer or string. */
  value: string | boolean | number;
}

/** An agent's config id (core `AgentId::as_str`), in core's display order. */
export type AgentId = "claude" | "codex" | "gemini" | "copilot" | "opencode" | "antigravity" | "cursor";

/** Present only on a project installation the backend proved was installed from a reviewed snapshot. */
export interface ManagedSkillDto {
  readonly review_id: string;
  readonly snapshot_id: string;
  readonly repo: string;
  readonly path: string;
}

export interface InstalledSkillDto {
  readonly name: string;
  readonly engine: AgentId;
  readonly compatible_agents?: readonly AgentId[];
  readonly scope: "project" | "user";
  readonly location: string;
  readonly managed?: ManagedSkillDto;
}

export interface InstalledSkillTarget {
  project: string | null;
  location: string;
}

export interface LinkedSkillSourceDto {
  repo: string;
  path: string;
  commit: string;
  updates_in_oah: boolean;
}

export interface InstalledSkillManagementDto {
  revision: string;
  location: string;
  agents: AgentId[];
  source: LinkedSkillSourceDto | null;
}

export interface RegisterSkillSourceReq {
  target: InstalledSkillTarget;
  revision: string;
  review_id: string;
  acknowledge_warnings: boolean;
}

export interface RemoveInstalledSkillReq {
  target: InstalledSkillTarget;
  revision: string;
  confirm_shared: boolean;
}

export interface RemovedSkillDto {
  archive: string;
}

export type ProjectSkillEngine = "claude" | "codex";

/** `apply_project_skill`'s request: the project handle (never a path), the engine and the immutable review IDs. */
export interface ApplyProjectSkillReq {
  project: string;
  engine: ProjectSkillEngine;
  review_id: string;
  /** `null` on install; the previously installed review ID on update. */
  expected_review_id: string | null;
  acknowledge_warnings: boolean;
}

export interface ApplyProjectSkillDto {
  status: "installed" | "updated" | "unchanged";
  review_id: string;
}

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
  opencode?: { checked: boolean; saved_connections: number | null; default_model: string | null };
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
  opencode_details?: boolean;
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

export interface WorkflowProjectReq<T = Record<string, never>> {
  readonly project_id: string;
  readonly input: T;
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

/** One project tool card of `project_tools_status` (core `dto::workflow`). The UI shows the text as core wrote it. */
export type ProjectToolId = "wiki" | "impact-map" | "jev";
export type ToolPathBase = "project" | "git-common-dir" | "state-dir";

export interface ToolPathDto {
  label: string;
  /** Relative to `base`, written with `/`; never a full path. */
  path: string;
  base: ToolPathBase;
}

export interface ProjectToolDto {
  id: ProjectToolId;
  name: string;
  /** A stable kebab word such as `found`, `no-commit` or `off`; the text fields carry the meaning. */
  state: string;
  status: string;
  reason: string | null;
  next_step: string;
  paths: ToolPathDto[];
  notes: string[];
}

export interface ProjectToolsDto {
  /** The project the cards are about; an answer for another project is dropped. */
  project_id: string;
  tools: ProjectToolDto[];
}

export type ImpactMapAction = "status" | "refresh";
export type ImpactGraphState = "absent" | "available" | "stale" | "unproven";

export interface ImpactMapRunReq {
  action: ImpactMapAction;
}

export interface ImpactMapRunDto {
  project_id: string;
  action: ImpactMapAction;
  changed: boolean;
  graph: ImpactGraphState | null;
  refusal: string | null;
}

/**
 * Project tool settings: `project_tools_plan` and `project_tools_apply` (core `config::tools`).
 * Both take `WorkflowProjectReq<...>`: `{ project_id, input }` under the single `req` argument.
 * Nothing here carries config bytes, a full path, a key or a credential.
 */
export type ProjectToolsEdit =
  | { kind: "wiki-dir"; value: string }
  /** `null` removes the separate list, so the Impact Map follows the wiki folder again. */
  | { kind: "impact-wiki-dirs"; value: string[] | null }
  | { kind: "impact-graph-file"; value: string }
  | { kind: "impact-rerank"; value: boolean }
  | { kind: "jev-enabled"; value: boolean }
  | { kind: "wiki-index-managed"; value: boolean }
  | { kind: "wiki-index-exclude"; value: string[] }
  | { kind: "wiki-index-embedding-model"; value: string }
  | { kind: "graphify-managed"; value: boolean };

/** A value and whether the file gives it; without the key the value is the default. */
export interface ToolSettingDto<T> {
  value: T;
  explicit: boolean;
}

/** The settings as `.oah/config.toml` has them now (core `ProjectTools`, same nesting). */
export interface ProjectToolsSettingsDto {
  wiki: { dir: ToolSettingDto<string> };
  impact: {
    wiki_dirs: ToolSettingDto<string[]>;
    graph_file: ToolSettingDto<string>;
    rerank: ToolSettingDto<boolean>;
  };
  jev: { enabled: ToolSettingDto<boolean>; provider: ToolSettingDto<string> };
  managed: {
    wiki_index: {
      managed: ToolSettingDto<boolean>;
      exclude: ToolSettingDto<string[]>;
      embedding_model: ToolSettingDto<string>;
    };
    graphify: { managed: ToolSettingDto<boolean> };
  };
}

/** The `[project_tools]` parts core offers for editing; one that is not listed is not shown. */
export type ProjectToolsOptionalSection = "wiki-index" | "graphify";

/** A value of one settings key as the file writes it. */
export type ToolSettingValue = string | boolean | string[];

/** One key the save changes; `null` is a key the file does not have. `key` is dotted, such as `wiki.dir`. */
export interface ToolsKeyChangeDto {
  key: string;
  before: ToolSettingValue | null;
  after: ToolSettingValue | null;
}

export type ToolsKeptReason = "explicit-wiki-dirs" | "other-provider";

/** A value the plan leaves as the file has it, with the reason. */
export interface ToolsKeptDto {
  key: string;
  value: ToolSettingValue;
  reason: ToolsKeptReason;
}

/** For a v0.4.0 file: the one-way upgrade to acknowledge, for exactly the bytes `fingerprint` names. */
export interface ToolsUpgradeDto {
  message: string;
  fingerprint: string;
}

/** For a v0.4.0 file: what the save finds at the backup path (relative to the project, written with `/`). */
export interface ToolsBackupDto {
  state: "write" | "reuse" | "conflict";
  path: string;
}

/** `project_tools_plan`'s input. An empty `edits` reads the saved settings and plans nothing. */
export interface ProjectToolsPlanReq {
  edits: ProjectToolsEdit[];
}

/** What the edits would do, computed by reading only. */
export interface ProjectToolsPlanDto {
  /** The project the plan is for; a plan for another project is dropped. */
  project_id: string;
  /** Names this project, the file's bytes now and the bytes the save would write. */
  plan_id: string;
  /** The target file, relative to the project and written with `/`: `.oah/config.toml`. */
  config_path: string;
  settings: ProjectToolsSettingsDto;
  optional: ProjectToolsOptionalSection[];
  /** Empty when there is nothing to write. */
  changes: ToolsKeyChangeDto[];
  kept: ToolsKeptDto[];
  upgrade: ToolsUpgradeDto | null;
  backup: ToolsBackupDto | null;
  notices: NoticeDto[];
}

/** `project_tools_apply`'s input: the same edits, the plan they were shown as and nothing else. */
export interface ProjectToolsApplyReq {
  edits: ProjectToolsEdit[];
  plan_id: string;
  /** The plan's `upgrade.fingerprint`, sent only after the user acknowledged that plan's upgrade. */
  approve_upgrade?: string;
}

export interface ProjectToolsApplyDto {
  project_id: string;
  plan_id: string;
  /** `false` when the file already had these settings. */
  saved: boolean;
  upgraded: boolean;
  /** Where the original v0.4.0 file is kept, relative to the project; `null` without an upgrade. */
  backup: string | null;
}

/**
 * Project Wiki actions: `project_wiki_overview`, `project_wiki_plan`, `start_project_wiki_apply` and
 * `project_wiki_search` (core `dto::wiki`). Each takes `WorkflowProjectReq<...>`. A path is relative to the
 * project or to the wiki folder and written with `/`; nothing here carries a full path or the bytes of a file.
 */
export type WikiActionReq =
  /** `selected` is one of the overview's folders; `today` is the plan's date, sent back by the apply. */
  | { action: "setup"; selected: string | null; today: string | null }
  | { action: "copy"; source: string };

/** `start_project_wiki_apply`'s input: the request, the plan it was shown as and nothing else. */
export interface WikiApplyReq {
  request: WikiActionReq;
  plan_id: string;
  /** The plan's `settings.upgrade.fingerprint`, sent only after the user acknowledged that plan's upgrade. */
  approve_upgrade?: string;
}

/** Without `dir` the wiki folder in use is searched. */
export interface WikiSearchReq {
  dir: string | null;
  query: string;
  max_hits: number | null;
}

export interface WikiCandidateDto {
  dir: string;
  /** One of the standard wiki locations. */
  standard: boolean;
  obsidian: boolean;
  index: boolean;
  log: boolean;
}

export type WikiTargetState = "ready" | "incomplete" | "none" | "choose" | "configured-missing" | "unavailable";

/** The folder a setup without a choice would use, and what it would do. */
export interface WikiTargetDto {
  state: WikiTargetState;
  dir: string | null;
  creates: number;
  sets_dir: boolean;
  error: ErrorDto | null;
}

export interface ProjectWikiDto {
  project_id: string;
  /** The standard folder, where a copy goes. */
  default_dir: string;
  configured: { dir: string; explicit: boolean } | null;
  settings_error: ErrorDto | null;
  candidates: WikiCandidateDto[];
  problems: { dir: string; error: ErrorDto }[];
  truncated: boolean;
  target: WikiTargetDto;
}

export type WikiActionKind = "setup" | "copy";
export type WikiFileAction = "create" | "keep" | "blocked" | "copy" | "same";
export type WikiCopyBlock =
  | "link"
  | "not-portable"
  | "case-collision"
  | "oversized"
  | "too-deep"
  | "unreadable"
  | "conflict"
  | "link-leaves-wiki";

/** One file of a plan, relative to the wiki folder. `bytes` is the size of a file to copy. */
export interface WikiPlanFileDto {
  path: string;
  action: WikiFileAction;
  bytes: number | null;
}

/** The settings change that follows the files; the same shapes as a settings plan's. */
export interface WikiSettingsPlanDto {
  config_path: string;
  changes: ToolsKeyChangeDto[];
  kept: { key: string; value: ToolSettingValue; reason: string }[];
  upgrade: ToolsUpgradeDto | null;
  backup: ToolsBackupDto | null;
}

/** What the request would do, computed by reading only. */
export interface WikiPlanDto {
  project_id: string;
  /** Names the project, the request, the folders and files as they are now and the settings change. */
  plan_id: string;
  action: WikiActionKind;
  /** The wiki folder after the apply. */
  dir: string;
  source: string | null;
  mode: "create" | "adopt" | "copy";
  today: string | null;
  folders: string[];
  files: WikiPlanFileDto[];
  writes: number;
  blocked: { path: string; reason: WikiCopyBlock; line: number | null; target: string | null }[];
  blocked_total: number;
  left: string[];
  unresolved_links: { file: string; line: number; target: string }[];
  unresolved_total: number;
  settings: WikiSettingsPlanDto | null;
}

export type WikiItemStatus = "created" | "kept" | "failed" | "pending";
export type WikiSettingsState = "unchanged" | "saved" | "skipped" | "stale" | "failed";

/** The `done` payload of a wiki apply: what happened to each file, then to the settings. */
export interface WikiApplyDto {
  project_id: string;
  plan_id: string;
  action: WikiActionKind;
  dir: string;
  source: string | null;
  folders_created: string[];
  items: { path: string; status: WikiItemStatus }[];
  counts: { created: number; kept: number; failed: number; pending: number };
  /** Why the files stopped; `OAH_WIKI_STOPPED` when the user stopped the apply. */
  stop: ErrorDto | null;
  settings: {
    state: WikiSettingsState;
    config_path: string;
    upgraded: boolean;
    backup: string | null;
    error: ErrorDto | null;
  };
  /** Every file is in place and the settings lead to `dir`. */
  complete: boolean;
}

export type WikiSkipReason = "link" | "not-portable" | "oversized" | "not-text" | "unreadable" | "too-deep";

export interface WikiSearchDto {
  project_id: string;
  dir: string;
  /** `file` is relative to `dir`; `line` counts from 1; `heading` is the nearest heading above the line. */
  hits: { file: string; line: number; heading: string | null; context: string }[];
  truncated: boolean;
  files_searched: number;
  skipped: { path: string; reason: WikiSkipReason }[];
  skipped_total: number;
}

/** M1-S3 Explore review shapes (core `review`; EXPLORE-PRODUCTION-BRIEF, Independent desktop contract). */
export interface ReviewSpec {
  repo: string;
  path: string;
  ref?: string | null;
}

/** `start_review`'s request; the desktop caller leaves the optional fields out in this increment. */
export interface StartReviewReq {
  spec: ReviewSpec;
  search_name?: string | null;
}

export type GateResultName = "pass" | "warn" | "block";

/** One gate reason, shown as core returns it; `lines` are the `SKILL.md` lines a screening reason is about. */
export interface GateReasonDto {
  kind: string;
  detail: string;
  lines: number[];
  overridable?: boolean;
}

export interface GateDecisionDto {
  gate?: string;
  result: GateResultName;
  reasons: GateReasonDto[];
  compatible?: AgentId[];
}

/** The parts of `ReviewedSkillV1` the page shows; the record keeps its other serialized fields. */
export interface ReviewedSkillDto {
  version: number;
  id: string;
  source: {
    provider: string;
    locator: string;
    commit: string | null;
    tree: string | null;
    path: string;
  };
  frontmatter: { name: string | null; description: string | null };
  license: {
    verdict: "recognized" | "unknown";
    expression: string | null;
    eligibility: "eligible" | "eligible-by-choice" | "excluded" | "unknown";
  };
  status: "reviewed" | "rejected";
  reason: string | null;
  checked_at: string;
}

/** The terminal `done` payload of a review task. */
export interface ReviewResultDto {
  record: ReviewedSkillDto;
  gate: GateDecisionDto;
  unchanged: boolean;
}

/**
 * `start_wiki_index`: the optional local semantic index of the project's committed wiki notes.
 * Takes `WorkflowProjectReq<WikiIndexInput>` as the single `req` plus the task channel, and answers
 * `TaskEvent<WikiIndexReply>`. The only text the webview sends is a search query; it never sends a path, a host,
 * a binary or an argument list, and it has no action that downloads, installs, starts or approves anything.
 */
export type WikiIndexInput =
  | { action: "status" | "probe" | "build" }
  | { action: "search"; query: string };

export type WikiIndexAction = WikiIndexInput["action"];

/** Known `state` words; any other word is shown as generic text. */
export type WikiIndexKnownState = "not_built" | "incomplete" | "ready" | "stale" | "unknown";

export interface WikiIndexResultDto {
  /** Relative to the project, written with `/`. */
  file: string;
  /** 1-based. */
  line: number;
  title?: string;
  heading?: string;
  snippet: string;
}

export interface WikiIndexReply {
  /** The project the answer is about; an answer for another project is dropped. */
  project_id: string;
  /** The action this answers; an answer to another action is dropped. */
  action: WikiIndexAction;
  ok: boolean;
  state: WikiIndexKnownState | (string & {});
  /** A stable `OAH_WIKI_INDEX_*` code when `ok` is false, or when a search fell back to text. */
  code?: string;
  /** Text for the person; for a text-mode search it says why the index was not used. */
  message?: string;
  source?: { commit?: string; notes?: number };
  model?: { name: string; dimensions?: number };
  mode?: "semantic" | "text" | "none";
  results?: WikiIndexResultDto[];
  truncated?: boolean;
}
