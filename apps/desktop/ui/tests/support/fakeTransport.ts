import type {
  AgentStatusDto,
  AgentsChangedDto,
  AgentsProbeReq,
  AppInfoDto,
  ApplyProjectSkillReq,
  NoticeDto,
  ProjectDto,
  RefreshDto,
  ReviewResultDto,
  SettingsDto,
  SettingsEdit,
  StagesSetDto,
  StagesSetReq,
  StartReviewReq,
  PresetApplyReq,
  TaskEvent,
  TaskSnapshotDto,
  WorkflowDto,
  WorkflowProjectReq,
} from "../../src/contract";
import type { ScanChanged, Transport } from "../../src/transport/types";
import appInfo from "../../../../../crates/oah-core/tests/dto_samples/app/app_info.json";
import settings from "../../../../../crates/oah-core/tests/dto_samples/app/settings.json";
import project from "../../../../../crates/oah-core/tests/dto_samples/project/git_legacy.json";
import claudeError from "../../../../../crates/oah-core/tests/dto_samples/agents/status-claude-error.json";
import codexSignedOut from "../../../../../crates/oah-core/tests/dto_samples/agents/status-codex-signed-out.json";
import copilotDocs from "../../../../../crates/oah-core/tests/dto_samples/agents/status-copilot-docs.json";
import cursorMissing from "../../../../../crates/oah-core/tests/dto_samples/agents/status-cursor-missing.json";
import agentsChanged from "../../../../../crates/oah-core/tests/dto_samples/agents/agents-changed.json";
import probeRequest from "../../../../../crates/oah-core/tests/dto_samples/agents/probe-request.json";
import workflowLegacy from "../../../../../crates/oah-core/tests/dto_samples/workflow/sections_show_legacy.json";
import stagesUpgraded from "../../../../../crates/oah-core/tests/dto_samples/workflow/stages_set_upgraded.json";

/** The shared Team Workflow samples (crates/oah-core/tests/dto_samples/workflow), byte-compared by core's own test. */
export const sampleWorkflow = {
  legacy: workflowLegacy as WorkflowDto,
  upgraded: stagesUpgraded as StagesSetDto,
};

