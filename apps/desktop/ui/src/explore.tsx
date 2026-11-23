// 한국어: Explore 화면 — GitHub 폴더 직접 입력 검토 폼과 결과(고정 커밋·라이선스·게이트 사유). 검토 데이터는 전부 텍스트로만 그린다.
import { useId, useState, type FormEvent } from "react";
import type {
  AgentStatusDto,
  GateReasonDto,
  ProjectDto,
  ProjectSkillEngine,
  ReviewResultDto,
} from "./contract";
import { writeKey, type SkillWrites, type WriteEntry } from "./useApply";
import { parseTarget, type useReview } from "./useReview";

type Review = ReturnType<typeof useReview>;

function lineText(lines: number[]): string {
  if (lines.length === 0) return "";
  return `SKILL.md ${lines.length === 1 ? "line" : "lines"} ${lines.join(", ")}`;
}

export function Reason({ reason }: { reason: GateReasonDto }) {
  const lines = lineText(reason.lines ?? []);
  return (
    <li className="gate-reason" data-kind={reason.kind}>
      <strong>{reason.kind}</strong> <span>{reason.detail}</span>
      {lines ? <span className="muted"> ({lines})</span> : null}
    </li>
  );
}

/** Repository, nested folder and pinned commit shown together as rows of an enclosing `dl`. */
export function SourceFacts({ source }: { source: ReviewResultDto["record"]["source"] }) {
  return (
    <>
      <dt>GitHub repository</dt>
      <dd>{source.locator}</dd>
      <dt>Folder in repository</dt>
      <dd>
        <code>{source.path}</code>
      </dd>
      <dt>Pinned commit</dt>
      <dd>{source.commit ? <code>{source.commit}</code> : "Not pinned"}</dd>
    </>
  );
}

const HEADLINE = {
  pass: "Review passed",
  warn: "Review passed with warnings",
  block: "Quarantined: the gate blocked this skill",
} as const;

function ReviewResult({ result }: { result: ReviewResultDto }) {
  const { record, gate } = result;
  const quarantined = gate.result === "block";
  const reasons = gate.reasons ?? [];
  return (
    <section
      className={quarantined ? "review-result quarantine" : "review-result"}
      aria-label={quarantined ? "Quarantine" : "Review result"}
      data-result={gate.result}
      data-quarantined={quarantined ? "true" : "false"}
    >
      <h2>{HEADLINE[gate.result] ?? `Gate result: ${String(gate.result)}`}</h2>
      {quarantined ? <p>No install is offered for this folder.</p> : null}
      {result.unchanged ? <p className="muted">Same as the previous review of this commit.</p> : null}
      <dl className="agent-facts">
        <dt>Skill</dt>
        <dd>{record.frontmatter.name ?? "(no name)"}</dd>
        <SourceFacts source={record.source} />
        <dt>License</dt>
        <dd>
          {record.license.expression ?? "Unknown"} ({record.license.eligibility})
        </dd>
        <dt>Decision</dt>
        <dd>{gate.result}</dd>
        <dt>Review status</dt>
        <dd>
          {record.status}
          {record.reason ? `: ${record.reason}` : ""}
        </dd>
      </dl>
      <h3>Reasons</h3>
      {reasons.length === 0 ? (
        <p className="muted">The gate returned no reasons.</p>
      ) : (
        <ul className="gate-reasons">
          {reasons.map((reason, index) => (
            <Reason key={`${index}:${reason.kind}`} reason={reason} />
          ))}
        </ul>
      )}
    </section>
  );
}

const ENGINE_LABEL: Record<ProjectSkillEngine, string> = { claude: "Claude Code", codex: "Codex" };
const ENGINE_ROOT: Record<ProjectSkillEngine, string> = { claude: ".claude/skills", codex: ".agents/skills" };

/** Warnings the user must acknowledge before an install or update can be confirmed. */
export function needsAcknowledgement(result: ReviewResultDto): boolean {
  return result.gate.result === "warn" || result.record.license.eligibility === "eligible-by-choice";
}

/** A result the gate did not block and the record accepted; only these can be written. */
export function isInstallable(result: ReviewResultDto): boolean {
  return result.gate.result !== "block" && result.record.status === "reviewed";
}

