import type {
  AgentStatusDto,
  AgentsChangedDto,
  AgentsProbeReq,
  AppInfoDto,
  ApplyProjectSkillReq,
  NoticeDto,
  ProjectDto,
  ProjectToolsApplyReq,
  ProjectToolsDto,
  ProjectToolsEdit,
  ProjectToolsPlanDto,
  ProjectToolsPlanReq,
  ProjectToolsSettingsDto,
  ProjectWikiDto,
  ToolSettingValue,
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
  WikiActionReq,
  WikiApplyDto,
  WikiApplyReq,
  WikiPlanDto,
  WikiSearchDto,
  WikiSearchReq,
  WorkflowDto,
  WorkflowProjectReq,
} from "../../src/contract";
import type { JevAction, JevActionReq, JevReply } from "../../src/jevContract";
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
import projectTools from "../../../../../crates/oah-core/tests/dto_samples/workflow/project_tools_status.json";
import wikiOverviewChoose from "../../../../../crates/oah-core/tests/dto_samples/wiki/overview_choose.json";
import wikiOverviewNone from "../../../../../crates/oah-core/tests/dto_samples/wiki/overview_none.json";
import wikiOverviewReady from "../../../../../crates/oah-core/tests/dto_samples/wiki/overview_ready.json";
import wikiPlanSetup from "../../../../../crates/oah-core/tests/dto_samples/wiki/plan_setup_adopt.json";
import wikiPlanCopy from "../../../../../crates/oah-core/tests/dto_samples/wiki/plan_copy.json";
import wikiApplyDone from "../../../../../crates/oah-core/tests/dto_samples/wiki/apply_done.json";
import wikiApplyStopped from "../../../../../crates/oah-core/tests/dto_samples/wiki/apply_stopped.json";
import wikiApplySettingsStale from "../../../../../crates/oah-core/tests/dto_samples/wiki/apply_settings_stale.json";
import wikiSearch from "../../../../../crates/oah-core/tests/dto_samples/wiki/search.json";

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

/** The shared project tool cards (same folder), byte-compared by core's own test. */
export const sampleProjectTools = projectTools as ProjectToolsDto;

/** The shared Project Wiki samples (crates/oah-core/tests/dto_samples/wiki), byte-compared by core's own test. */
export const sampleWiki = {
  overviewChoose: wikiOverviewChoose as ProjectWikiDto,
  overviewNone: wikiOverviewNone as ProjectWikiDto,
  overviewReady: wikiOverviewReady as ProjectWikiDto,
  planSetup: wikiPlanSetup as WikiPlanDto,
  planCopy: wikiPlanCopy as WikiPlanDto,
  applyDone: wikiApplyDone as WikiApplyDto,
  applyStopped: wikiApplyStopped as WikiApplyDto,
  applySettingsStale: wikiApplySettingsStale as WikiApplyDto,
  search: wikiSearch as WikiSearchDto,
};

/** The saved settings the fake plans from: a current-format file that gives only the wiki folder. */
export const sampleToolSettings: ProjectToolsSettingsDto = {
  wiki: { dir: { value: "docs/wiki", explicit: true } },
  impact: {
    wiki_dirs: { value: ["docs/wiki"], explicit: false },
    graph_file: { value: "graphify-out/graph.json", explicit: false },
    rerank: { value: false, explicit: false },
  },
  jev: { enabled: { value: false, explicit: false }, provider: { value: "", explicit: false } },
  managed: {
    wiki_index: {
      managed: { value: false, explicit: false },
      exclude: { value: [], explicit: false },
      embedding_model: { value: "", explicit: false },
    },
    graphify: { managed: { value: false, explicit: false } },
  },
};

