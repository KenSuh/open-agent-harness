// 한국어: 로컬 Wiki 색인 컨트롤 — 접힌 채 시작해 눌렀을 때만 저장된 상태를 읽고, 도구 확인·색인 만들기·검색을 명시 실행으로만 보낸다.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { ErrorDto, Progress, SettingsDto, TaskEvent, WikiIndexAction, WikiIndexInput, WikiIndexReply } from "./contract";
import { toErrorDto, type Transport } from "./transport/types";

const STATE_TEXT: Record<string, string> = {
  not_built: "No local index has been built yet.",
  incomplete: "A build was started and has not finished. Build again to continue it.",
  ready: "The local index is current.",
  stale: "The local index is out of date. Refresh it to match the committed notes.",
};

const TOOL_CODES = ["TOOL_NOT_CONFIGURED", "TOOL_MISSING", "TOOL_REJECTED", "OLLAMA_DOWN", "NOT_LOCAL", "MODEL_MISSING", "MODEL_UNUSABLE"];

/** The concrete next step for a stop code, or null when the message alone says enough. */
export function nextStepFor(code: string | undefined): string | null {
  const name = (code ?? "").replace(/^OAH_WIKI_INDEX_/, "");
  if (["TOOL_NOT_CONFIGURED", "TOOL_MISSING", "TOOL_REJECTED"].includes(name)) {
    return "Install gbrain on this computer, then choose Check Tools again.";
  }
  if (name === "OLLAMA_DOWN" || name === "NOT_LOCAL") {
    return "Start Ollama on this computer so it listens on a loopback address, then choose Check Tools again.";
  }
  if (name === "MODEL_MISSING" || name === "MODEL_UNUSABLE") {
    return "Pull the embedding model in Ollama yourself, then choose Check Tools again. OAH does not download models.";
  }
  if (name === "NO_SNAPSHOT" || name === "NO_NOTES") {
    return "Commit the wiki notes first. The index reads only committed notes.";
  }
  if (name === "BUSY") return "Another index run is in progress. Wait for it to finish.";
  return null;
}

function isToolProblem(code: string | undefined): boolean {
  const name = (code ?? "").replace(/^OAH_WIKI_INDEX_/, "");
  return TOOL_CODES.includes(name);
}

const DEFAULT_PORT = 11434;
type Device = { gbrain: string; bun_entry: string; port: string };

function deviceOf(settings: SettingsDto): Device {
  const w = settings.wiki_index;
  return { gbrain: w?.gbrain ?? "", bun_entry: w?.bun_entry ?? "", port: String(w?.ollama_port ?? DEFAULT_PORT) };
}

const ABSOLUTE = /^(\/|[A-Za-z]:[\\/]|\\\\)/;

/** The reason a device form cannot be saved, or null. The core checks everything again. */
export function deviceProblem(form: Device): string | null {
  const port = form.port.trim();
  if (port !== "" && !(/^\d+$/.test(port) && Number(port) >= 1 && Number(port) <= 65535)) {
    return "Enter an Ollama port from 1 to 65535, or leave it blank for 11434.";
  }
  if (form.bun_entry.trim() !== "" && !ABSOLUTE.test(form.bun_entry.trim())) {
    return "The Bun entry must be an absolute path to gbrain's cli.ts, or blank.";
  }
  return null;
}

/**
 * Device choices for the local tools, read and saved through the existing settings commands. Saving only stores the
 * choices; it runs nothing. It holds no key or credential.
 */
