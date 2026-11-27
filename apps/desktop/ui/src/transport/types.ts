// 한국어: UI가 의존하는 전송 계층 인터페이스. 운영 구현은 tauri.ts, 테스트 구현은 tests/support 에만 있다.
import type {
  AgentStatusDto,
  AgentsChangedDto,
  AgentsProbeReq,
  AppInfoDto,
  ErrorDto,
  BannerDto,
  ImpactMapRunDto,
  ImpactMapRunReq,
  InstalledSkillDto,
  InstalledSkillManagementDto,
  InstalledSkillTarget,
  LinkedSkillSourceDto,
  RegisterSkillSourceReq,
  RemoveInstalledSkillReq,
  RemovedSkillDto,
  NoticeDto,
  PresetApplyReq,
  ProjectDto,
  ProjectToolsApplyDto,
  ProjectToolsApplyReq,
  ProjectToolsDto,
  ProjectToolsPlanDto,
  ProjectToolsPlanReq,
  ProjectWikiDto,
  RefreshDto,
  RulesStateDto,
  ReviewResultDto,
  ApplyProjectSkillDto,
  ApplyProjectSkillReq,
  RulesSyncDto,
  SettingsDto,
  SettingsEdit,
  StagesSetDto,
  StagesSetReq,
  StartReviewReq,
  TaskEvent,
  TaskId,
  TaskSnapshotDto,
  WikiActionReq,
  WikiApplyDto,
  WikiApplyReq,
  WikiPlanDto,
  WikiSearchDto,
  WikiSearchReq,
  WikiIndexInput,
  WikiIndexReply,
  WorkflowDto,
  WorkflowProjectReq,
} from "../contract";
import type { JevActionReq, JevReply } from "../jevContract";

export interface ScanChanged {
  scope: "skill" | "root" | "config" | "rules" | "all";
  keys: string[];
}

export type Unlisten = () => void;

/** One method per M1-S1, M1-S2, M1-S3 (start_review) and M2-S1 command of CONTRACTS C15, plus the three global events. */
export interface Transport {
  appInfo(): Promise<AppInfoDto>;
  checkAppUpdate(): Promise<{ readonly version: string } | null>;
  installAppUpdate(version: string): Promise<void>;
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
  skillsInstalled(): Promise<InstalledSkillDto[]>;
  inspectInstalledSkill(req: InstalledSkillTarget): Promise<InstalledSkillManagementDto>;
  registerSkillSource(req: RegisterSkillSourceReq): Promise<LinkedSkillSourceDto>;
  removeInstalledSkill(req: RemoveInstalledSkillReq): Promise<RemovedSkillDto>;
  applyProjectSkill(req: ApplyProjectSkillReq): Promise<ApplyProjectSkillDto>;
  sectionsShow(req: WorkflowProjectReq): Promise<WorkflowDto>;
  stagesSet(req: WorkflowProjectReq<StagesSetReq>): Promise<StagesSetDto>;
  presetApply(req: WorkflowProjectReq<PresetApplyReq>): Promise<StagesSetDto>;
  workflowStatus(): Promise<BannerDto>;
  rulesCheck(): Promise<RulesStateDto>;
  rulesSync(req: WorkflowProjectReq): Promise<RulesSyncDto>;
  projectToolsStatus(req: WorkflowProjectReq): Promise<ProjectToolsDto>;
  startImpactMap(
    req: WorkflowProjectReq<ImpactMapRunReq>,
    onEvent: (event: TaskEvent<ImpactMapRunDto>) => void,
  ): Promise<TaskId>;
  /** Reads only: what the edits would change. Empty edits return the saved settings. */
  projectToolsPlan(req: WorkflowProjectReq<ProjectToolsPlanReq>): Promise<ProjectToolsPlanDto>;
  /** Saves the edits exactly as the plan `plan_id` names showed them. */
  projectToolsApply(req: WorkflowProjectReq<ProjectToolsApplyReq>): Promise<ProjectToolsApplyDto>;
  /** Reads only: the folders that look like a wiki and the one in use. */
  projectWikiOverview(req: WorkflowProjectReq): Promise<ProjectWikiDto>;
  /** Reads only: what a setup or a copy would create, keep and change. */
  projectWikiPlan(req: WorkflowProjectReq<WikiActionReq>): Promise<WikiPlanDto>;
  /** Applies the request exactly as the plan `plan_id` names showed it; `taskCancel` stops it before the next file. */
  startProjectWikiApply(req: WorkflowProjectReq<WikiApplyReq>, onEvent: (event: TaskEvent<WikiApplyDto>) => void): Promise<TaskId>;
  projectWikiSearch(req: WorkflowProjectReq<WikiSearchReq>): Promise<WikiSearchDto>;
  /** One Jev action of one project (consent, device key, connection check). Only a `store-key` input carries a key; no answer can. */
  projectJevAction(req: JevActionReq): Promise<JevReply>;
  /** One explicit local wiki index action (status, probe, build or search); progress and the answer arrive on the channel. */
  startWikiIndex(req: WorkflowProjectReq<WikiIndexInput>, onEvent: (event: TaskEvent<WikiIndexReply>) => void): Promise<TaskId>;
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
