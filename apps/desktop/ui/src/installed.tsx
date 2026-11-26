import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { AgentId, AgentStatusDto, ErrorDto, InstalledSkillDto, ReviewResultDto } from "./contract";
import { isManagedProjectSkill, judgeUpdate, updateBasis, writeKey, type ManagedProjectSkill, type UpdateBasis, type UpdateVerdict } from "./confirmation";
import { HEADLINE, Reason, SourceFacts, WarningList } from "./explore";
import { InstalledSkillActions } from "./installedManagement";
import { reviewOnce } from "./reviewOnce";
import { toErrorDto, type Transport } from "./transport/types";
import type { Load } from "./useController";
import type { Operation, SkillConfirmations } from "./useApply";

/** A check belongs to the exact basis (selection, folder and receipt revision) that started it. */
type Check =
  | { readonly phase: "idle" }
  | { readonly phase: "checking"; readonly basis: UpdateBasis; readonly token: number }
  | { readonly phase: "result"; readonly basis: UpdateBasis; readonly result: ReviewResultDto }
  | { readonly phase: "failed"; readonly basis: UpdateBasis; readonly error: ErrorDto };

const IDLE: Check = { phase: "idle" };

const AGENT_LABELS: Record<AgentId, string> = {
  claude: "Claude Code",
  codex: "Codex",
  gemini: "Gemini CLI",
  copilot: "Copilot CLI",
  opencode: "OpenCode",
  antigravity: "Antigravity",
  cursor: "Cursor",
};
const AGENT_FILTERS = Object.entries(AGENT_LABELS) as [AgentId, string][];

function ErrorLines({ error }: { error: ErrorDto }) {
  return (
    <div className="error-box" role="alert">
      <strong>{error.code}</strong>
      <p>{error.message}</p>
      {error.hint ? <p className="muted">{error.hint}</p> : null}
    </div>
  );
}

function headline(result: ReviewResultDto): string {
  return HEADLINE[result.gate.result] ?? `Gate result: ${String(result.gate.result)}`;
}

/** The single current status line: a write in progress or finished owns it ahead of any check. */
function updateAnnouncement(
  skill: InstalledSkillDto,
  check: Check,
  verdict: UpdateVerdict | null,
  update: Operation | undefined,
): string | null {
  if (update?.phase === "applying") return `Applying the update to ${skill.name}…`;
  if (update?.phase === "done") return update.result.status === "unchanged" ? "Already current; nothing changed." : "Updated.";
  if (check.phase === "checking") return `Checking ${skill.name} for updates…`;
  if (check.phase !== "result" || verdict === null) return null;
  switch (verdict.kind) {
    case "current":
      return "Up to date. The current review matches the installed one.";
    case "mismatch":
      return "Update not applicable. The review does not match the installed copy. The installed copy is unchanged.";
    case "blocked":
      return "Update blocked. The installed copy is unchanged.";
    case "incompatible":
      return `Update not compatible with ${AGENT_LABELS[skill.engine]}. The installed copy is unchanged.`;
    case "reviewed":
      return `New revision reviewed. ${headline(check.result)}.`;
  }
}