function LocalToolsSetup({ transport }: { transport: Transport }) {
  const uid = useId();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<Device | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const seq = useRef(0);
  useEffect(
    () => () => {
      seq.current += 1;
    },
    [],
  );

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (!next || form) return;
    const mine = ++seq.current;
    setLoading(true);
    setMessage(null);
    transport.settingsGet().then(
      (settings) => {
        if (seq.current !== mine) return;
        setForm(deviceOf(settings));
        setLoading(false);
      },
      (error) => {
        if (seq.current !== mine) return;
        const e = toErrorDto(error);
        setMessage({ ok: false, text: `${e.code}: ${e.message}` });
        setLoading(false);
      },
    );
  };

  const save = async () => {
    if (!form || saving) return;
    const problem = deviceProblem(form);
    if (problem) {
      setMessage({ ok: false, text: problem });
      return;
    }
    const mine = ++seq.current;
    setSaving(true);
    setMessage(null);
    try {
      const port = form.port.trim() === "" ? DEFAULT_PORT : Number(form.port);
      const saved = await transport.settingsSet([
        { key: "wiki_index.gbrain", value: form.gbrain.trim() },
        { key: "wiki_index.bun_entry", value: form.bun_entry.trim() },
        { key: "wiki_index.ollama_port", value: port },
      ]);
      if (seq.current !== mine) return;
      setForm(deviceOf(saved));
      setMessage({ ok: true, text: "Saved. Nothing was run. Choose Check Tools to test these choices." });
    } catch (error) {
      if (seq.current !== mine) return;
      const e = toErrorDto(error);
      setMessage({ ok: false, text: `${e.code}: ${e.message}` });
    } finally {
      if (seq.current === mine) setSaving(false);
    }
  };

  const bodyId = `${uid}-setup`;
  const set = (patch: Partial<Device>) => {
    setForm((f) => (f ? { ...f, ...patch } : f));
    setMessage(null);
  };
  return (
    <div className="wiki-index-setup">
      <button type="button" aria-expanded={open} aria-controls={bodyId} onClick={toggle}>
        Local Tools Setup
      </button>
      {open ? (
        <form
          id={bodyId}
          className="wiki-index-setup-form"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <p className="muted">
            These device choices are stored in OAH's user settings, not in the project. Ollama stays at 127.0.0.1; only its
            port can change. Saving runs nothing.
          </p>
          {loading || !form ? (
            loading ? <p className="muted">Reading the saved choices…</p> : null
          ) : (
            <>
              <label htmlFor={`${uid}-gbrain`}>gbrain command</label>
              <input id={`${uid}-gbrain`} type="text" autoComplete="off" spellCheck={false} value={form.gbrain} onChange={(e) => set({ gbrain: e.target.value })} />
              <p className="muted" id={`${uid}-gbrain-help`}>Leave blank to find gbrain on your PATH.</p>
              <label htmlFor={`${uid}-bun`}>Bun entry (optional)</label>
              <input id={`${uid}-bun`} type="text" autoComplete="off" spellCheck={false} aria-describedby={`${uid}-bun-help`} value={form.bun_entry} onChange={(e) => set({ bun_entry: e.target.value })} />
              <p className="muted" id={`${uid}-bun-help`}>
                An absolute path to gbrain's cli.ts. It takes effect only when the gbrain command above is a trusted Bun
                executable. Leave blank to run the command directly.
              </p>
              <label htmlFor={`${uid}-port`}>Ollama port</label>
              <input id={`${uid}-port`} type="text" inputMode="numeric" autoComplete="off" value={form.port} onChange={(e) => set({ port: e.target.value })} />
              <div className="wiki-index-actions">
                <button type="submit" aria-disabled={saving}>Save Local Tools</button>
              </div>
            </>
          )}
          <div role={message && !message.ok ? "alert" : "status"}>{message ? <p>{message.text}</p> : null}</div>
        </form>
      ) : null}
    </div>
  );
}

type Run = { action: WikiIndexAction; id: string | null; progress: Progress | null; cancelling: boolean };
type Outcome =
  | { action: WikiIndexAction; kind: "reply"; reply: WikiIndexReply }
  | { action: WikiIndexAction; kind: "error"; error: ErrorDto }
  | { action: WikiIndexAction; kind: "cancelled" };
type Found = { query: string; reply: WikiIndexReply };

function sourceText(reply: WikiIndexReply): string | null {
  const parts: string[] = [];
  if (reply.source?.commit) parts.push(`committed snapshot ${reply.source.commit.slice(0, 12)}`);
  if (typeof reply.source?.notes === "number") parts.push(`${reply.source.notes} notes`);
  if (reply.model) {
    parts.push(`model ${reply.model.name}${reply.model.dimensions ? ` (${reply.model.dimensions} dimensions)` : ""}`);
  }
  return parts.length > 0 ? parts.join(", ") : null;
}

function headline(outcome: Outcome): string {
  const label = outcome.action === "probe" ? "Check Tools" : outcome.action === "build" ? "Index build" : outcome.action === "search" ? "Search" : "Index status";
  if (outcome.kind === "cancelled") return `${label} was stopped.`;
  if (outcome.kind === "error") return `${label} did not finish.`;
  if (outcome.reply.ok) {
    return outcome.action === "probe" ? "The local tools are ready." : outcome.action === "build" ? "The local index was built." : `${label} finished.`;
  }
  return `${label} stopped.`;
}

/**
 * The optional local semantic index of the wiki notes, as one compact collapsed section. It is collapsed on project
 * open and sends nothing until the person opens it. It only ever starts the four typed actions, each from a click.
 */
