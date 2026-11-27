// 한국어: 운영 전송 계층 — 실제 Tauri invoke/Channel/event 만 사용한다 (mock·웹 저장소 없음).
import { Channel, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type {
  AgentStatusDto,
  AgentsChangedDto,
  AppInfoDto,
  ApplyProjectSkillDto,
  BannerDto,
  ImpactMapRunDto,
  InstalledSkillDto,
  InstalledSkillManagementDto,
  LinkedSkillSourceDto,
  RemovedSkillDto,
  NoticeDto,
  ProjectDto,
  ProjectToolsApplyDto,
  ProjectToolsDto,
  ProjectToolsPlanDto,
  ProjectWikiDto,
  RefreshDto,
  ReviewResultDto,
  RulesStateDto,
  RulesSyncDto,
  SettingsDto,
  SettingsEdit,
  StagesSetDto,
  TaskEvent,
  TaskId,
  TaskSnapshotDto,
  WikiApplyDto,
  WikiPlanDto,
  WikiSearchDto,
  WorkflowDto,
} from "../contract";
import { toErrorDto, type ScanChanged, type Transport } from "./types";

/** Every command takes exactly one argument named `req` (CONTRACTS C3). */
async function call<T>(command: string, req: object = {}, extra: object = {}): Promise<T> {
  try {
    return await invoke<T>(command, { req, ...extra });
  } catch (error) {
    throw toErrorDto(error);
  }
}

export function createTauriTransport(): Transport {
  return {
    appInfo: () => call<AppInfoDto>("app_info"),
    checkAppUpdate: () => call<{ readonly version: string } | null>("check_app_update"),
    installAppUpdate: (version) => call<void>("install_app_update", { version }),
    projectOpenDialog: () => call<ProjectDto | null>("project_open_dialog"),
    projectCurrent: () => call<ProjectDto | null>("project_current"),
    startRefresh(onEvent) {
      const channel = new Channel<TaskEvent<RefreshDto>>();
      channel.onmessage = onEvent;
      return call<TaskId>("start_refresh", {}, { onEvent: channel });
    },
    taskCancel: async (id) => {
      await call<unknown>("task_cancel", { id });
    },
    tasksList: () => call<TaskSnapshotDto[]>("tasks_list"),
    settingsGet: () => call<SettingsDto>("settings_get"),
    settingsSet: (edits: SettingsEdit[]) => call<SettingsDto>("settings_set", { edits }),
    agentsList: () => call<AgentStatusDto[]>("agents_list"),
    startAgentsProbe(req, onEvent) {
      const channel = new Channel<TaskEvent<AgentStatusDto[]>>();
      channel.onmessage = onEvent;
      return call<TaskId>("start_agents_probe", req, { onEvent: channel });
    },
    startReview(req, onEvent) {
      const channel = new Channel<TaskEvent<ReviewResultDto>>();
      channel.onmessage = onEvent;
      return call<TaskId>("start_review", req, { onEvent: channel });
    },
    skillsInstalled: () => call<InstalledSkillDto[]>("skills_installed"),
    inspectInstalledSkill: (req) => call<InstalledSkillManagementDto>("inspect_installed_skill", req),
    registerSkillSource: (req) => call<LinkedSkillSourceDto>("register_skill_source", req),
    removeInstalledSkill: (req) => call<RemovedSkillDto>("remove_installed_skill", req),
    applyProjectSkill: (req) => call<ApplyProjectSkillDto>("apply_project_skill", req),
    sectionsShow: (req) => call<WorkflowDto>("sections_show", req),
    stagesSet: (req) => call<StagesSetDto>("stages_set", req),
    presetApply: (req) => call<StagesSetDto>("preset_apply", req),
    workflowStatus: () => call<BannerDto>("workflow_status"),
    rulesCheck: () => call<RulesStateDto>("rules_check"),
    rulesSync: (req) => call<RulesSyncDto>("rules_sync", req),
    projectToolsStatus: (req) => call<ProjectToolsDto>("project_tools_status", req),
    startImpactMap(req, onEvent) {
      const channel = new Channel<TaskEvent<ImpactMapRunDto>>();
      channel.onmessage = onEvent;
      return call<TaskId>("start_impact_map", req, { onEvent: channel });
    },
    projectToolsPlan: (req) => call<ProjectToolsPlanDto>("project_tools_plan", req),
    projectToolsApply: (req) => call<ProjectToolsApplyDto>("project_tools_apply", req),
    projectWikiOverview: (req) => call<ProjectWikiDto>("project_wiki_overview", req),
    projectWikiPlan: (req) => call<WikiPlanDto>("project_wiki_plan", req),
    startProjectWikiApply(req, onEvent) {
      const channel = new Channel<TaskEvent<WikiApplyDto>>();
      channel.onmessage = onEvent;
      return call<TaskId>("start_project_wiki_apply", req, { onEvent: channel });
    },
    projectWikiSearch: (req) => call<WikiSearchDto>("project_wiki_search", req),
    onNotice: (handler) => listen<NoticeDto>("notice", (event) => handler(event.payload)),
    onScanChanged: (handler) =>
      listen<ScanChanged>("scan-changed", (event) => handler(event.payload)),
    onAgentsChanged: (handler) =>
      listen<AgentsChangedDto>("agents-changed", (event) => handler(event.payload)),
  };
}