/** Explicit update check for a managed project row; nothing is written until the apply click. */
function ManagedUpdate({
  skill,
  project,
  transport,
  confirmations,
  nameId,
}: {
  skill: ManagedProjectSkill;
  project: string;
  transport: Transport;
  confirmations: SkillConfirmations;
  nameId: string;
}) {
  const [check, setCheck] = useState<Check>(IDLE);
  const running = useRef<string | null>(null); // basis key of the check in flight
  const tokens = useRef(0);
  const { generation } = confirmations.selection;
  const destination = writeKey(project, skill.engine, skill.name);
  const basis = updateBasis({ project, generation }, skill, confirmations.acquired[destination]);
  // A changed selection or receipt leaves the check behind: it shows nothing and confirms nothing.
  const current: Check = check.phase !== "idle" && basis !== null && check.basis.key === basis.key ? check : IDLE;
  const result = current.phase === "result" ? current.result : null;
  const verdict = current.phase === "result" ? judgeUpdate(current.basis, current.result) : null;
  const confirmation = verdict?.kind === "reviewed" ? verdict.confirmation : null;
  const operation = confirmations.operations[destination];
  const update = operation?.confirmation.operation === "update" ? operation : undefined;
  const busy = operation?.phase === "applying";
  const mine = confirmation !== null && update?.confirmation.key === confirmation.key ? update : undefined;
  const done = mine?.phase === "done";
  const failure = update?.phase === "failed" && (mine === update || current.phase === "idle") ? update : undefined;
  const acknowledged = confirmation !== null && confirmations.acknowledged(confirmation);
  const announcement = updateAnnouncement(skill, current, verdict, update);

  const runCheck = () => {
    if (basis === null || running.current === basis.key) return;
    const started = basis;
    const token = ++tokens.current;
    running.current = started.key;
    confirmations.clear(started.destination); // a fresh check supersedes the old apply error and outcome
    setCheck({ phase: "checking", basis: started, token });
    const settle = (next: Check) => {
      if (running.current === started.key) running.current = null;
      setCheck((previous) => (previous.phase === "checking" && previous.token === token ? next : previous));
    };
    // The source is the receipt's own repository and folder, fixed when the check starts.
    reviewOnce(transport, started.acquired.acquisition.repo, started.acquired.acquisition.path).then(
      (value) => settle({ phase: "result", basis: started, result: value }),
      (error) => settle({ phase: "failed", basis: started, error: toErrorDto(error) }),
    );
  };

  return (
    <div className="managed-update" role="group" aria-labelledby={nameId}>
      <button type="button" aria-describedby={nameId} onClick={runCheck} disabled={basis === null || current.phase === "checking" || busy}>
        {current.phase === "checking" ? "Checking…" : "Check for update"}
      </button>
      {current.phase === "failed" ? <ErrorLines error={current.error} /> : null}
      {result && verdict?.kind === "blocked" ? (
        <div className="review-result quarantine" aria-label="Update blocked">
          <p>Blocked: the newest review of this skill cannot be applied. The installed copy is unchanged.</p>
          <ul className="gate-reasons">
            {(result.gate.reasons ?? []).map((reason, index) => (
              <Reason key={`${index}:${reason.kind}`} reason={reason} />
            ))}
          </ul>
        </div>
      ) : null}
      {result && verdict?.kind === "mismatch" ? (
        <div className="review-result" aria-label="Update not applicable">
          <p>
            This review cannot update the installed copy: it names a different skill, repository or folder than the one installed.
            The installed copy is unchanged.
          </p>
          <dl className="agent-facts">
            <SourceFacts source={result.record.source} />
          </dl>
        </div>
      ) : null}
      {result && verdict?.kind === "incompatible" ? (
        <div className="review-result" aria-label="Update not compatible">
          <p>
            This update cannot be applied: the new review is not compatible with {AGENT_LABELS[skill.engine]}, the agent this copy
            belongs to. The installed copy is unchanged.
          </p>
          <dl className="agent-facts">
            <SourceFacts source={result.record.source} />
          </dl>
        </div>
      ) : null}
      {result && confirmation ? (
        <div className="review-result" aria-label="New revision reviewed">
          <p>New revision reviewed. {headline(result)}.</p>
          <dl className="agent-facts">
            <SourceFacts source={result.record.source} />
          </dl>
          {confirmation.warnings ? (
            <>
              <WarningList result={result} />
              <label>
                <input
                  type="checkbox"
                  checked={acknowledged}
                  disabled={busy || done}
                  aria-describedby={nameId}
                  onChange={(event) => confirmations.acknowledge(confirmation, event.target.checked)}
                />{" "}
                I have read these warnings.
              </label>
            </>
          ) : null}
          <button
            type="button"
            aria-describedby={nameId}
            disabled={busy || done || (confirmation.warnings && !acknowledged)}
            onClick={() => void confirmations.apply(confirmation)}
          >
            {busy ? "Applying…" : "Apply update"}
          </button>
        </div>
      ) : null}
      {failure ? <ErrorLines error={failure.error} /> : null}
      <div className="apply-status" role="status" aria-live="polite" aria-atomic="true" aria-label={`Update status for ${skill.name}`}>
        {announcement ? <p>{announcement}</p> : null}
      </div>
    </div>
  );
}

function InstalledRow({ skill, children }: { skill: InstalledSkillDto; children: (nameId: string) => ReactNode }) {
  const nameId = useId();
  return (
    <li className="agent-card" data-skill={skill.name}>
      {children(nameId)}
    </li>
  );
}