export function WarningList({ result }: { result: ReviewResultDto }) {
  const reasons = result.gate.reasons ?? [];
  return (
    <div className="gate-warnings" aria-label="Review warnings">
      {result.record.license.eligibility === "eligible-by-choice" ? (
        <p>License: {result.record.license.expression ?? "Unknown"} is eligible by your choice only.</p>
      ) : null}
      {reasons.length > 0 ? (
        <ul className="gate-reasons">
          {reasons.map((reason, index) => (
            <Reason key={`${index}:${reason.kind}`} reason={reason} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function InstallPanel({
  result,
  project,
  agents,
  writes,
}: {
  result: ReviewResultDto;
  project: ProjectDto | null;
  agents: readonly AgentStatusDto[] | null;
  writes: SkillWrites;
}) {
  const { record, gate } = result;
  const name = record.frontmatter.name;
  const installedIds = new Set((agents ?? []).filter((agent) => agent.installed).map((agent) => agent.id as string));
  const engines = (["claude", "codex"] as const).filter(
    (engine) => installedIds.has(engine) && (gate.compatible === undefined || gate.compatible.includes(engine)),
  );
  const [choice, setChoice] = useState<ProjectSkillEngine | null>(null);
  // null = not edited; see `acknowledgedNow` for the single value behind checkbox, eligibility and request.
  const [edited, setEdited] = useState<boolean | null>(null);
  const warningsId = useId();
  const skillId = useId();
  const ack = needsAcknowledgement(result);

  if (!isInstallable(result)) return null;
  if (!project) return <p className="muted">Open a project to install this skill.</p>;
  if (!name) return <p className="muted">This skill has no name, so it cannot be installed.</p>;

  // A request in flight or finished for this review pins its engine, whatever the agent list says later.
  const pinned: WriteEntry | undefined = Object.values(writes.entries).find(
    (entry) =>
      entry.confirmation.project === project.id &&
      entry.confirmation.name === name &&
      entry.confirmation.operation === "install" &&
      entry.confirmation.review_id === record.id &&
      entry.phase !== "failed",
  );
  // After a failure the confirmed engine stays the default for the retry until the user picks another.
  const failedEngine = Object.values(writes.entries).find(
    (entry) =>
      entry.phase === "failed" &&
      entry.confirmation.project === project.id &&
      entry.confirmation.name === name &&
      entry.confirmation.operation === "install" &&
      entry.confirmation.review_id === record.id,
  )?.confirmation.engine;
  const engine: ProjectSkillEngine | null =
    pinned?.confirmation.engine ??
    (choice !== null && engines.includes(choice) ? choice : failedEngine !== undefined ? failedEngine : (engines[0] ?? null));
  const kept = pinned?.confirmation.engine ?? failedEngine;
  const shown = kept && !engines.includes(kept) ? [kept, ...engines] : engines;
  const entry = engine ? writes.entries[writeKey(project.id, engine, name)] : undefined;
  const mine = entry !== undefined && entry.confirmation.review_id === record.id && entry.confirmation.operation === "install" ? entry : undefined;
  const applying = entry?.phase === "applying";
  const done = mine?.phase === "done";
  const destination = mine?.confirmation.destination ?? (engine ? `${project.root}/${ENGINE_ROOT[engine]}/${name}` : "");
  const acknowledgedNow =
    mine && mine.phase !== "failed" ? mine.confirmation.acknowledge_warnings : (edited ?? (mine ? mine.confirmation.acknowledge_warnings : false));
  return (
    <section className="install-panel" aria-label="Install in project">
      <h3>Install in this project</h3>
      {shown.length === 0 ? (
        <p className="muted">No compatible Claude Code or Codex is installed on this computer, so nothing can be installed.</p>
      ) : (
        <>
          <fieldset disabled={applying || done} aria-describedby={skillId}>
            <legend>Agent</legend>
            {shown.map((item) => (
              <label key={item}>
                <input type="radio" name="install-engine" checked={engine === item} onChange={() => setChoice(item)} /> {ENGINE_LABEL[item]}
              </label>
            ))}
          </fieldset>
          <dl className="agent-facts">
            <dt>Skill</dt>
            <dd id={skillId}>{name}</dd>
            <dt>Destination</dt>
            <dd>
              <code>{destination}</code>
            </dd>
            <SourceFacts source={record.source} />
          </dl>
          {ack ? (
            <div id={warningsId}>
              <WarningList result={result} />
              <label>
                <input
                  type="checkbox"
                  checked={acknowledgedNow}
                  disabled={applying || done}
                  aria-describedby={skillId}
                  onChange={(event) => setEdited(event.target.checked)}
                />{" "}
                I have read these warnings.
              </label>
            </div>
          ) : null}
          <button
            type="button"
            aria-describedby={skillId}
            disabled={applying || done || engine === null || (ack && !acknowledgedNow)}
            onClick={() =>
              engine &&
              void writes.apply({
                project: project.id,
                generation: writes.generation,
                engine,
                name,
                operation: "install",
                review_id: record.id,
                expected_review_id: null,
                acknowledge_warnings: ack && acknowledgedNow,
                destination,
              })
            }
          >
            {applying ? "Installing…" : "Confirm install"}
          </button>
        </>
      )}
      <div className="apply-status" role="status" aria-live="polite" aria-atomic="true" aria-describedby={skillId}>
        {applying ? <p>Installing {name}…</p> : null}
        {mine?.phase === "done" ? (
          <p>
            {mine.result.status === "unchanged"
              ? "Already installed; nothing changed."
              : `Installed ${name}. It now appears in Installed.`}
          </p>
        ) : null}
      </div>
      {mine?.phase === "failed" ? (
        <div className="error-box" role="alert">
          <strong>{mine.error.code}</strong>
          <p>{mine.error.message}</p>
          {mine.error.hint ? <p className="muted">{mine.error.hint}</p> : null}
        </div>
      ) : null}
    </section>
  );
}

export function ExploreView({
  review,
  project,
  agents,
  writes,
}: {
  review: Review;
  project: ProjectDto | null;
  agents: readonly AgentStatusDto[] | null;
  writes: SkillWrites;
}) {
  const { state, submit, cancel, cancelError } = review;
  const [input, setInput] = useState("");
  const [invalid, setInvalid] = useState<string | null>(null);
  const inputId = useId();
  const hintId = useId();
  const running = state.phase === "running";

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (running) return;
    const parsed = parseTarget(input);
    if (!parsed.ok) {
      setInvalid(parsed.message);
      return;
    }
    setInvalid(null);
    void submit(parsed.target);
  };

  return (
    <div className="explore">
      <form className="review-form" aria-label="Review a GitHub skill folder" onSubmit={onSubmit} noValidate>
        <label htmlFor={inputId}>GitHub skill folder</label>
        <div className="review-form-row">
          <input
            id={inputId}
            type="text"
            value={input}
            placeholder="owner/repo/folder"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            aria-describedby={hintId}
            aria-invalid={invalid ? true : undefined}
            onChange={(event) => setInput(event.target.value)}
          />
          <button type="submit" disabled={running}>
            Review
          </button>
          {running ? (
            <button type="button" disabled={state.id === null || state.cancelling} onClick={() => void cancel()}>
              Cancel
            </button>
          ) : null}
        </div>
        <p id={hintId} className="muted">
          Reviewing never installs anything; installing needs a separate confirmation. Nested folders are fine.
        </p>
        {invalid ? (
          <p className="error-box" role="alert">
            {invalid}
          </p>
        ) : null}
      </form>

      <div role="status" aria-label="Review status">
        {state.phase === "running" ? (
          <p>
            Reviewing {state.target.repo}/{state.target.path}…
            {state.progress
              ? ` ${state.progress.phase} ${state.progress.done}${state.progress.total === null ? "" : ` / ${state.progress.total}`}`
              : ""}
          </p>
        ) : null}
        {state.phase === "cancelled" ? <p>Review cancelled.</p> : null}
      </div>
      {cancelError ? (
        <p className="error-box" role="alert">
          <strong>{cancelError.code}</strong> {cancelError.message}
        </p>
      ) : null}
      {state.phase === "failed" && !invalid ? (
        <div className="error-box" role="alert">
          <strong>{state.error.code}</strong>
          <p>{state.error.message}</p>
          {state.error.hint ? <p className="muted">{state.error.hint}</p> : null}
        </div>
      ) : null}
      {state.phase === "done" ? <ReviewResult result={state.result} /> : null}
      {state.phase === "done" ? (
        <InstallPanel
          key={`${project?.id ?? ""}:${state.result.record.id}`}
          result={state.result}
          project={project}
          agents={agents}
          writes={writes}
        />
      ) : null}
      {state.phase === "idle" ? <p className="empty">Enter a GitHub skill folder to review it. Nothing to show yet.</p> : null}
    </div>
  );
}
