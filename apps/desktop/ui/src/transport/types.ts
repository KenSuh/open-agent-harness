// 한국어: UI가 의존하는 전송 계층 인터페이스. 운영 구현은 tauri.ts, 테스트 구현은 tests/support 에만 있다.
import type {
  AgentStatusDto,
  AgentsChangedDto,
  AgentsProbeReq,
  AppInfoDto,
  ErrorDto,
  BannerDto,
  NoticeDto,
  PresetApplyReq,
  ProjectDto,
  RefreshDto,
  RulesStateDto,
  ReviewResultDto,
  RulesSyncDto,
  SettingsDto,
  SettingsEdit,
  StagesSetDto,
  StagesSetReq,
  StartReviewReq,
  TaskEvent,
  TaskId,
  TaskSnapshotDto,
  WorkflowDto,
  WorkflowProjectReq,
} from "../contract";

export interface ScanChanged {
  scope: "skill" | "root" | "config" | "rules" | "all";
  keys: string[];
}

export type Unlisten = () => void;

/** One method per M1-S1, M1-S2, M1-S3 (start_review) and M2-S1 command of CONTRACTS C15, plus the three global events. */
export interface Transport {
  appInfo(): Promise<AppInfoDto>;
  projectOpenDialog(): Promise<ProjectDto | null>;
  projectCurrent(): Promise<ProjectDto | null>;
  startRefresh(onEvent: (event: TaskEvent<RefreshDto>) => void): Promise<TaskId>;
  taskCancel(id: TaskId): Promise<void>;
  tasksList(): Promise<TaskSnapshotDto[]>;
  settingsGet(): Promise<SettingsDto>;
  settingsSet(edits: SettingsEdit[]): Promise<SettingsDto>;
  agentsList(): Promise<AgentStatusDto[]>;
  startAgentsProbe(req: AgentsProbeReq, onEvent: (event: TaskEvent<AgentStatusDto[]>) => void): Promise<TaskId>;
  startReview(req: StartReviewReq, onEvent: (event: TaskEvent<ReviewResultDto>) => void): Promise<TaskId>;
  sectionsShow(req: WorkflowProjectReq): Promise<WorkflowDto>;
  stagesSet(req: WorkflowProjectReq<StagesSetReq>): Promise<StagesSetDto>;
  presetApply(req: WorkflowProjectReq<PresetApplyReq>): Promise<StagesSetDto>;
  workflowStatus(): Promise<BannerDto>;
  rulesCheck(): Promise<RulesStateDto>;
  rulesSync(req: WorkflowProjectReq): Promise<RulesSyncDto>;
  onNotice(handler: (notice: NoticeDto) => void): Promise<Unlisten>;
  onScanChanged(handler: (change: ScanChanged) => void): Promise<Unlisten>;
  onAgentsChanged(handler: (change: AgentsChangedDto) => void): Promise<Unlisten>;
}

/** Normalises anything a rejected invoke can carry into an ErrorDto. */
export function toErrorDto(error: unknown): ErrorDto {
  if (typeof error === "object" && error !== null) {
    const record = error as Record<string, unknown>;
    if (typeof record.code === "string" && typeof record.message === "string") {
      return {
        code: record.code,
        message: record.message,
        hint: typeof record.hint === "string" ? record.hint : null,
      };
    }
  }
  const message = typeof error === "string" ? error : "The app core did not answer.";
  return { code: "UI_TRANSPORT_ERROR", message, hint: null };
}
