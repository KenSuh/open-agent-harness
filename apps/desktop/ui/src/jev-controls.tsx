// 한국어: Jev 기기 제어 — 관리 열기 때만 상태를 읽고, 동의 미리보기→확인, 기기 끄기, 키 저장·교체·삭제, 고정 연결 점검(별도 미리보기·승인)을 한 호출(project_jev_action)로 수행한다. 낡은 응답은 버리고 키는 저장 호출 밖으로 나가지 않는다.
import { useEffect, useRef, useState, type RefObject } from "react";
import type { ErrorDto } from "./contract";
import {
  parseJevReply,
  type JevAction,
  type JevEnvelope,
  type JevInput,
  type JevReply,
} from "./jevContract";
import { toErrorDto, type Transport } from "./transport/types";

/** The distinct real states the one feedback region can be in. */
type Outcome =
  | { state: "loading"; text: string }
  | { state: "unavailable"; text: string; code: string | null }
  | { state: "refusal"; text: string; word: string }
  | { state: "ready"; text: string }
  | { state: "saved"; text: string }
  | { state: "disabled"; text: string };

interface View {
  readonly projectId: string;
  readonly status: JevEnvelope<"status"> | null;
  readonly keys: JevEnvelope<"key-state"> | null;
  readonly consentPreview: JevEnvelope<"prepare"> | null;
  readonly testPreview: JevEnvelope<"prepare-test"> | null;
  readonly testResult: JevEnvelope<"test"> | null;
  readonly busy: JevAction | "open" | null;
  readonly keyForm: boolean;
  readonly confirmDelete: boolean;
  readonly outcome: Outcome | null;
}

/** Words core answers with that a person can act on; an unknown word is shown as the word. */
const WORDS: Record<string, string> = {
  "no-consent": "This device has not approved Jev for this project.",
  "consent-revoked": "Jev was switched off for this project on this device.",
  "consent-stale": "What Jev would send changed since you approved it. Review and approve again.",
  "device-state": "The saved approval on this device cannot be trusted, so nothing is sent. Check the device's OAH state folder.",
  destination: "This project's destination cannot be used with the key on this device.",
  "config-invalid": "The project's settings file could not be read, so no preview was made.",
  "preview-changed": "The settings changed after the preview. Review again before approving.",
  "ack-required": "The approval did not match the preview. Review again.",
  scope: "That approval scope is not offered.",
  "key-locked": "The device key store is locked. Unlock it and try again.",
  "key-denied": "The device key store refused access. Allow OAH in its settings and try again.",
  "no-key": "No Jev key is available. Save one on this device first.",
  invalid: "That key was not accepted. Check that you copied it whole.",
  locked: "The device key store is locked. Unlock it and try again.",
  denied: "The device key store refused access. Allow OAH in its settings and try again.",
  unsupported: "This device has no key store OAH can use.",
  error: "The device key store failed. Nothing was saved.",
  unauthorized: "The provider did not accept the key.",
  "rate-limited": "The provider asked for a pause. Try again later.",
  overloaded: "The provider is busy. Try again later.",
  timeout: "The provider did not answer in time.",
  network: "The provider could not be reached.",
  "bad-response": "The provider's answer could not be understood.",
};

function wordText(reply: JevReply): string {
  return WORDS[reply.status] ?? (reply.status.startsWith("http-") ? "The provider answered with " + reply.status + "." : null) ?? reply.message ?? reply.status;
}

const KEY_TEXT: Record<string, string> = {
  present: "A key is stored on this device.",
  missing: "No key is stored on this device.",
  locked: "The device key store is locked.",
  denied: "The device key store refused access.",
  unsupported: "This device has no key store OAH can use.",
  error: "The device key store failed.",
};

const CONSENT_TEXT: Record<string, string> = {
  none: "Not approved on this device.",
  enabled: "Approved on this device.",
  disabled: "Switched off on this device.",
  "consent-stale": "Approved before, but what is sent changed. Approve again to use it.",
  "device-state": "The saved approval cannot be trusted, so nothing is sent.",
};

const unavailable = (raw: unknown): Outcome => {
  const error: ErrorDto = toErrorDto(raw);
  return { state: "unavailable", text: error.message, code: error.code };
};

