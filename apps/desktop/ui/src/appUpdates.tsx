import { useCallback, useEffect, useRef, useState } from "react";
import { toErrorDto, type Transport } from "./transport/types";

type State =
  | { readonly phase: "idle" | "checking" | "current" }
  | { readonly phase: "available" | "installing"; readonly version: string }
  | { readonly phase: "error"; readonly message: string };

const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

export function AppUpdates({ transport }: { transport: Transport }) {
  const [state, setState] = useState<State>({ phase: "idle" });
  const busy = useRef(false);
  const offered = useRef(false);

  const check = useCallback(async (manual: boolean) => {
    if (busy.current || (!manual && offered.current)) return;
    busy.current = true;
    if (manual) setState({ phase: "checking" });
    try {
      const update = await transport.checkAppUpdate();
      offered.current = update !== null;
      setState(update ? { phase: "available", version: update.version } : { phase: manual ? "current" : "idle" });
    } catch (error) {
      if (manual) setState({ phase: "error", message: toErrorDto(error).message });
    } finally {
      busy.current = false;
    }
  }, [transport]);

  useEffect(() => {
    const startup = window.setTimeout(() => void check(false), 0);
    const refresh = window.setInterval(() => void check(false), CHECK_INTERVAL_MS);
    return () => {
      window.clearTimeout(startup);
      window.clearInterval(refresh);
    };
  }, [check]);

  async function install(version: string) {
    if (busy.current) return;
    busy.current = true;
    setState({ phase: "installing", version });
    try {
      await transport.installAppUpdate(version);
    } catch (error) {
      offered.current = false;
      const { message, hint } = toErrorDto(error);
      setState({ phase: "error", message: hint ? `${message} ${hint}` : message });
      busy.current = false;
    }
  }

  const available = state.phase === "available" || state.phase === "installing";
  return (
    <section className="app-updates" aria-label="App updates">
      <div role="status">
        {state.phase === "available" ? <strong>OAH v{state.version} is available</strong> : null}
        {state.phase === "installing" ? <>Installing OAH v{state.version}…</> : null}
        {state.phase === "current" ? <>OAH is up to date.</> : null}
        {state.phase === "error" ? <span className="app-update-error">{state.message}</span> : null}
      </div>
      {available ? (
        <button className="install-app-update" type="button" disabled={state.phase === "installing"}
          onClick={() => void install(state.version)}>Install and restart</button>
      ) : (
        <button type="button" disabled={state.phase === "checking"} onClick={() => void check(true)}>
          {state.phase === "checking" ? "Checking…" : "Check for app updates"}
        </button>
      )}
    </section>
  );
}
