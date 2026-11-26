import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { isInstallable, needsAcknowledgement } from "./confirmation";
import type { AgentId, ErrorDto, InstalledSkillManagementDto, InstalledSkillTarget, LinkedSkillSourceDto, ReviewResultDto } from "./contract";
import { HEADLINE, SourceFacts, WarningList } from "./explore";
import { reviewOnce } from "./reviewOnce";
import { toErrorDto, type Transport } from "./transport/types";
import { parseTarget } from "./useReview";

function parseSource(input: string): ReturnType<typeof parseTarget> {
  const text = input.trim();
  if (!text.includes("://")) return parseTarget(text);
  const invalid = { ok: false, message: "Paste a GitHub link to the skill folder or its SKILL.md file." } as const;
  try {
    const url = new URL(text);
    if (url.origin !== "https://github.com" || url.username || url.password) return invalid;
    const [owner, repo, kind, ref, ...folder] = url.pathname.slice(1).replace(/\/$/, "").split("/");
    if (!owner || !repo || !ref || (kind !== "tree" && kind !== "blob")) return invalid;
    const decoded = folder.map((part) => decodeURIComponent(part));
    if (decoded.some((part) => /[\\/]/.test(part) || part === "." || part === "..")) return invalid;
    if (kind === "blob" && decoded.pop() !== "SKILL.md") return invalid;
    if (decoded.length === 0) return invalid;
    return parseTarget([owner, repo, ...decoded].join("/"));
  } catch {
    return invalid;
  }
}

function ActionError({ error }: { error: ErrorDto }) {
  return <p className="error-box" role="alert">{error.message}{error.hint ? ` ${error.hint}` : null}</p>;
}

function SourceRegistration({
  transport, target, detail, onCancel, onRegistered,
}: {
  transport: Transport;
  target: InstalledSkillTarget;
  detail: InstalledSkillManagementDto;
  onCancel(): void;
  onRegistered(source: LinkedSkillSourceDto): void;
}) {
  const linkId = useId();
  const [input, setInput] = useState(detail.source ? `${detail.source.repo}/${detail.source.path}` : "");
  const [result, setResult] = useState<ReviewResultDto | null>(null);
  const [error, setError] = useState<ErrorDto | null>(null);
  const [invalid, setInvalid] = useState<string | null>(null);
  const [busy, setBusy] = useState<"review" | "register" | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const warnings = result !== null && needsAcknowledgement(result);

  const review = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy !== null) return;
    const parsed = parseSource(input);
    if (!parsed.ok) {
      setInvalid(parsed.message);
      return;
    }
    setError(null);
    setInvalid(null);
    setResult(null);
    setAcknowledged(false);
    setBusy("review");
    try {
      setResult(await reviewOnce(transport, parsed.target.repo, parsed.target.path));
    } catch (error) {
      setError(toErrorDto(error));
    } finally {
      setBusy(null);
    }
  };

  const register = async () => {
    if (busy !== null || result === null || !isInstallable(result) || (warnings && !acknowledged)) return;
    setError(null);
    setBusy("register");
    try {
      onRegistered(await transport.registerSkillSource({
        target, revision: detail.revision, review_id: result.record.id, acknowledge_warnings: acknowledged,
      }));
    } catch (error) {
      setError(toErrorDto(error));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="review-form">
      <form className="review-form" onSubmit={review} noValidate>
        <label htmlFor={linkId}>GitHub skill link</label>
        <p id={`${linkId}-help`} className="muted">
          Paste the folder or SKILL.md link, or enter <code>owner/repo/folder</code>. OAH reviews the repository’s default branch.
        </p>
        <div className="review-form-row">
          <input id={linkId} aria-describedby={`${linkId}-help`} aria-invalid={invalid !== null}
            value={input} disabled={busy !== null} onChange={(event) => {
              setInput(event.target.value);
              setResult(null);
              setError(null);
              setInvalid(null);
            }} />
          <button type="submit" disabled={busy !== null}>Review source</button>
        </div>
      </form>
      {invalid ? <p className="error-box" role="alert">{invalid}</p> : null}
      {busy ? <p role="status">{busy === "review" ? "Reviewing GitHub source…" : "Registering GitHub source…"}</p> : null}
      {result ? (
        <section className="review-result" aria-label="Reviewed GitHub source" data-result={result.gate.result}>
          <h3>{HEADLINE[result.gate.result]}</h3>
          <dl className="agent-facts"><SourceFacts source={result.record.source} /></dl>
          <WarningList result={result} />
          {warnings && isInstallable(result) ? (
            <label><input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} /> I acknowledge the review warnings.</label>
          ) : null}
        </section>
      ) : null}
      {error ? <ActionError error={error} /> : null}
      <div className="review-form-row">
        <button type="button" onClick={onCancel} disabled={busy !== null}>Cancel</button>
        {result && isInstallable(result) ? (
          <button type="button" onClick={register} disabled={busy !== null || (warnings && !acknowledged)}>Register Git source</button>
        ) : null}
      </div>
    </div>
  );
}

