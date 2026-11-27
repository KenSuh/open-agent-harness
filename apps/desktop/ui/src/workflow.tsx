// 한국어: 팀 작업 흐름 화면 — 섹션 카드·에이전트 선택·단일 배너·프리셋·업그레이드 확인·규칙 동기 상태·고급 보기와 프로젝트 도구(위키·영향 지도·Jev) 상태 카드·설정 편집 진입. 판단은 모두 core 응답을 그대로 쓴다.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type {
  AgentId,
  AgentOptionDto,
  BannerDto,
  ErrorDto,
  PresetApplyReq,
  ProjectToolDto,
  ProjectToolId,
  ProjectToolsDto,
  RulesStateDto,
  SectionDto,
  StagesSetDto,
  StagesSetReq,
  ToolPathBase,
  WorkflowDto,
} from "./contract";
import { ToolSettingsPanel, useToolSettings, type ToolSettings } from "./project-tools-settings";
import { ProjectWikiPanel } from "./project-wiki";
import { ImpactMapActions } from "./project-impact-map";
import { toErrorDto, type Transport } from "./transport/types";

type PageLoad = { status: "loading" } | { status: "ready"; value: WorkflowDto } | { status: "error"; error: ErrorDto };

/** The save waiting for the one-time upgrade approval of a v0.4.0 config. */
type Pending = { kind: "stages"; req: StagesSetReq } | { kind: "preset"; req: PresetApplyReq };

/** A short line under the banner after an action; errors keep their code and hint. */
type Status = { level: "info" | "error"; text: string; hint?: string | null } | null;

function optionLabel(option: AgentOptionDto): string {
  if (option.installed === false) return `${option.name} (not installed)`;
  if (option.sign_in === "signed-out") return `${option.name} (signed out)`;
  return option.name;
}

