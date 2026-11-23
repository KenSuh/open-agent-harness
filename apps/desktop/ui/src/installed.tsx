import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { ErrorDto, InstalledSkillDto, ReviewResultDto } from "./contract";
import { isInstallable, needsAcknowledgement, Reason, SourceFacts, WarningList } from "./explore";
import { reviewOnce } from "./reviewOnce";
import { toErrorDto, type Transport } from "./transport/types";
import type { Load } from "./useController";
import { writeKey, type SkillWrites } from "./useApply";

type Check =
  | { phase: "idle" }
  | { phase: "checking" }
  | { phase: "result"; result: ReviewResultDto }
  | { phase: "failed"; error: ErrorDto };

function ErrorLines({ error }: { error: ErrorDto }) {
  return (
    <div className="error-box" role="alert">
      <strong>{error.code}</strong>
      <p>{error.message}</p>
      {error.hint ? <p className="muted">{error.hint}</p> : null}
    </div>
  );
}

/** Explicit update check for a managed project row; nothing is written until the apply click. */
function ManagedUpdate({
  skill,
  transport,
  projectId,
  writes,
  nameId,
}: {
  skill: InstalledSkillDto;
  transport: Transport;
  projectId: string;
  writes: SkillWrites;
  nameId: string;
}) {
  const managed = skill.managed!;
  const [check, setCheck] = useState<Check>({ phase: "idle" });
  // null = not edited since the check; the one editable acknowledgement value (see `shownAck`).
  const [edited, setEdited] = useState<boolean | null>(null);
  const checking = useRef(false);
  const alive = useRef(true);
  const project = useRef(projectId);
  project.current = projectId;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const key = writeKey(projectId, skill.engine, skill.name);
  const entry = writes.entries[key];
  const { clear } = writes;

  const runCheck = () => {
    if (checking.current) return;
    checking.current = true;
    const started = project.current;
    setEdited(null);
    clear(key); // a fresh check supersedes the old apply error and outcome
    setCheck({ phase: "checking" });
    reviewOnce(transport, managed.repo, managed.path).then(
      (result) => {
        checking.current = false;
        if (alive.current && project.current === started) setCheck({ phase: "result", result });
      },
      (error) => {
        checking.current = false;
        if (alive.current && project.current === started) setCheck({ phase: "failed", error: toErrorDto(error) });
      },
    );
  };

  const result = check.phase === "result" ? check.result : null;
  const same = result !== null && result.record.id === managed.review_id;
  const blocked = result !== null && !same && !isInstallable(result);
  // The fresh review's own compatible list decides whether this row's engine can take it.
  const incompatible =
    result !== null && !same && !blocked && result.gate.compatible !== undefined && !result.gate.compatible.includes(skill.engine);
  const available = result !== null && !same && !blocked && !incompatible;
  const ack = result !== null && needsAcknowledgement(result);
  // The outcome belongs to the displayed candidate only.
  const mine =
    result !== null &&
    entry !== undefined &&
    entry.confirmation.operation === "update" &&
    entry.confirmation.review_id === result.record.id &&
    entry.confirmation.expected_review_id === managed.review_id
      ? entry
      : undefined;
  const applying = entry?.phase === "applying";
  const done = mine?.phase === "done";
  // After the inventory re-read the check is gone; the finished write still explains the new row.
  const settled =
    entry?.phase === "done" &&
    entry.confirmation.operation === "update" &&
    (entry.confirmation.review_id === managed.review_id || entry.confirmation.expected_review_id === managed.review_id)
      ? entry
      : undefined;
  const outcome = mine?.phase === "done" ? mine : settled;
  // A failure stays visible for its candidate, or after a re-read dropped the check (never for a different candidate).
  // One value drives the checkbox, the button and the request; only an attempt in flight or finished freezes it.
  const shownAck = mine && mine.phase !== "failed" ? mine.confirmation.acknowledge_warnings : (edited ?? (mine ? mine.confirmation.acknowledge_warnings : false));
  const failure =
    entry?.phase === "failed" && entry.confirmation.expected_review_id === managed.review_id && (result === null || mine === entry)
      ? entry
      : undefined;
  return (
    <div className="managed-update" role="group" aria-labelledby={nameId}>
      <button type="button" aria-describedby={nameId} onClick={runCheck} disabled={check.phase === "checking" || applying}>
        {check.phase === "checking" ? "Checking…" : "Check for update"}
      </button>
      {check.phase === "failed" ? <ErrorLines error={check.error} /> : null}
      {result && blocked ? (
        <div className="review-result quarantine" aria-label="Update blocked">
          <p>Blocked: the newest review of this skill cannot be applied. The installed copy is unchanged.</p>
          <ul className="gate-reasons">
            {(result.gate.reasons ?? []).map((reason, index) => (
              <Reason key={`${index}:${reason.kind}`} reason={reason} />
            ))}
          </ul>
        </div>
      ) : null}
      {result && incompatible ? (
        <div className="review-result" aria-label="Update not compatible">
          <p>
            This update cannot be applied: the new review is not compatible with {skill.engine === "claude" ? "Claude Code" : "Codex"}, the
            agent this copy belongs to. The installed copy is unchanged.
          </p>
          <dl className="agent-facts">
            <SourceFacts source={result.record.source} />
          </dl>
        </div>
      ) : null}
      {result && available ? (
        <div className="review-result" aria-label="Update available">
          <p>Update available. New review {result.gate.result}.</p>
          <dl className="agent-facts">
            <SourceFacts source={result.record.source} />
          </dl>
          {ack ? (
            <>
              <WarningList result={result} />
              <label>
                <input
                  type="checkbox"
                  checked={shownAck}
                  disabled={applying || done}
                  aria-describedby={nameId}
                  onChange={(event) => setEdited(event.target.checked)}
                />{" "}
                I have read these warnings.
              </label>
            </>
          ) : null}
          <button
            type="button"
            aria-describedby={nameId}
            disabled={applying || done || (ack && !shownAck)}
            onClick={() =>
              void writes.apply({
                project: projectId,
                generation: writes.generation,
                engine: skill.engine,
                name: skill.name,
                operation: "update",
                review_id: result.record.id,
                expected_review_id: managed.review_id,
                acknowledge_warnings: ack && shownAck,
                destination: skill.location,
              })
            }
          >
            {applying ? "Applying…" : "Apply update"}
          </button>
        </div>
      ) : null}
      {failure ? <ErrorLines error={failure.error} /> : null}
      <div className="apply-status" role="status" aria-live="polite" aria-atomic="true" aria-label={`Update status for ${skill.name}`}>
        {check.phase === "checking" ? <p>Checking {skill.name} for updates…</p> : null}
        {result && blocked ? <p>Update blocked. The installed copy is unchanged.</p> : null}
        {result && incompatible ? (
          <p>Update not compatible with {skill.engine === "claude" ? "Claude Code" : "Codex"}. The installed copy is unchanged.</p>
        ) : null}
        {result && available ? <p>Update available. New review {result.gate.result}.</p> : null}
        {same ? <p>Up to date. The current review matches the installed one.</p> : null}
        {applying ? <p>Applying the update to {skill.name}…</p> : null}
        {outcome ? <p>{outcome.result.status === "unchanged" ? "Already current; nothing changed." : "Updated."}</p> : null}
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
  projectOpen,
  projectId = null,
  writes,
  inventoryRevision,
}: {
  transport: Transport;
  projectOpen: boolean;
  projectId?: string | null;
  writes: SkillWrites;
  /** Bumped by the controller after a confirmed install or update; a bump re-reads the real inventory. */
  inventoryRevision: number;
}) {
  const [load, setLoad] = useState<Load<InstalledSkillDto[]>>({ status: "loading" });
  const [read, setRead] = useState(0);
  // A re-read keeps the rows (and their live status regions) mounted; only the first read replaces the view.
  const [reading, setReading] = useState(false);

  useEffect(() => {
    let current = true;
    setReading(true);
    setLoad((previous) => (previous.status === "ready" || previous.status === "loading" ? previous : { status: "loading" }));
    transport.skillsInstalled().then(
      (value) => {
        if (!current) return;
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
  }, [transport, read, inventoryRevision]);

  const refresh = () => setRead((previous) => previous + 1);

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
      {load.status === "ready" && (load.value.length === 0 ? (
        <p className="empty" role="status">
          {projectOpen ? "No skills installed in this project or your user folders." : "No skills installed in your user folders."}
        </p>
      ) : (
        <ul className="installed-list" aria-label="Installed skills">
          {load.value.map((skill, index) => (
            <InstalledRow key={`${skill.scope}:${skill.location}:${index}`} skill={skill}>
              {(nameId) => (
                <>
              <div className="installed-heading">
                <strong id={nameId}>{skill.name}</strong>
                <span className="muted">
                  {skill.engine === "claude" ? "Claude Code" : "Codex"} · {skill.scope === "project" ? "Project" : "User"}
                </span>
              </div>
              <code className="muted">{skill.location}</code>
              {skill.scope === "project" && skill.managed && projectId ? (
                <ManagedUpdate skill={skill} transport={transport} projectId={projectId} writes={writes} nameId={nameId} />
              ) : null}
                </>
              )}
            </InstalledRow>
          ))}
        </ul>
      ))}
    </div>
  );
}