function ManagementPanel({
  target, name, transport, labels, onRegistered, onRemoved,
}: {
  target: InstalledSkillTarget;
  name: string;
  transport: Transport;
  labels: Record<AgentId, string>;
  onRegistered(source: LinkedSkillSourceDto): void;
  onRemoved(archive: string): void;
}) {
  const [detail, setDetail] = useState<InstalledSkillManagementDto | null>(null);
  const [error, setError] = useState<ErrorDto | null>(null);
  const [mode, setMode] = useState<"actions" | "source" | "remove">("actions");
  const [removing, setRemoving] = useState(false);
  const [retry, setRetry] = useState(0);
  const cancel = useRef<HTMLButtonElement>(null);
  const { location, project } = target;

  useEffect(() => {
    let current = true;
    transport.inspectInstalledSkill({ location, project }).then(
      (value) => { if (current) setDetail(value); },
      (error) => { if (current) setError(toErrorDto(error)); },
    );
    return () => { current = false; };
  }, [location, project, retry, transport]);
  useEffect(() => {
    if (mode === "remove") cancel.current?.focus();
  }, [mode]);

  const reload = () => {
    setDetail(null);
    setError(null);
    setMode("actions");
    setRetry((value) => value + 1);
  };

  if (detail === null) return error ? (
    <div><ActionError error={error} /><button type="button" onClick={reload}>Retry</button></div>
  ) : <p role="status">Checking the installed folder…</p>;

  const shared = detail.agents.length > 1;
  const remove = async () => {
    if (removing) return;
    setRemoving(true);
    setError(null);
    try {
      const { archive } = await transport.removeInstalledSkill({
        target, revision: detail.revision, confirm_shared: shared,
      });
      onRemoved(archive);
      reload();
    } catch (error) {
      setError(toErrorDto(error));
    } finally {
      setRemoving(false);
    }
  };

  return (
    <section className="review-result review-form" aria-label={`Manage ${name}`}>
      {mode === "source" ? (
        <SourceRegistration target={target} detail={detail} transport={transport}
          onCancel={() => setMode("actions")} onRegistered={(source) => {
            onRegistered(source);
            reload();
          }} />
      ) : mode === "remove" ? (
        <div className="review-form" role="group" aria-label="Confirm skill removal">
          <h3>{shared ? "Remove this shared skill for all its agents?" : "Remove this skill?"}</h3>
          <p><strong>{name}</strong> · <code>{detail.location}</code></p>
          <p>Agents using this path: {detail.agents.map((id) => labels[id]).join(", ")}.</p>
          <p className="muted">The folder will move to a recovery location.</p>
          {error ? <ActionError error={error} /> : null}
          <div className="review-form-row">
            <button ref={cancel} type="button" disabled={removing} onClick={() => { setMode("actions"); setError(null); }}>Cancel</button>
            <button type="button" className="skill-remove" disabled={removing || error !== null} onClick={remove}>
              {shared ? "Remove for all agents" : "Remove skill"}
            </button>
            {error ? <button type="button" onClick={reload}>Refresh skill details</button> : null}
          </div>
          {removing ? <p role="status">Removing skill…</p> : null}
        </div>
      ) : (
        <>
          {detail.source ? (
            <dl className="agent-facts">
              <dt>Git source</dt><dd>{detail.source.repo}/{detail.source.path}</dd>
              <dt>Reviewed commit</dt><dd><code>{detail.source.commit}</code></dd>
              <dt>Updates</dt><dd>{detail.source.updates_in_oah ? "Managed in OAH" : "Source registered for reference"}</dd>
            </dl>
          ) : null}
          <div className="review-form-row">
            {!detail.source?.updates_in_oah ? <button type="button" onClick={() => setMode("source")}>
              {detail.source ? "Change Git source" : "Register Git source"}
            </button> : null}
            <button type="button" onClick={() => setMode("remove")}>Remove…</button>
          </div>
        </>
      )}
    </section>
  );
}

export function InstalledSkillActions(props: Parameters<typeof ManagementPanel>[0]) {
  const [open, setOpen] = useState(false);
  return (
    <div className="review-form">
      <div>
        <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          {open ? "Close management" : "Manage…"}
        </button>
      </div>
      {open ? <ManagementPanel {...props} /> : null}
    </div>
  );
}
