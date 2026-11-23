import { useEffect, useRef, useState } from "react";
import type { ApplyProjectSkillDto, ErrorDto, InstalledSkillDto, ReviewResultDto } from "./contract";
import { isInstallable, needsAcknowledgement, Reason, WarningList } from "./explore";
import { reviewOnce } from "./reviewOnce";
import { toErrorDto, type Transport } from "./transport/types";
import type { Load } from "./useController";
import { useApply } from "./useApply";

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
  onApplied,
}: {
  skill: InstalledSkillDto;
  transport: Transport;
  projectId: string;
  onApplied: (skill: InstalledSkillDto, result: ApplyProjectSkillDto) => void;
}) {
  const managed = skill.managed!;
  const [check, setCheck] = useState<Check>({ phase: "idle" });
  const [acknowledged, setAcknowledged] = useState(false);
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
  const { state, apply } = useApply(transport, projectId, (result) => onApplied(skill, result));

  const runCheck = () => {
    if (checking.current) return;
    checking.current = true;
    const started = project.current;
    setAcknowledged(false);
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
  const ack = result !== null && needsAcknowledgement(result);
  const applying = state.phase === "applying";
  const done = state.phase === "done";
  return (
    <div className="managed-update">
      <button type="button" onClick={runCheck} disabled={check.phase === "checking" || applying}>
        {check.phase === "checking" ? "Checking…" : "Check for update"}
      </button>
      {check.phase === "failed" ? <ErrorLines error={check.error} /> : null}
      {same ? <p role="status">Up to date. The current review matches the installed one.</p> : null}
      {result && blocked ? (
        <div className="review-result quarantine" role="status" aria-label="Update blocked">
          <p>Blocked: the newest review of this skill cannot be applied. The installed copy is unchanged.</p>
          <ul className="gate-reasons">
            {(result.gate.reasons ?? []).map((reason, index) => (
              <Reason key={`${index}:${reason.kind}`} reason={reason} />
            ))}
          </ul>
        </div>
      ) : null}
      {result && !same && !blocked ? (
        <div className="review-result" aria-label="Update available">
          <p>
            Update available. New review {result.gate.result}
            {result.record.source.commit ? <> at <code>{result.record.source.commit}</code></> : null}.
          </p>
          {ack ? (
            <>
              <WarningList result={result} />
              <label>
                <input
                  type="checkbox"
                  checked={acknowledged}
                  disabled={applying || done}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                />{" "}
                I have read these warnings.
              </label>
            </>
          ) : null}
          <button
            type="button"
            disabled={applying || done || (ack && !acknowledged)}
            onClick={() =>
              void apply({
                engine: skill.engine,
                review_id: result.record.id,
                expected_review_id: managed.review_id,
                acknowledge_warnings: ack && acknowledged,
              })
            }
          >
            {applying ? "Applying…" : "Apply update"}
          </button>
        </div>
      ) : null}
      {state.phase === "failed" ? <ErrorLines error={state.error} /> : null}
      {state.phase === "done" ? (
        <p role="status">{state.result.status === "unchanged" ? "Already current; nothing changed." : "Updated."}</p>
      ) : null}
    </div>
  );
}

export function InstalledView({
  transport,
  projectOpen,
  projectId = null,
  onNotify,
}: {
  transport: Transport;
  projectOpen: boolean;
  projectId?: string | null;
  onNotify?: (text: string) => void;
}) {
  const [load, setLoad] = useState<Load<InstalledSkillDto[]>>({ status: "loading" });
  const [read, setRead] = useState(0);

  useEffect(() => {
    let current = true;
    transport.skillsInstalled().then(
      (value) => {
        if (current) setLoad({ status: "ready", value });
      },
      (error) => {
        if (current) setLoad({ status: "error", error: toErrorDto(error) });
      },
    );
    return () => {
      current = false;
    };
  }, [transport, read]);

  const refresh = () => {
    setLoad({ status: "loading" });
    setRead((previous) => previous + 1);
  };

  const applied = (skill: InstalledSkillDto, result: ApplyProjectSkillDto) => {
    onNotify?.(result.status === "unchanged" ? `${skill.name} is already current.` : `Updated ${skill.name}.`);
    refresh(); // re-read the real inventory after a confirmed success
  };

  return (
    <div className="installed">
      <div className="installed-controls">
        <p className="muted">
          {projectOpen
            ? "Showing skills in this project and your user folders."
            : "Showing user skills. Open a project to see its skills."}
        </p>
        <button type="button" onClick={refresh} disabled={load.status === "loading"}>
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
      {load.status === "loading" ? <p className="muted" role="status">Reading installed skills…</p> : null}
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
            <li className="agent-card" key={`${skill.scope}:${skill.location}:${index}`}>
              <div className="installed-heading">
                <strong>{skill.name}</strong>
                <span className="muted">
                  {skill.engine === "claude" ? "Claude Code" : "Codex"} · {skill.scope === "project" ? "Project" : "User"}
                </span>
              </div>
              <code className="muted">{skill.location}</code>
              {skill.scope === "project" && skill.managed && projectId ? (
                <ManagedUpdate skill={skill} transport={transport} projectId={projectId} onApplied={applied} />
              ) : null}
            </li>
          ))}
        </ul>
      ))}
    </div>
  );
}
