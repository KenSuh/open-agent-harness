// 한국어: Explore 화면 — GitHub 폴더 직접 입력 검토 폼과 결과(고정 커밋·라이선스·게이트 사유). 검토 데이터는 전부 텍스트로만 그린다.
import { useId, useState, type FormEvent } from "react";
import type { GateReasonDto, ReviewResultDto } from "./contract";
import { parseTarget, type useReview } from "./useReview";

type Review = ReturnType<typeof useReview>;

function lineText(lines: number[]): string {
  if (lines.length === 0) return "";
  return `SKILL.md ${lines.length === 1 ? "line" : "lines"} ${lines.join(", ")}`;
}

function Reason({ reason }: { reason: GateReasonDto }) {
  const lines = lineText(reason.lines ?? []);
  return (
    <li className="gate-reason" data-kind={reason.kind}>
      <strong>{reason.kind}</strong> <span>{reason.detail}</span>
      {lines ? <span className="muted"> ({lines})</span> : null}
    </li>
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
        <dt>Folder</dt>
        <dd>
          {record.source.locator} / {record.source.path}
        </dd>
        <dt>Pinned commit</dt>
        <dd>{record.source.commit ? <code>{record.source.commit}</code> : "Not pinned"}</dd>
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

export function ExploreView({ review }: { review: Review }) {
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
          Reviews the folder only; nothing is installed. Nested folders are fine.
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
      {state.phase === "failed" ? (
        <div className="error-box" role="alert">
          <strong>{state.error.code}</strong>
          <p>{state.error.message}</p>
          {state.error.hint ? <p className="muted">{state.error.hint}</p> : null}
        </div>
      ) : null}
      {state.phase === "done" ? <ReviewResult result={state.result} /> : null}
      {state.phase === "idle" ? <p className="empty">Enter a GitHub skill folder to review it. Nothing to show yet.</p> : null}
    </div>
  );
}