interface Controls {
  readonly view: View | null;
  open(): void;
  close(): void;
  run(input: JevInput, after?: (reply: JevReply) => Partial<View>): void;
  patch(change: Partial<View>): void;
}

/**
 * One Manage Jev session for the open project. Nothing runs until open(). Each call is numbered: an answer is used
 * only if it names this project and the same action and no newer step, close or project change came first.
 */
function useJev(transport: Transport, projectId: string): Controls {
  const [state, setState] = useState<View | null>(null);
  const step = useRef(0);
  useEffect(
    () => () => {
      step.current += 1;
      setState(null);
    },
    [projectId],
  );
  const view = state?.projectId === projectId ? state : null;

  const call = async (input: JevInput): Promise<JevReply> => {
    const raw = await transport.projectJevAction({ project_id: projectId, input });
    const reply = parseJevReply(raw);
    if (reply === null) throw { code: "UI_JEV_BAD_REPLY", message: "The answer was not in the expected form, so it is not used.", hint: null };
    if (reply.project_id !== projectId || reply.action !== input.kind) {
      throw { code: "UI_JEV_REPLY_MISMATCH", message: "The answer was for another project or action, so it is not used.", hint: null };
    }
    return reply;
  };

  const open = async () => {
    const ticket = ++step.current;
    const base: View = {
      projectId, status: null, keys: null, consentPreview: null, testPreview: null, testResult: null,
      busy: "open", keyForm: false, confirmDelete: false, outcome: { state: "loading", text: "Reading what this device holds for Jev…" },
    };
    setState(base);
    try {
      const [status, keys] = await Promise.all([call({ kind: "status" }), call({ kind: "key-state" })]);
      if (ticket !== step.current) return;
      if (status.action !== "status" || keys.action !== "key-state") return;
      setState((now) =>
        now ? { ...now, status, keys, busy: null, outcome: status.ok ? null : { state: "refusal", text: wordText(status), word: status.status } } : now,
      );
    } catch (raw) {
      if (ticket !== step.current) return;
      setState((now) => (now ? { ...now, busy: null, outcome: unavailable(raw) } : now));
    }
  };

  const run = async (input: JevInput, after?: (reply: JevReply) => Partial<View>) => {
    if (!view || view.busy) return;
    const ticket = ++step.current;
    setState({ ...view, busy: input.kind, outcome: { state: "loading", text: "Working…" } });
    try {
      const reply = await call(input);
      if (ticket !== step.current) return;
      setState((now) => {
        if (!now) return now;
        const change = after ? after(reply) : {};
        return { ...now, busy: null, ...change };
      });
    } catch (raw) {
      if (ticket !== step.current) return;
      setState((now) => (now ? { ...now, busy: null, outcome: unavailable(raw) } : now));
    }
  };

  return {
    view,
    open: () => void open(),
    close() {
      step.current += 1;
      setState(null);
    },
    run: (input, after) => void run(input, after),
    patch(change) {
      if (!view || view.busy) return;
      step.current += 1;
      setState({ ...view, ...change });
    },
  };
}

function refusal(reply: JevReply): Outcome {
  return { state: "refusal", text: wordText(reply), word: reply.status };
}

function Preview({ title, children, onApprove, approveLabel, onCancel, busy, regionRef }: {
  title: string; children: React.ReactNode; onApprove: () => void; approveLabel: string; onCancel: () => void; busy: boolean;
  regionRef: RefObject<HTMLElement | null>;
}) {
  return (
    <section className="tool-review jev-preview" tabIndex={-1} ref={regionRef} aria-label={title}>
      <h4>{title}</h4>
      {children}
      <div className="tool-actions">
        <button type="button" aria-disabled={busy} onClick={() => { if (!busy) onApprove(); }}>{approveLabel}</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
    </section>
  );
}