const SETTING_ACCESSORS: Record<ProjectToolsEdit["kind"], [string, (s: ProjectToolsSettingsDto) => { value: ToolSettingValue; explicit: boolean }]> = {
  "wiki-dir": ["wiki.dir", (s) => s.wiki.dir],
  "impact-wiki-dirs": ["impact.wiki_dirs", (s) => s.impact.wiki_dirs],
  "impact-graph-file": ["impact.graph_file", (s) => s.impact.graph_file],
  "impact-rerank": ["impact.rerank", (s) => s.impact.rerank],
  "jev-enabled": ["rerank.enabled", (s) => s.jev.enabled],
  "wiki-index-managed": ["project_tools.wiki_index.managed", (s) => s.managed.wiki_index.managed],
  "wiki-index-exclude": ["project_tools.wiki_index.exclude", (s) => s.managed.wiki_index.exclude],
  "wiki-index-embedding-model": ["project_tools.wiki_index.embedding_model", (s) => s.managed.wiki_index.embedding_model],
  "graphify-managed": ["project_tools.graphify.managed", (s) => s.managed.graphify.managed],
};

/** Like core's plan for a current-format file: one before/after row per edit, an ID that names the edits. */
export function samplePlan(projectId: string, edits: ProjectToolsEdit[], extra: Partial<ProjectToolsPlanDto> = {}): ProjectToolsPlanDto {
  return {
    project_id: projectId,
    plan_id: `plan:${JSON.stringify(edits)}`,
    config_path: ".oah/config.toml",
    settings: sampleToolSettings,
    optional: [],
    changes: edits.map((edit) => {
      const [key, read] = SETTING_ACCESSORS[edit.kind];
      const now = read(sampleToolSettings);
      return { key, before: now.explicit ? now.value : null, after: edit.value };
    }),
    kept: [],
    upgrade: null,
    backup: null,
    notices: [],
    ...extra,
  };
}

