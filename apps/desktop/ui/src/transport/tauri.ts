// 한국어: 운영 전송 계층 — 실제 Tauri invoke/Channel/event 만 사용한다 (mock·웹 저장소 없음).
import { Channel, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type {
  AgentStatusDto,
  AgentsChangedDto,
  AppInfoDto,
  BannerDto,
  NoticeDto,
  ProjectDto,
  RefreshDto,
  RulesStateDto,
  RulesSyncDto,
  SettingsDto,
  SettingsEdit,
  StagesSetDto,
  TaskEvent,
  TaskId,
  TaskSnapshotDto,
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
    sectionsShow: (req) => call<WorkflowDto>("sections_show", req),
    stagesSet: (req) => call<StagesSetDto>("stages_set", req),
    presetApply: (req) => call<StagesSetDto>("preset_apply", req),
    workflowStatus: () => call<BannerDto>("workflow_status"),
    rulesCheck: () => call<RulesStateDto>("rules_check"),
    rulesSync: (req) => call<RulesSyncDto>("rules_sync", req),
    onNotice: (handler) => listen<NoticeDto>("notice", (event) => handler(event.payload)),
    onScanChanged: (handler) =>
      listen<ScanChanged>("scan-changed", (event) => handler(event.payload)),
    onAgentsChanged: (handler) =>
      listen<AgentsChangedDto>("agents-changed", (event) => handler(event.payload)),
  };
}