/** The Jev card's device controls. Rendering this reads nothing: the Manage button is the only way in. */
export function JevControls({ transport, projectId }: { transport: Transport; projectId: string }) {
  const jev = useJev(transport, projectId);
  const { view } = jev;
  const panel = useRef<HTMLDivElement>(null);
  const consentRegion = useRef<HTMLElement>(null);
  const testRegion = useRef<HTMLElement>(null);
  const keyInput = useRef<HTMLInputElement>(null);
  const feedback = useRef<HTMLParagraphElement>(null);
  const manage = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  const open = view !== null;
  useEffect(() => {
    if (open) panel.current?.focus();
    else if (wasOpen.current) manage.current?.focus();
    wasOpen.current = open;
  }, [open]);
  const hasConsentPreview = view?.consentPreview != null;
  const hasTestPreview = view?.testPreview != null;
  const hasKeyForm = view?.keyForm === true;
  useEffect(() => { if (hasConsentPreview) consentRegion.current?.focus(); }, [hasConsentPreview]);
  useEffect(() => { if (hasTestPreview) testRegion.current?.focus(); }, [hasTestPreview]);
  useEffect(() => { if (hasKeyForm) keyInput.current?.focus(); }, [hasKeyForm]);
  const outcomeKey = view?.outcome ? view.outcome.state + view.outcome.text : "";
  useEffect(() => {
    // A finished step (saved, disabled, refused) moves focus to its message so it is read and Escape-free to leave.
    if (outcomeKey && view?.outcome && view.outcome.state !== "loading" && view.outcome.state !== "ready") feedback.current?.focus();
  }, [outcomeKey]);

  // The temporary key field is emptied whenever it goes away, whatever the reason.
  useEffect(() => {
    const field = keyInput.current;
    return () => { if (field) field.value = ""; };
  }, [hasKeyForm, projectId]);

  if (!view) {
    return (
      <div className="tool-jev">
        <button type="button" className="tool-edit" id="project-tool-jev-manage" ref={manage} onClick={jev.open}>Manage Jev on this device</button>
      </div>
    );
  }

  const { status, keys } = view;
  const busy = view.busy !== null;
  const dest = status?.destination;
  const consent = status?.consent ?? "none";
  const active = consent === "enabled" || consent === "consent-stale";
  const custom = dest?.kind === "custom";
  const refused = dest !== undefined && dest.kind === "refused";
  const deviceKey = keys?.key_state?.device;
  const hasKey = deviceKey === "present";

  const submitKey = () => {
    const field = keyInput.current;
    const value = field?.value ?? "";
    if (field) field.value = "";
    if (value === "") {
      jev.patch({ outcome: { state: "refusal", text: "Type the key first.", word: "empty" } });
      return;
    }
    const replacing = hasKey;
    jev.run({ kind: "store-key", key: value }, (reply) =>
      reply.ok && (reply.status === "added" || reply.status === "updated")
        ? { keyForm: false, keys: { ...(keys as JevEnvelope<"key-state">), key_state: { environment: keys?.key_state?.environment ?? false, device: "present" as const } }, outcome: { state: "saved", text: reply.status === "updated" ? "The key on this device was replaced." : "The key was saved on this device." } }
        : { keyForm: replacing, outcome: refusal(reply) },
    );
  };
  const dismissKey = () => {
    if (keyInput.current) keyInput.current.value = "";
    jev.patch({ keyForm: false, outcome: null });
  };

  return (
    <div className="tool-jev tool-settings" id="project-tool-jev-controls" data-phase={view.busy ?? "idle"} ref={panel} tabIndex={-1} role="group" aria-label="Jev on this device" aria-busy={busy}>
      <h4>Jev on this device</h4>
      <p className="tool-boundary">
        Jev reorders Impact Map results only when this device has approved it <strong>and</strong> the project settings above turn it on
        (Use Jev in this project and Re-order Impact Map results with Jev). Either one alone sends nothing.
      </p>

      <p ref={feedback} tabIndex={-1} className="tool-settings-feedback" role="status" data-state={view.outcome?.state ?? "idle"}>
        {view.outcome ? view.outcome.text : ""}
      </p>

      {status ? (
        <dl className="jev-status">
          <dt>Approval on this device</dt>
          <dd data-consent={consent}>{CONSENT_TEXT[consent]}</dd>
          <dt>Destination</dt>
          <dd>
            {dest ? (
              <>
                <code translate="no">{dest.url}</code> · model <code translate="no">{dest.model}</code>
                {dest.kind === "official" ? " (official TypeSafe address)" : null}
              </>
            ) : (
              WORDS[status.status] ?? "No usable destination."
            )}
          </dd>
          <dt>Device key</dt>
          <dd>{keys ? (keys.key_state?.environment ? "A key is set in this app's environment. " : "") + (deviceKey ? KEY_TEXT[deviceKey] : "") : "Not read."}</dd>
        </dl>
      ) : null}

      {custom || refused ? (
        <p className="tool-boundary" role="note">
          {custom
            ? "This project names its own destination. A key stored on this device is never sent there, so Jev stays blocked for it here."
            : "This project's destination cannot be used, so Jev stays blocked for it here."}
        </p>
      ) : null}

      {status && !custom && !refused && !view.consentPreview ? (
        <div className="tool-field">
          <h4>Impact Map opt-in</h4>
          <p className="muted">Nothing is sent until you review what would be sent and approve it.</p>
          <button type="button" aria-disabled={busy} onClick={() => {
            if (busy) return;
            jev.run({ kind: "prepare" }, (reply) =>
              reply.action === "prepare" && reply.ok && reply.status === "ready" && reply.token
                ? { consentPreview: reply, outcome: { state: "ready", text: "Review what would be sent. Nothing is sent or saved yet." } }
                : { outcome: refusal(reply) },
            );
          }}>{active ? "Review Impact Opt-in Again" : "Review Impact Opt-in"}</button>
        </div>
      ) : null}

      {view.consentPreview ? (
        <Preview
          title="Approve Impact reordering on this device"
          regionRef={consentRegion}
          busy={busy}
          approveLabel="Approve on This Device"
          onCancel={() => jev.patch({ consentPreview: null, outcome: null })}
          onApprove={() => {
            const token = view.consentPreview?.token;
            if (!token) return;
            jev.run({ kind: "enable", token }, (reply) =>
              reply.ok && reply.status === "enabled"
                ? {
                    consentPreview: null,
                    status: { ...(status as JevEnvelope<"status">), consent: "enabled" },
                    outcome: { state: "saved", text: "Approved on this device. Jev also needs the project settings turned on before it reorders anything." },
                  }
                : { consentPreview: null, outcome: refusal(reply) },
            );
          }}
        >
          {view.consentPreview.destination ? (
            <p>
              Sends to <code translate="no">{view.consentPreview.destination.url}</code>, model{" "}
              <code translate="no">{view.consentPreview.destination.model}</code>. At most one request per run.
            </p>
          ) : null}
          {(view.consentPreview.scopes ?? []).map((scope) => (
            <div key={scope.scope} data-scope={scope.scope}>
              <strong>{scope.title ?? scope.scope}</strong>
              <ul>
                {(scope.sends ?? []).map((line) => <li key={line}>Sends: {line}</li>)}
                {scope.labels && scope.labels.length > 0 ? <li>Fields per row: {scope.labels.join(", ")}</li> : null}
                {(scope.never ?? []).map((line) => <li key={line}>Never: {line}</li>)}
                {Object.entries(scope.bounds ?? {}).map(([name, value]) => <li key={name}>Limit {name}: {value}</li>)}
              </ul>
            </div>
          ))}
          <p className="muted">The provider may charge for the request. No file contents, search text or row details are sent.</p>
        </Preview>
      ) : null}

      {status && active ? (
        <button type="button" id="jev-disable" aria-disabled={busy} onClick={() => {
          if (busy) return;
          jev.run({ kind: "disable" }, (reply) =>
            reply.ok && reply.status === "disabled"
              ? {
                  consentPreview: null,
                  status: { ...(status as JevEnvelope<"status">), consent: "disabled" },
                  outcome: { state: "disabled", text: "Jev is switched off on this device. Nothing is sent for this project." },
                }
              : { outcome: refusal(reply) },
          );
        }}>Disable on this device</button>
      ) : null}

      {keys ? (
        <div className="tool-field">
          <h4>Device key</h4>
          {view.keyForm ? (
            <form onSubmit={(event) => { event.preventDefault(); if (!busy) submitKey(); }} onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); dismissKey(); } }}>
              <label htmlFor="jev-key-input">{hasKey ? "New key to replace the stored one" : "Jev key"}</label>
              <input id="jev-key-input" ref={keyInput} type="password" autoComplete="off" spellCheck={false} autoCapitalize="off" translate="no" aria-describedby="jev-key-help" />
              <span id="jev-key-help" className="muted">Goes only to this device's key store. It is cleared as soon as you save or cancel.</span>
              <div className="tool-actions">
                <button type="submit" aria-disabled={busy}>Save Key on This Device</button>
                <button type="button" onClick={dismissKey}>Cancel</button>
              </div>
            </form>
          ) : view.confirmDelete ? (
            <div role="group" aria-label="Delete the device key?">
              <p>Delete the key stored on this device? Jev stops working until you save one again.</p>
              <div className="tool-actions">
                <button type="button" aria-disabled={busy} onClick={() => {
                  if (busy) return;
                  jev.run({ kind: "delete-key" }, (reply) =>
                    reply.ok && (reply.status === "removed" || reply.status === "missing")
                      ? {
                          confirmDelete: false,
                          keys: { ...(keys as JevEnvelope<"key-state">), key_state: { environment: keys.key_state?.environment ?? false, device: "missing" } },
                          outcome: { state: "saved", text: reply.status === "removed" ? "The key was deleted from this device." : "There was no key on this device." },
                        }
                      : { confirmDelete: false, outcome: refusal(reply) },
                  );
                }}>Delete Key</button>
                <button type="button" onClick={() => jev.patch({ confirmDelete: false, outcome: null })}>Keep Key</button>
              </div>
            </div>
          ) : (
            <div className="tool-actions">
              {!custom && !refused ? (
                <button type="button" id="jev-key-open" onClick={() => jev.patch({ keyForm: true, outcome: null })}>{hasKey ? "Replace Key" : "Save Key"}</button>
              ) : null}
              {hasKey ? <button type="button" onClick={() => jev.patch({ confirmDelete: true, outcome: null })}>Delete Key…</button> : null}
            </div>
          )}
        </div>
      ) : null}

      {status && !custom && !refused && !view.testPreview ? (
        <div className="tool-field">
          <h4>Test connection</h4>
          <p className="muted">One fixed test request with no project data. It does not turn anything on.</p>
          <button type="button" aria-disabled={busy} onClick={() => {
            if (busy) return;
            jev.run({ kind: "prepare-test" }, (reply) =>
              reply.action === "prepare-test" && reply.ok && reply.status === "ready" && reply.token
                ? { testPreview: reply, testResult: null, outcome: { state: "ready", text: "Review the test. Nothing is sent yet." } }
                : { outcome: refusal(reply) },
            );
          }}>Review Connection Test</button>
        </div>
      ) : null}

      {view.testPreview ? (
        <Preview
          title="Send one test request"
          regionRef={testRegion}
          busy={busy}
          approveLabel="Send Test Request"
          onCancel={() => jev.patch({ testPreview: null, outcome: null })}
          onApprove={() => {
            const token = view.testPreview?.token;
            if (!token) return;
            jev.run({ kind: "test", token }, (reply) =>
              reply.ok && reply.status === "ok"
                ? {
                    testPreview: null,
                    testResult: reply as JevEnvelope<"test">,
                    outcome: { state: "saved", text: "The connection works" + (reply.source ? " (key from " + (reply.source === "device" ? "this device" : "the environment") + ")" : "") + ". Nothing was turned on." },
                  }
                : { testPreview: null, outcome: refusal(reply) },
            );
          }}
        >
          <p>
            Sends one fixed synthetic request to <code translate="no">{view.testPreview.destination?.url ?? "the official TypeSafe address"}</code>
            {view.testPreview.destination ? <> (model <code translate="no">{view.testPreview.destination.model}</code>)</> : null}.
            It holds no project data.
            {view.testPreview.timeout_seconds ? ` Network timeout is ${view.testPreview.timeout_seconds} seconds.` : ""}
          </p>
          <p className="muted">The provider may charge for it. This does not approve or turn on Jev.</p>
        </Preview>
      ) : null}

      <div className="tool-actions">
        <button type="button" onClick={jev.close}>Close Jev Controls</button>
      </div>
    </div>
  );
}