/** What core answers for each Jev action by default: an official destination, nothing approved, no stored key. */
export function sampleJevReply(project_id: string, kind: JevAction): JevReply {
  const destination = { kind: "official" as const, url: "https://api.typesafe.ai/v1/systemone", model: "example-model" };
  const base = { project_id, ok: true };
  switch (kind) {
    case "status":
      return { ...base, action: "status", status: "ok", destination, consent: "none", scopes: [] };
    case "key-state":
      return { ...base, action: "key-state", status: "ok", key_state: { environment: false, device: "missing" } };
    case "prepare":
      return {
        ...base, action: "prepare", status: "ready", destination, token: "v1:impact:fake-token",
        scopes: [{
          scope: "impact", title: "Impact ordering", sends: ["One request per run with at most 20 rows."],
          labels: ["project-relative path", "line", "binding", "kind/layer"], never: ["file contents", "the query", "row details"], bounds: { requests: 1 },
        }],
      };
    case "enable":
      return { ...base, action: "enable", status: "enabled" };
    case "disable":
      return { ...base, action: "disable", status: "disabled" };
    case "store-key":
      return { ...base, action: "store-key", status: "added" };
    case "delete-key":
      return { ...base, action: "delete-key", status: "removed" };
    case "prepare-test":
      return { ...base, action: "prepare-test", status: "ready", destination, token: "v1:probe:fake-token", requests: 1, timeout_seconds: 10 };
    case "test":
      return { ...base, action: "test", status: "ok", source: "device" };
  }
}

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
  /** Every `project_tools_plan` and `project_tools_apply` request, in order. */
  toolsPlanRequests: WorkflowProjectReq<ProjectToolsPlanReq>[];
  toolsApplyRequests: WorkflowProjectReq<ProjectToolsApplyReq>[];
  /** Every `project_jev_action` request, in order (a store-key request holds the fake key the test typed). */
  jevRequests: JevActionReq[];
  /** Every `apply_project_skill` request, in order. */
  applyRequests: ApplyProjectSkillReq[];
  /** Every `project_wiki_plan`, `start_project_wiki_apply` and `project_wiki_search` request, in order. */
  wikiPlanRequests: WorkflowProjectReq<WikiActionReq>[];
  wikiApplyRequests: WorkflowProjectReq<WikiApplyReq>[];
  wikiSearchRequests: WorkflowProjectReq<WikiSearchReq>[];
  /** Sends an event to the channel of the wiki apply start at `index` (default: the newest). */
  emitWikiApply(event: TaskEvent<WikiApplyDto>, index?: number): void;
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
  const wikiSinks: Array<(event: TaskEvent<WikiApplyDto>) => void> = [];
  let probeCount = 0;
  const fake: FakeTransport = {
    calls: [],
    settingsEdits: [],
    override: {},
    async appInfo() {
      fake.calls.push("app_info");
      return fake.override.appInfo ? fake.override.appInfo() : sampleAppInfo;
    },
    async checkAppUpdate() {
      return fake.override.checkAppUpdate?.() ?? null;
    },
    async installAppUpdate(version) {
      await fake.override.installAppUpdate?.(version);
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
    async projectToolsStatus(req) {
      fake.calls.push("project_tools_status");
      // Like core: the answer names the project the request named.
      return fake.override.projectToolsStatus
        ? fake.override.projectToolsStatus(req)
        : { ...sampleProjectTools, project_id: req.project_id };
    },
    async startImpactMap(req, onEvent) {
      fake.calls.push("start_impact_map");
      if (fake.override.startImpactMap) return fake.override.startImpactMap(req, onEvent);
      onEvent({
        event: "done",
        data: {
          project_id: req.project_id,
          action: req.input.action,
          changed: req.input.action === "refresh",
          graph: "absent",
          refusal: null,
        },
      });
      return "task-impact";
    },
    toolsPlanRequests: [],
    toolsApplyRequests: [],
    jevRequests: [],
    async projectJevAction(req) {
      fake.calls.push("project_jev_action:" + req.input.kind);
      fake.jevRequests.push(req);
      return fake.override.projectJevAction ? fake.override.projectJevAction(req) : sampleJevReply(req.project_id, req.input.kind);
    },
    async projectToolsPlan(req) {
      fake.calls.push("project_tools_plan");
      fake.toolsPlanRequests.push(req);
      return fake.override.projectToolsPlan ? fake.override.projectToolsPlan(req) : samplePlan(req.project_id, req.input.edits);
    },
    async projectToolsApply(req) {
      fake.calls.push("project_tools_apply");
      fake.toolsApplyRequests.push(req);
      return fake.override.projectToolsApply
        ? fake.override.projectToolsApply(req)
        : { project_id: req.project_id, plan_id: req.input.plan_id, saved: true, upgraded: false, backup: null };
    },
    wikiPlanRequests: [],
    wikiApplyRequests: [],
    wikiSearchRequests: [],
    async projectWikiOverview(req) {
      fake.calls.push("project_wiki_overview");
      // Like core: the answer names the project the request named.
      return fake.override.projectWikiOverview
        ? fake.override.projectWikiOverview(req)
        : { ...sampleWiki.overviewNone, project_id: req.project_id };
    },
    async projectWikiPlan(req) {
      fake.calls.push("project_wiki_plan");
      fake.wikiPlanRequests.push(req);
      if (fake.override.projectWikiPlan) return fake.override.projectWikiPlan(req);
      return { ...(req.input.action === "copy" ? sampleWiki.planCopy : sampleWiki.planSetup), project_id: req.project_id };
    },
    async startProjectWikiApply(req, onEvent) {
      fake.calls.push("start_project_wiki_apply");
      fake.wikiApplyRequests.push(req);
      wikiSinks.push(onEvent);
      if (fake.override.startProjectWikiApply) return fake.override.startProjectWikiApply(req, onEvent);
      return `wiki-apply-${wikiSinks.length}`;
    },
    emitWikiApply: (event, index = wikiSinks.length - 1) => wikiSinks[index]?.(event),
    async projectWikiSearch(req) {
      fake.calls.push("project_wiki_search");
      fake.wikiSearchRequests.push(req);
      return fake.override.projectWikiSearch
        ? fake.override.projectWikiSearch(req)
        : { ...sampleWiki.search, project_id: req.project_id };
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
