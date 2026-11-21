// 한국어: 팀 작업 흐름 화면 — 섹션 카드·에이전트 선택·단일 배너·프리셋·업그레이드 확인·규칙 동기 상태·고급 보기. 판단은 모두 core 응답을 그대로 쓴다.
import { useCallback, useEffect, useState } from "react";
import type {
  AgentId,
  AgentOptionDto,
  BannerDto,
  ErrorDto,
  PresetApplyReq,
  RulesStateDto,
  SectionDto,
  StagesSetDto,
  StagesSetReq,
  WorkflowDto,
} from "./contract";
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

/**
 * The Team Workflow page. It reloads `sections_show` when the project or the agent statuses change (`reloadKey`).
 * Every assignment is one `stages_set`; a refused upgrade shows the warning once and retries with its approval.
 */
export function TeamWorkflow({ transport, reloadKey }: { transport: Transport; reloadKey: string }) {
  const [page, setPage] = useState<PageLoad>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [status, setStatus] = useState<Status>(null);

  const load = useCallback(async () => {
    try {
      const value = await transport.sectionsShow();
      setPage({ status: "ready", value });
    } catch (error) {
      setPage({ status: "error", error: toErrorDto(error) });
    }
  }, [transport]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

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
    async (next: Pending) => {
      setBusy(true);
      setStatus(null);
      try {
        settle(next.kind === "stages" ? await transport.stagesSet(next.req) : await transport.presetApply(next.req));
      } catch (raw) {
        const error = toErrorDto(raw);
        if (error.code === "OAH_CONFIG_UPGRADE_REQUIRED") {
          setPending(next);
        } else if (error.code === "OAH_CONFIG_CHANGED") {
          setStatus({ level: "error", text: "The file changed on disk. It was read again; repeat the change.", hint: null });
          await load();
        } else {
          setStatus({ level: "error", text: `${error.code}: ${error.message}`, hint: error.hint });
        }
      } finally {
        setBusy(false);
      }
    },
    [transport, settle, load],
  );

  const syncNow = useCallback(async () => {
    setBusy(true);
    try {
      const result = await transport.rulesSync();
      setStatus(
        result.synced
          ? { level: "info", text: "Rules synced." }
          : { level: "error", text: `Rules not synced: ${result.error?.code ?? ""} ${result.error?.message ?? ""}`.trim() },
      );
    } catch (raw) {
      const error = toErrorDto(raw);
      setStatus({ level: "error", text: `${error.code}: ${error.message}`, hint: error.hint });
    } finally {
      setBusy(false);
      await load();
    }
  }, [transport, load]);

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
  const approve = (next: Pending): Pending =>
    view.config_sha256 === null ? next : { ...next, req: { ...next.req, approve_upgrade: view.config_sha256 } } as Pending;

  return (
    <div className="team-workflow">
      <WorkflowBanner banner={view.banner} />
      {status ? (
        <p className={status.level === "error" ? "workflow-status status-error" : "workflow-status"} role="status">
          {status.text}
          {status.hint ? <span className="muted"> {status.hint}</span> : null}
        </p>
      ) : null}
      {pending && view.upgrade_warning ? (
        <UpgradePrompt
          text={view.upgrade_warning}
          onAccept={() => {
            const next = approve(pending);
            setPending(null);
            void run(next);
          }}
          onCancel={() => setPending(null)}
        />
      ) : null}
      <RulesTile rules={view.rules} onSync={() => void syncNow()} busy={busy} />
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
            disabled={busy || preset.disabled_reason !== null}
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
            busy={busy}
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