/** The shared core samples (crates/oah-core/tests/dto_samples/agents), byte-compared by core's own test. */
export const sampleAgents = {
  claudeError: claudeError as AgentStatusDto,
  codexSignedOut: codexSignedOut as AgentStatusDto,
  copilotDocs: copilotDocs as AgentStatusDto,
  cursorMissing: cursorMissing as AgentStatusDto,
  changed: agentsChanged as AgentsChangedDto,
  probeRequest: probeRequest as AgentsProbeReq,
};

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
  /** What the session cache holds; `agents_list` answers with it. */
  agents: AgentStatusDto[];
  /** Every `start_agents_probe` request, in order. */
  probeRequests: AgentsProbeReq[];
  /** Sends an event to the channel of the newest agents probe start. */
  emitProbe(event: TaskEvent<AgentStatusDto[]>): void;
  emitAgentsChanged(change: AgentsChangedDto): void;
  /** Whether an agents-changed subscription is live. */
  agentsSubscribed(): boolean;
  workflowRequests: WorkflowProjectReq<StagesSetReq | PresetApplyReq | Record<string, never>>[];
  /** Every `start_review` request, in order. */
  reviewRequests: StartReviewReq[];
  /** Every `apply_project_skill` request, in order. */
  applyRequests: ApplyProjectSkillReq[];
  /** Sends an event to the channel of the review start at `index` (default: the newest). */
  emitReview(event: TaskEvent<ReviewResultDto>, index?: number): void;
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
  let probeSink: ((event: TaskEvent<AgentStatusDto[]>) => void) | null = null;
  let agentsSink: ((change: AgentsChangedDto) => void) | null = null;
  const reviewSinks: Array<(event: TaskEvent<ReviewResultDto>) => void> = [];
  let probeCount = 0;
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
    async skillsInstalled() {
      fake.calls.push("skills_installed");
      return fake.override.skillsInstalled ? fake.override.skillsInstalled() : [];
    },
    async inspectInstalledSkill(req) {
      fake.calls.push("inspect_installed_skill");
      if (!fake.override.inspectInstalledSkill) throw new Error("No skill management fixture");
      return fake.override.inspectInstalledSkill(req);
    },
    async registerSkillSource(req) {
      fake.calls.push("register_skill_source");
      if (!fake.override.registerSkillSource) throw new Error("No source registration fixture");
      return fake.override.registerSkillSource(req);
    },
    async removeInstalledSkill(req) {
      fake.calls.push("remove_installed_skill");
      if (!fake.override.removeInstalledSkill) throw new Error("No removal fixture");
      return fake.override.removeInstalledSkill(req);
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
    agents: [],
    probeRequests: [],
    async agentsList() {
      fake.calls.push("agents_list");
      return fake.override.agentsList ? fake.override.agentsList() : [...fake.agents];
    },
    async startAgentsProbe(req, onEvent) {
      fake.calls.push("start_agents_probe");
      fake.probeRequests.push(req);
      probeSink = onEvent;
      if (fake.override.startAgentsProbe) return fake.override.startAgentsProbe(req, onEvent);
      return `probe-${++probeCount}`;
    },
    async onAgentsChanged(handler) {
      if (fake.override.onAgentsChanged) return fake.override.onAgentsChanged(handler);
      agentsSink = handler;
      return () => {
        fake.calls.push("unlisten:agents-changed");
        if (agentsSink === handler) agentsSink = null;
      };
    },
    workflowRequests: [],
    reviewRequests: [],
    applyRequests: [],
    async applyProjectSkill(req) {
      fake.calls.push("apply_project_skill");
      fake.applyRequests.push(req);
      if (fake.override.applyProjectSkill) return fake.override.applyProjectSkill(req);
      return { status: req.expected_review_id === null ? "installed" : "updated", review_id: req.review_id };
    },
    async startReview(req, onEvent) {
      fake.calls.push("start_review");
      fake.reviewRequests.push(req);
      reviewSinks.push(onEvent);
      if (fake.override.startReview) return fake.override.startReview(req, onEvent);
      return `review-${reviewSinks.length}`;
    },
    emitReview: (event, index = reviewSinks.length - 1) => reviewSinks[index]?.(event),
    async sectionsShow(req) {
      fake.calls.push("sections_show");
      return fake.override.sectionsShow ? fake.override.sectionsShow(req) : sampleWorkflow.legacy;
    },
    async stagesSet(req) {
      fake.calls.push("stages_set");
      fake.workflowRequests.push(req);
      return fake.override.stagesSet ? fake.override.stagesSet(req) : sampleWorkflow.upgraded;
    },
    async presetApply(req) {
      fake.calls.push("preset_apply");
      fake.workflowRequests.push(req);
      return fake.override.presetApply ? fake.override.presetApply(req) : sampleWorkflow.upgraded;
    },
    async workflowStatus() {
      fake.calls.push("workflow_status");
      return fake.override.workflowStatus ? fake.override.workflowStatus() : sampleWorkflow.legacy.banner;
    },
    async rulesCheck() {
      fake.calls.push("rules_check");
      return fake.override.rulesCheck ? fake.override.rulesCheck() : sampleWorkflow.legacy.rules;
    },
    async rulesSync(req) {
      fake.calls.push("rules_sync");
      fake.workflowRequests.push(req);
      return fake.override.rulesSync ? fake.override.rulesSync(req) : { synced: true, written: [], error: null };
    },
    emitProbe: (event) => probeSink?.(event),
    emitAgentsChanged: (change) => agentsSink?.(change),
    agentsSubscribed: () => agentsSink !== null,
    emitRefresh: (event) => {
      if (event.event !== "progress") runningRefresh = null;
      refreshSink?.(event);
    },
    emitNotice: (notice) => noticeSink?.(notice),
    emitScan: (change) => scanSink?.(change),
  };
  return fake;
}
