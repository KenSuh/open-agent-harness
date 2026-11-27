import { useEffect, useRef, useState } from "react";
import type { ErrorDto, ImpactGraphState, ImpactMapAction, TaskId } from "./contract";
import { toErrorDto, type Transport } from "./transport/types";

const graphStatus: Record<ImpactGraphState, string> = {
  absent: "No Graphify file was found at the configured path.",
  available: "The Graphify file matches this checkout.",
  stale: "The Graphify file is from a different branch or commit.",
  unproven: "OAH could not verify which checkout produced the Graphify file.",
};

const refusalMessages: Partial<Record<string, string>> = {
  OAH_IMPACT_NO_COMMIT: "Make the first commit to build an Impact Map.",
  OAH_PROJECT_TOOLS_GIT_LAYOUT: "Open a local Git working tree to build its Impact Map.",
  OAH_PROJECT_TOOLS_GIT_UNSUPPORTED: "The installed Git does not support this Impact Map operation.",
  OAH_PROJECT_TOOLS_GIT_UNTRUSTED: "OAH could not find a trusted Git executable.",
  OAH_PROJECT_TOOLS_UNSAFE_CONFIG: "Review the paths in the project's Impact Map settings.",
  OAH_PROJECT_TOOLS_UNSAFE_STORE: "OAH could not verify the local Impact Map storage folder.",
};

type Run = { id: TaskId | null; cancelling: boolean };

export function ImpactMapActions({
  transport,
  projectId,
  hasMap,
  onSettled,
}: {
  transport: Transport;
  projectId: string;
  hasMap: boolean;
  onSettled: () => void;
}) {
  const active = useRef<Run | null>(null);
  const [busy, setBusy] = useState(false);
  const [canCancel, setCanCancel] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState<ErrorDto | null>(null);
  const [graph, setGraph] = useState<ImpactGraphState | null>(null);

  useEffect(() => () => {
    active.current = null;
  }, [projectId]);

  async function start(action: ImpactMapAction) {
    if (active.current) return;
    const run: Run = { id: null, cancelling: false };
    active.current = run;
    setBusy(true);
    setError(null);
    setMessage(action === "refresh" ? "Preparing to build the map…" : "Checking the map and Graphify file…");
    function finish() {
      active.current = null;
      setBusy(false);
      setCanCancel(false);
    }
    try {
      const id = await transport.startImpactMap({ project_id: projectId, input: { action } }, (event) => {
        if (active.current !== run) return;
        if (event.event === "progress") {
          if (run.cancelling) return;
          if (event.data.done === 0) setMessage("Checking the local Impact Map runner…");
          else setMessage(action === "refresh" ? "Building the map locally…" : "Checking the Graphify file…");
          return;
        }
        finish();
        setError(null);
        if (event.event === "failed") {
          setMessage("");
          setError(event.data);
          onSettled();
          return;
        }
        if (event.event === "cancelled") {
          setMessage("The run stopped. OAH is checking the current map.");
          onSettled();
          return;
        }
        if (event.data.project_id !== projectId || event.data.action !== action) {
          setMessage("");
          setError({
            code: "UI_PROJECT_CHANGED",
            message: "The runner answered for a different project or action.",
            hint: "Try the action again from this project.",
          });
          return;
        }
        setGraph(event.data.graph);
        if (event.data.refusal) {
          setMessage("");
          setError({
            code: event.data.refusal,
            message: refusalMessages[event.data.refusal] ?? "OAH could not complete the Impact Map operation.",
            hint: null,
          });
          return;
        }
        setMessage(action === "status" ? "Checked the local map and graph." : event.data.changed
          ? "Impact Map built." : "The Impact Map was already current.");
        onSettled();
      });
      if (active.current !== run) return;
      run.id = id;
      setCanCancel(true);
    } catch (err) {
      if (active.current !== run) return;
      finish();
      setMessage("");
      setError(toErrorDto(err));
    }
  }

  async function cancel() {
    const run = active.current;
    if (!run?.id || run.cancelling) return;
    run.cancelling = true;
    setError(null);
    setCanCancel(false);
    setMessage("Stopping the Impact Map runner…");
    try {
      await transport.taskCancel(run.id);
    } catch (err) {
      if (active.current !== run) return;
      run.cancelling = false;
      setCanCancel(true);
      setMessage("The Impact Map job is still running.");
      setError(toErrorDto(err));
    }
  }

  return (
    <>
      <div className="tool-settings-actions">
        <button className="tool-save" type="button" aria-disabled={busy} onClick={() => void start("refresh")}>
          {hasMap ? "Refresh Impact Map" : "Build Impact Map"}
        </button>
        <button type="button" aria-disabled={busy} onClick={() => void start("status")}>
          Check Graphify
        </button>
        {busy ? (
          <button type="button" aria-disabled={!canCancel} onClick={() => void cancel()}>
            Cancel
          </button>
        ) : null}
      </div>
      <div className="tool-settings-feedback" role="status">
        {message}
      </div>
      {error ? <p className="status-error" role="alert">{error.message} {error.hint || error.code}</p> : null}
      {graph ? <p className="tool-notes">{graphStatus[graph]}</p> : null}
    </>
  );
}