export function ProjectWikiIndex({
  transport,
  projectId,
  managed = false,
  onUseWikiSearch,
}: {
  transport: Transport;
  projectId: string;
  /** True when the project setting asks OAH to index this wiki; only the opening button's name changes. */
  managed?: boolean;
  /** Where "Use Wiki Search" goes; the button is left out without it. */
  onUseWikiSearch?: () => void;
}) {
  const uid = useId();
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState<WikiIndexReply | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [found, setFound] = useState<Found | null>(null);
  const [query, setQuery] = useState("");
  const [cancelError, setCancelError] = useState<ErrorDto | null>(null);
  const seq = useRef(0);
  const running = useRef(false);
  const active = useRef<{ action: WikiIndexAction; id: string | null } | null>(null);

  // Answers that arrive after the project changed or the section went away belong to no run. This section's own
  // check or build is asked to stop; a build can be continued later.
  useEffect(
    () => () => {
      seq.current += 1;
      const task = active.current;
      active.current = null;
      if (task?.id && (task.action === "probe" || task.action === "build")) void transport.taskCancel(task.id).catch(() => undefined);
    },
    [transport],
  );

  const start = useCallback(
    async (input: WikiIndexInput) => {
      if (running.current) return;
      running.current = true;
      const mine = ++seq.current;
      active.current = { action: input.action, id: null };
      let settled = false;
      setRun({ action: input.action, id: null, progress: null, cancelling: false });
      setCancelError(null);
      if (input.action === "search") setFound(null);
      else if (input.action !== "status") setOutcome(null);
      const finish = (apply: () => void): void => {
        if (settled || seq.current !== mine) return;
        settled = true;
        running.current = false;
        active.current = null;
        setRun(null);
        apply();
      };
      const onEvent = (event: TaskEvent<WikiIndexReply>) => {
        switch (event.event) {
          case "progress":
            if (!settled && seq.current === mine) {
              setRun((r) => (r ? { ...r, progress: event.data } : r));
            }
            break;
          case "done": {
            const reply = event.data;
            finish(() => {
              if (reply.project_id !== projectId || reply.action !== input.action) {
                // An answer about another project or action is never shown here.
                setOutcome({
                  action: input.action,
                  kind: "error",
                  error: { code: "UI_ANSWER_MISMATCH", message: "The answer was for another project or action, so it is not shown.", hint: null },
                });
                return;
              }
              if (input.action === "search") {
                setFound({ query: input.query, reply });
                if (!reply.ok) setOutcome({ action: "search", kind: "reply", reply });
                return;
              }
              if (input.action === "status" || input.action === "build") setIndex(reply.state ? reply : null);
              // A status that is fine needs no message of its own; the summary shows it.
              if (input.action !== "status" || !reply.ok) setOutcome({ action: input.action, kind: "reply", reply });
            });
            break;
          }
          case "failed":
            finish(() => setOutcome({ action: input.action, kind: "error", error: event.data }));
            break;
          case "cancelled":
            finish(() => setOutcome({ action: input.action, kind: "cancelled" }));
            break;
        }
      };
      try {
        const id = await transport.startWikiIndex({ project_id: projectId, input }, onEvent);
        if (!settled && seq.current === mine) {
          if (active.current) active.current.id = id;
          setRun((r) => (r ? { ...r, id } : r));
        } else if (!settled && (input.action === "probe" || input.action === "build")) {
          // The section went away before the task id came back; stop the task it started.
          void transport.taskCancel(id).catch(() => undefined);
        }
      } catch (error) {
        finish(() => setOutcome({ action: input.action, kind: "error", error: toErrorDto(error) }));
      }
    },
    [transport, projectId],
  );

  const stop = useCallback(async () => {
    if (!run || run.id === null || run.cancelling) return;
    const { id } = run;
    const mine = seq.current;
    setRun((r) => (r ? { ...r, cancelling: true } : r));
    try {
      await transport.taskCancel(id);
    } catch (error) {
      if (seq.current !== mine || !running.current) return;
      setRun((r) => (r ? { ...r, cancelling: false } : r));
      setCancelError(toErrorDto(error));
    }
  }, [run, transport]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    // Opening is the click that reads the saved state; nothing else reads it.
    if (next) void start({ action: "status" });
  };

  const busy = run !== null;
  const canStop = run !== null && (run.action === "probe" || run.action === "build");
  const bodyId = `${uid}-body`;
  const problemCode = outcome?.kind === "reply" && !outcome.reply.ok ? outcome.reply.code : undefined;
  const step = nextStepFor(problemCode);
  const buildLabel = index?.state === "ready" || index?.state === "stale" ? "Refresh Index" : "Build Index";
  const alert = outcome !== null && (outcome.kind === "error" || (outcome.kind === "reply" && !outcome.reply.ok));
  const progress = run?.progress ?? null;

  return (
    <div className="wiki-index" data-open={open ? "" : undefined}>
      <button type="button" className="wiki-index-toggle" aria-expanded={open} aria-controls={bodyId} onClick={toggle}>
        {managed ? "Manage Local Index" : "Local Wiki Index"}
      </button>
      {open ? (
        <div id={bodyId} className="wiki-index-body">
          <p className="muted">
            An optional semantic index of the committed wiki notes. It uses the gbrain installed on this computer and an
            Ollama that is already running here. OAH does not install, start or download anything. Turn on “Ask OAH to
            index this wiki” and choose the model in this wiki's settings.
          </p>
          {index ? (
            <p className="wiki-index-state">
              {STATE_TEXT[index.state] ?? `Index state: ${index.state}.`}
              {sourceText(index) ? <span className="muted"> {sourceText(index)}.</span> : null}
            </p>
          ) : null}
          <div className="wiki-index-actions">
            <button type="button" aria-disabled={busy} onClick={() => { if (!busy) void start({ action: "probe" }); }}>
              Check Tools
            </button>
            <button type="button" aria-disabled={busy} onClick={() => { if (!busy) void start({ action: "build" }); }}>
              {buildLabel}
            </button>
            {canStop ? (
              <button type="button" aria-disabled={run?.cancelling} onClick={() => void stop()}>
                Stop
              </button>
            ) : null}
          </div>
          <div className="wiki-index-feedback" role={alert ? "alert" : "status"}>
            {busy ? (
              <p className="muted">
                Working…{progress ? ` ${progress.phase}${progress.total ? ` ${progress.done}/${progress.total}` : ` ${progress.done}`}` : ""}
              </p>
            ) : null}
            {cancelError ? <p>{cancelError.code}: {cancelError.message}</p> : null}
            {!busy && outcome ? (
              <div className={alert ? "error-box" : "notice"}>
                <strong>{headline(outcome)}</strong>
                {outcome.kind === "error" ? (
                  <p>{outcome.error.code}: {outcome.error.message}{outcome.error.hint ? ` ${outcome.error.hint}` : ""}</p>
                ) : null}
                {outcome.kind === "reply" ? (
                  <>
                    {outcome.reply.message ? <p>{outcome.reply.message}</p> : null}
                    {outcome.reply.ok && sourceText(outcome.reply) ? <p>{sourceText(outcome.reply)}.</p> : null}
                  </>
                ) : null}
                {step ? <p><strong>Next step:</strong> {step}</p> : null}
                {onUseWikiSearch && isToolProblem(problemCode) ? (
                  <button type="button" onClick={onUseWikiSearch}>Use Wiki Search</button>
                ) : null}
              </div>
            ) : null}
          </div>
          <LocalToolsSetup transport={transport} />
          <form
            className="wiki-index-search"
            role="search"
            onSubmit={(event) => {
              event.preventDefault();
              const text = query.trim();
              if (text && !busy) void start({ action: "search", query: text });
            }}
          >
            <label htmlFor={`${uid}-query`}>Search the local index</label>
            <div className="wiki-index-search-row">
              <input id={`${uid}-query`} type="search" maxLength={300} value={query} onChange={(e) => setQuery(e.target.value)} />
              <button type="submit" aria-disabled={busy || query.trim() === ""}>Search</button>
            </div>
          </form>
          {found && found.reply.ok ? (
            <div className="wiki-index-results" role="status">
              {found.reply.mode === "text" ? (
                <p className="notice">
                  {found.reply.message ?? "The local index could not be used, so these are plain text matches."}
                </p>
              ) : null}
              {found.reply.results && found.reply.results.length > 0 ? (
                <ul>
                  {found.reply.results.map((hit) => (
                    <li key={`${hit.file}:${hit.line}`}>
                      <strong>{hit.title ?? hit.file}</strong>
                      <span> <code translate="no">{hit.file}:{hit.line}</code></span>
                      {hit.heading ? <span className="muted"> {hit.heading}</span> : null}
                      <p>{hit.snippet}</p>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">No notes matched “{found.query}”.</p>
              )}
              {found.reply.truncated ? <p className="muted">Only the first results are shown.</p> : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