export function InstalledView({
  transport,
  confirmations,
  agents,
  projectRevision,
  inventoryRevision,
}: {
  transport: Transport;
  confirmations: SkillConfirmations;
  agents: readonly AgentStatusDto[];
  projectRevision: number;
  /** Bumped by the controller after a confirmed install or update; a bump re-reads the real inventory. */
  inventoryRevision: number;
}) {
  const [load, setLoad] = useState<Load<InstalledSkillDto[]>>({ status: "loading" });
  const [read, setRead] = useState(0);
  const [filter, setFilter] = useState<AgentId | "all">("all");
  const [managementNotice, setManagementNotice] = useState<{ generation: number; text: string } | null>(null);
  // A re-read keeps the rows (and their live status regions) mounted; only the first read replaces the view.
  const [reading, setReading] = useState(false);
  const { observe } = confirmations;
  const { project, generation } = confirmations.selection;
  const projectOpen = project !== null;

  useEffect(() => {
    let current = true;
    setReading(true);
    setLoad((previous) => (previous.status === "ready" || previous.status === "loading" ? previous : { status: "loading" }));
    transport.skillsInstalled().then(
      (value) => {
        if (!current) return;
        // The owner sees the receipts first, so a confirmation built on a replaced one is void before this read renders.
        if (project !== null) observe(project, generation, value);
        setReading(false);
        setLoad({ status: "ready", value });
      },
      (error) => {
        if (!current) return;
        setReading(false);
        setLoad({ status: "error", error: toErrorDto(error) });
      },
    );
    return () => {
      current = false;
    };
  }, [transport, read, projectRevision, inventoryRevision, observe, project, generation]);

  const refresh = () => setRead((previous) => previous + 1);
  const inventory = load.status === "ready" ? load.value : [];
  const agentFilters = AGENT_FILTERS.flatMap(([id, label]) => {
    const rows = inventory.filter((skill) => skill.engine === id || skill.compatible_agents?.includes(id));
    return rows.length > 0 || agents.some((agent) => agent.id === id && agent.installed) ? [{ id, label, rows }] : [];
  });
  const all = { id: "all", label: "All", rows: inventory } as const;
  const selected = agentFilters.find(({ id }) => id === filter) ?? all;
  const visibleRows = selected.id === "all" ? selected.rows : [
    ...selected.rows.filter((skill) => skill.engine === selected.id),
    ...selected.rows.filter((skill) => skill.engine !== selected.id),
  ];
  if (load.status === "ready" && selected.id !== filter) setFilter(selected.id);
  const scope = projectOpen ? "this project or your user folders" : "your user folders";

  return (
    <div className="installed">
      <div className="installed-controls">
        <p className="muted">
          {projectOpen
            ? "Showing skills in this project and your user folders."
            : "Showing user skills. Open a project to see its skills."}
        </p>
        <button type="button" onClick={refresh} disabled={load.status === "loading" || reading}>
          Refresh inventory
        </button>
      </div>
      {managementNotice?.generation === generation ? <p className="muted" role="status">{managementNotice.text}</p> : null}
      <details className="agent-card installed-help">
        <summary>Manage skills with the source CLI</summary>
        <p>
          Get the full Python CLI with <code>install.sh</code> (macOS) or <code>install.ps1</code> (Windows) from the OAH release.
          Run the commands below from your project folder. Updates apply to OAH toolkit copies.
        </p>
        <dl className="agent-facts">
          <dt>Install in this project</dt>
          <dd><code>oah skills install NAME</code></dd>
          <dt>Install for your user</dt>
          <dd><code>oah skills install NAME --global</code></dd>
          <dt>Update project copies</dt>
          <dd><code>oah skills update --project</code></dd>
          <dt>Update user copies</dt>
          <dd><code>oah skills update --global</code></dd>
        </dl>
        <p>
          If <code>oah</code> runs the desktop helper, use <code>uv tool run --from "/path/to/open-agent-harness" oah</code>
          {" "}in place of <code>oah</code>. Use the absolute path to your OAH source checkout or unpacked source archive.
        </p>
      </details>
      {load.status === "loading" || (reading && load.status === "ready") ? <p className="muted" role="status">Reading installed skills…</p> : null}
      {load.status === "error" ? (
        <div className="error-box" role="alert">
          <strong>{load.error.code}</strong>
          <p>{load.error.message}</p>
          {load.error.hint ? <p>{load.error.hint}</p> : null}
        </div>
      ) : null}
      {load.status === "ready" ? (
        <>
          <div className="installed-filters" role="group" aria-label="Filter installed skills by agent">
            {[all, ...agentFilters].map(({ id, label, rows }) => (
              <button key={id} type="button" aria-pressed={selected.id === id} onClick={() => setFilter(id)}>
                <span translate="no">{label}</span> {rows.length.toLocaleString()}
              </button>
            ))}
          </div>
          {selected.rows.length === 0 ? (
            <p className="empty" role="status">
              {selected.id === "all" ? `No skills installed in ${scope}.` : `No ${selected.label} skills found in ${scope}.`}
            </p>
          ) : (
            <ul className="installed-list" aria-label="Installed skills">
              {visibleRows.map((skill) => (
                <InstalledRow key={JSON.stringify([skill.scope, skill.engine, skill.location])} skill={skill}>
                  {(nameId) => (
                    <>
                      <div className="installed-heading">
                        <strong id={nameId}>{skill.name}</strong>
                        <span className="muted">
                          {selected.id !== "all" && selected.id !== skill.engine ? "Shared from " : ""}
                          {AGENT_LABELS[skill.engine]} · {skill.scope === "project" ? "Project" : "User"}
                        </span>
                      </div>
                      <code className="muted">{skill.location}</code>
                      {project !== null && (selected.id === "all" || selected.id === skill.engine) && isManagedProjectSkill(skill) ? (
                        <ManagedUpdate skill={skill} project={project} transport={transport} confirmations={confirmations} nameId={nameId} />
                      ) : null}
                      <InstalledSkillActions key={generation} target={{ project, location: skill.location }}
                        name={skill.name} transport={transport} labels={AGENT_LABELS}
                        onRegistered={(source) => {
                          setManagementNotice({ generation, text: source.updates_in_oah
                            ? `Git source registered for ${skill.name}. You can now check for updates.`
                            : `Git source registered for ${skill.name}.` });
                          refresh();
                        }}
                        onRemoved={(archive) => {
                          setManagementNotice({ generation, text: `Removed ${skill.name}. Recover it from ${archive}.` });
                          refresh();
                        }} />
                    </>
                  )}
                </InstalledRow>
              ))}
            </ul>
          )}
        </>
      ) : null}
    </div>
  );
}