function WorkflowBanner({ banner }: { banner: BannerDto }) {
  return (
    <div className={`workflow-banner banner-${banner.kind}`} role="status" aria-label="Workflow status" data-kind={banner.kind}>
      <p>{banner.message}</p>
      {banner.kind === "not-signed-in" ? (
        <ul>
          {banner.agents.map((agent) => (
            <li key={agent.id}>
              {agent.name}: <code>{agent.login_hint.text}</code>
              {agent.login_hint.is_command ? (
                <button type="button" onClick={() => void navigator.clipboard?.writeText(agent.login_hint.text)}>
                  Copy
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function RulesTile({ rules, onSync, busy }: { rules: RulesStateDto; onSync: () => void; busy: boolean }) {
  if (rules.synced) {
    return (
      <section className="rules-tile rules-synced" aria-label="Rules">
        <span>Rules: in sync</span>
      </section>
    );
  }
  return (
    <section className="rules-tile rules-not-synced" aria-label="Rules" role="alert">
      <strong>Rules: not synced</strong>
      {rules.error ? (
        <p>
          {rules.error.code}: {rules.error.message}
          {rules.error.hint ? <span className="muted"> {rules.error.hint}</span> : null}
        </p>
      ) : (
        <ul>
          {rules.drift.map((line) => (
            <li key={`${line.path}:${line.message}`}>
              {line.code}: {line.message}
            </li>
          ))}
        </ul>
      )}
      <button type="button" onClick={onSync} disabled={busy}>
        Sync now
      </button>
    </section>
  );
}

function SectionCard({
  section,
  sections,
  agents,
  busy,
  onAssign,
  onIndependence,
}: {
  section: SectionDto;
  sections: SectionDto[];
  agents: AgentOptionDto[];
  busy: boolean;
  onAssign: (section: string, agent: AgentId) => void;
  onIndependence: (section: string, of: string[]) => void;
}) {
  const others = sections.filter((other) => other.id !== section.id);
  const known = section.agent === null || agents.some((option) => option.id === section.agent);
  return (
    <article className="section-card" aria-label={section.label} data-section={section.id}>
      <h2>{section.label}</h2>
      <label>
        <span>Agent</span>
        <select
          value={section.agent ?? ""}
          disabled={busy}
          onChange={(event) => onAssign(section.id, event.target.value as AgentId)}
        >
          {section.agent === null ? (
            <option value="" disabled>
              Not assigned
            </option>
          ) : null}
          {known ? null : <option value={section.agent ?? ""}>{section.agent}</option>}
          {agents.map((option) => (
            <option key={option.id} value={option.id} disabled={option.disabled_reason !== null && option.id !== section.agent}>
              {optionLabel(option)}
            </option>
          ))}
        </select>
      </label>
      <p className="muted">From: {section.source}</p>
      {others.length > 0 ? (
        <fieldset className="independence">
          <legend>Use a different agent than</legend>
          {others.map((other) => {
            const checked = section.independent_of.includes(other.id);
            return (
              <label key={other.id}>
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={busy}
                  onChange={() =>
                    onIndependence(
                      section.id,
                      checked
                        ? section.independent_of.filter((id) => id !== other.id)
                        : [...section.independent_of, other.id],
                    )
                  }
                />
                {other.label}
              </label>
            );
          })}
        </fieldset>
      ) : null}
    </article>
  );
}

function UpgradePrompt({ text, onAccept, onCancel }: { text: string; onAccept: () => void; onCancel: () => void }) {
  return (
    <div className="upgrade-prompt" role="dialog" aria-modal="true" aria-label="Upgrade the project settings">
      <p>{text}</p>
      <div className="upgrade-actions">
        <button type="button" onClick={onAccept}>
          Upgrade and save
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function Assignments({ transport, projectId }: { transport: Transport; projectId: string }) {
  const [page, setPage] = useState<PageLoad>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [status, setStatus] = useState<Status>(null);
  const reads = useRef(0);

  const load = useCallback(async () => {
    const read = ++reads.current;
    setPage({ status: "loading" });
    try {
      const value = await transport.sectionsShow({ project_id: projectId, input: {} });
      if (read !== reads.current) return null;
      setPage({ status: "ready", value });
      return value;
    } catch (error) {
      if (read === reads.current) setPage({ status: "error", error: toErrorDto(error) });
      return null;
    }
  }, [transport, projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const settle = useCallback((result: StagesSetDto) => {
    setPage({ status: "ready", value: result.workflow });
    if (result.synced) {
      setStatus({ level: "info", text: result.upgraded ? "Upgraded the settings and saved." : "Saved." });
    } else {
      const reason = result.sync_error;
      setStatus({
        level: "error",
        text: `Saved, but AGENTS.md is not synced${reason ? `: ${reason.code}: ${reason.message}` : "."}`,
        hint: reason?.hint ?? null,
      });
    }
  }, []);

  const run = useCallback(
    async (next: Pending, approveUpgrade?: string) => {
      setBusy(true);
      setStatus(null);
      try {
        switch (next.kind) {
          case "stages":
            settle(await transport.stagesSet({
              project_id: projectId,
              input: { ...next.req, approve_upgrade: approveUpgrade },
            }));
            break;
          case "preset":
            settle(await transport.presetApply({
              project_id: projectId,
              input: { ...next.req, approve_upgrade: approveUpgrade },
            }));
            break;
        }
      } catch (raw) {
        const error = toErrorDto(raw);
        if (error.code === "OAH_CONFIG_UPGRADE_REQUIRED") {
          const current = await load();
          if (current?.upgrade_warning && current.config_sha256) {
            setPending(next);
          } else if (current) {
            setStatus({ level: "error", text: "The settings changed on disk. Repeat the change." });
          }
        } else if (error.code === "OAH_CONFIG_CHANGED") {
          setStatus({ level: "error", text: "The settings changed on disk. Repeat the change." });
          await load();
        } else {
          setStatus({ level: "error", text: `${error.code}: ${error.message}`, hint: error.hint });
        }
      } finally {
        setBusy(false);
      }
    },
    [transport, projectId, settle, load],
  );

  const syncNow = useCallback(async () => {
    setBusy(true);
    try {
      const result = await transport.rulesSync({ project_id: projectId, input: {} });
      setStatus(
        result.synced
          ? { level: "info", text: "Rules synced." }
          : { level: "error", text: `Rules not synced: ${result.error?.code ?? ""} ${result.error?.message ?? ""}`.trim() },
      );
    } catch (raw) {
      const error = toErrorDto(raw);
      setStatus({ level: "error", text: `${error.code}: ${error.message}`, hint: error.hint });
    } finally {
      await load();
      setBusy(false);
    }
  }, [transport, projectId, load]);

  if (page.status === "loading") return <p className="muted">Loading the team workflow…</p>;
  if (page.status === "error") {
    return (
      <div className="error-box" role="alert">
        <strong>{page.error.code}</strong>
        <p>{page.error.message}</p>
        {page.error.hint ? <p className="muted">{page.error.hint}</p> : null}
        <button type="button" onClick={() => void load()}>
          Retry
        </button>
      </div>
    );
  }
  const view = page.value;
  const controlsDisabled = busy || pending !== null;

  return (
    <div className="team-workflow">
      <WorkflowBanner banner={view.banner} />
      {status ? (
        <p className={status.level === "error" ? "workflow-status status-error" : "workflow-status"} role="status">
          {status.text}
          {status.hint ? <span className="muted"> {status.hint}</span> : null}
        </p>
      ) : null}
      {pending && view.upgrade_warning && view.config_sha256 ? (
        <UpgradePrompt
          text={view.upgrade_warning}
          onAccept={() => {
            setPending(null);
            void run(pending, view.config_sha256 ?? undefined);
          }}
          onCancel={() => setPending(null)}
        />
      ) : null}
      <RulesTile rules={view.rules} onSync={() => void syncNow()} busy={controlsDisabled} />
      {view.notices.length > 0 ? (
        <ul className="notices" aria-label="Workflow notices">
          {view.notices.map((notice) => (
            <li key={notice.id} className={`notice notice-${notice.level}`}>
              <strong>{notice.code}</strong> {notice.message}
              {notice.hint ? <span className="muted"> {notice.hint}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {view.notes.length > 0 ? (
        <ul className="workflow-notes">
          {view.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
      <section className="presets" aria-label="Presets">
        {view.presets.map((preset) => (
          <button
            key={preset.id}
            type="button"
            disabled={controlsDisabled || preset.disabled_reason !== null}
            title={preset.disabled_reason ?? undefined}
            onClick={() => void run({ kind: "preset", req: { preset: preset.id } })}
          >
            {preset.label}
            {preset.disabled_reason ? <span className="muted"> ({preset.disabled_reason})</span> : null}
          </button>
        ))}
      </section>
      <div className="section-cards">
        {view.sections.map((section) => (
          <SectionCard
            key={section.id}
            section={section}
            sections={view.sections}
            agents={view.agents}
            busy={controlsDisabled}
            onAssign={(id, agent) => void run({ kind: "stages", req: { stages: [{ section: id, agent }] } })}
            onIndependence={(id, of) => void run({ kind: "stages", req: { independent_of: [{ section: id, of }] } })}
          />
        ))}
      </div>
      <details className="advanced">
        <summary>Advanced</summary>
        <h3>.oah/config.toml</h3>
        <pre aria-label="Config tables">{view.advanced.config_tables || "(no [sections] or [stages] table yet)"}</pre>
        <h3>AGENTS.md block</h3>
        <pre aria-label="Rules block">{view.advanced.block ?? "(no managed block yet)"}</pre>
      </details>
    </div>
  );
}

/** What the cards hold: the last answer for this project, whether a check runs, and why the last one failed. */
type ToolsLoad = { value: ProjectToolsDto | null; checking: boolean; error: ErrorDto | null };

const WIKI_OPEN_ID = "project-wiki-open";

const TOOL_PATH_BASE: Record<ToolPathBase, string> = {
  project: "in the project",
  "git-common-dir": "in the Git folder",
  "state-dir": "in the OAH state folder",
};

/**
 * One card, top to bottom: the tool's name, what was found, why when it is a problem, the next step, then its
 * settings. Only one card's settings are open at a time, and an open card takes the full row. The Project Wiki
 * card also opens the wiki panel; while that is open no settings are, and the other way round.
 */
function ToolCard({
  tool,
  settings,
  saved,
  wiki,
  actions,
}: {
  tool: ProjectToolDto;
  settings: ToolSettings;
  saved: string | null;
  /** Whether the wiki panel is open, how to open it, and the panel itself on the Project Wiki card. */
  wiki: { open: boolean; show: () => void; panel: ReactNode };
  actions?: ReactNode;
}) {
  const titleId = `project-tool-${tool.id}`;
  const open = settings.editor?.tool === tool.id;
  const wikiHere = tool.id === "wiki" && wiki.open;
  return (
    <article
      className="tool-card"
      aria-labelledby={titleId}
      data-tool={tool.id}
      data-state={tool.state}
      data-editing={open || wikiHere ? "" : undefined}
    >
      <h3 id={titleId}>{tool.name}</h3>
      <p className="tool-status">{tool.status}</p>
      {tool.reason ? <p className="tool-reason">{tool.reason}</p> : null}
      {(!actions || tool.state !== "ready") && <p className="tool-next">
        <strong>Next step:</strong> {tool.next_step}
      </p>}
      {tool.paths.length > 0 ? (
        <ul className="tool-paths">
          {tool.paths.map((path) => (
            <li key={`${path.base}:${path.path}`}>
              {path.label}: <code translate="no">{path.path}</code> <span className="muted">({TOOL_PATH_BASE[path.base]})</span>
            </li>
          ))}
        </ul>
      ) : null}
      {tool.notes.length > 0 ? (
        <ul className="tool-notes">
          {tool.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
      {actions}
      {saved ? <p className="tool-saved">{saved}</p> : null}
      {open ? (
        <ToolSettingsPanel name={tool.name} settings={settings} />
      ) : wikiHere ? (
        wiki.panel
      ) : settings.editor ? (
        <p className="muted tool-settings-wait">Close the open settings to edit these.</p>
      ) : wiki.open ? (
        <p className="muted tool-settings-wait">Close the wiki panel to edit these.</p>
      ) : (
        <div className="tool-actions">
          {tool.id === "wiki" ? (
            <button type="button" id={WIKI_OPEN_ID} onClick={wiki.show}>
              Wiki Setup & Search
            </button>
          ) : null}
          <button type="button" className="tool-edit" id={`project-tool-edit-${tool.id}`} onClick={() => settings.open(tool.id)}>
            Edit {tool.name} Settings
          </button>
        </div>
      )}
    </article>
  );
}

/**
 * The Project Wiki, Impact Map and Jev cards. Reading them changes and starts nothing. A card's settings are read
 * only after its Edit button is pressed, and saved only after a preview of the exact changes is confirmed; a step
 * this app cannot do yet is described in the card's text and has no button. The wiki's folders are read only after
 * its Wiki Setup & Search button is pressed.
 */
function ProjectTools({ transport, projectId }: { transport: Transport; projectId: string }) {
  const [tools, setTools] = useState<ToolsLoad>({ value: null, checking: true, error: null });
  const [announcement, setAnnouncement] = useState("");
  const [saved, setSaved] = useState<{ projectId: string; tool: ProjectToolId; text: string } | null>(null);
  const reads = useRef(0);
  const returnFocus = useRef<{ projectId: string; tool: ProjectToolId } | null>(null);
  /** The project whose wiki panel is open; a panel opened under another project is not shown. */
  const [wikiFor, setWikiFor] = useState<string | null>(null);
  const wikiReturn = useRef<string | null>(null);
  const wikiOpen = wikiFor === projectId;

  const check = useCallback(
    async (announce: boolean) => {
      const read = ++reads.current;
      setTools((previous) => ({ ...previous, checking: true, error: null }));
      try {
        const value = await transport.projectToolsStatus({ project_id: projectId, input: {} });
        if (read !== reads.current) return;
        if (value.project_id !== projectId) {
          // An answer about another project is never shown under this one.
          setTools({
            value: null,
            checking: false,
            error: {
              code: "UI_PROJECT_CHANGED",
              message: "The answer was for another project, so it is not shown.",
              hint: "Choose Check Again.",
            },
          });
          return;
        }
        setTools({ value, checking: false, error: null });
        if (announce) {
          setAnnouncement(`Checked again. ${value.tools.map((tool) => `${tool.name}: ${tool.status}`).join(" ")}`);
        }
      } catch (raw) {
        if (read !== reads.current) return;
        setTools({ value: null, checking: false, error: toErrorDto(raw) });
        setAnnouncement("");
      }
    },
    [transport, projectId],
  );

  useEffect(() => {
    void check(false);
    return () => {
      // An answer that arrives after the project changed or the page closed is dropped.
      reads.current += 1;
    };
  }, [check]);

  const settings = useToolSettings(transport, projectId, (tool, text) => {
    setSaved({ projectId, tool, text });
    setAnnouncement(text);
    void check(false);
  });
  const editing = settings.editor?.tool ?? null;
  useEffect(() => {
    if (editing) {
      returnFocus.current = { projectId, tool: editing };
      setSaved(null);
    } else if (returnFocus.current) {
      // The editor closed: focus goes back to the button that opened it, unless the project changed meanwhile.
      if (returnFocus.current.projectId === projectId) {
        document.getElementById(`project-tool-edit-${returnFocus.current.tool}`)?.focus();
      }
      returnFocus.current = null;
    }
  }, [editing, projectId]);

  useEffect(() => {
    if (wikiOpen) {
      wikiReturn.current = projectId;
      setSaved(null);
      return;
    }
    // A panel left open under another project does not come back with that project.
    if (wikiFor !== null) setWikiFor(null);
    if (wikiReturn.current !== null) {
      // The panel closed: focus goes back to the button that opened it, unless the project changed meanwhile.
      if (wikiReturn.current === projectId) document.getElementById(WIKI_OPEN_ID)?.focus();
      wikiReturn.current = null;
    }
  }, [wikiOpen, wikiFor, projectId]);

  const wiki = {
    open: wikiOpen,
    show: () => setWikiFor(projectId),
    panel: wikiOpen ? (
      <ProjectWikiPanel
        transport={transport}
        projectId={projectId}
        onChanged={() => void check(false)}
        onClose={() => setWikiFor(null)}
      />
    ) : null,
  };

  return (
    <section className="project-tools" aria-labelledby="project-tools-title">
      <div className="project-tools-head">
        <h2 id="project-tools-title">Project Tools</h2>
        {/* aria-disabled keeps keyboard focus on the button while a check runs. */}
        <button
          type="button"
          aria-disabled={tools.checking}
          onClick={() => {
            if (!tools.checking) void check(true);
          }}
        >
          Check Again
        </button>
      </div>
      <p className="muted">
        What this project has for its wiki, impact map and Jev. Checking only reads the project: it changes nothing
        and starts nothing. Settings and wiki files are written only after you review the exact changes.
      </p>
      <p className="visually-hidden" role="status">
        {announcement}
      </p>
      {tools.error ? (
        <div className="error-box" role="alert">
          <strong>The project tools could not be checked.</strong>
          <p>
            {tools.error.code}: {tools.error.message}
            {tools.error.hint ? <span> {tools.error.hint}</span> : null}
          </p>
        </div>
      ) : null}
      {tools.value ? (
        <div className="tool-cards" aria-busy={tools.checking}>
          {tools.value.tools.map((tool) => (
            <ToolCard
              key={tool.id}
              tool={tool}
              settings={settings}
              saved={saved?.projectId === projectId && saved.tool === tool.id ? saved.text : null}
              wiki={wiki}
              actions={tool.id === "impact-map" && ["ready", "map-found", "unknown"].includes(tool.state)
                ? <ImpactMapActions
                    key={projectId}
                    transport={transport}
                    projectId={projectId}
                    onSettled={() => void check(false)}
                  />
                : null}
            />
          ))}
        </div>
      ) : tools.checking ? (
        <p className="muted">Checking the project tools…</p>
      ) : null}
    </section>
  );
}

/** The Team Workflow page of one project: who does which section, then what the project has for its tools. */
export function TeamWorkflow({ transport, projectId }: { transport: Transport; projectId: string }) {
  return (
    <>
      <Assignments transport={transport} projectId={projectId} />
      <ProjectTools transport={transport} projectId={projectId} />
    </>
  );
}
