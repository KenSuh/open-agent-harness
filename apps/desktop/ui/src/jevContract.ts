// 한국어: Jev 기기 동의·키·연결 점검의 단일 호출 계약 — `project_jev_action` 의 요청(엄격한 태그 유니온)과 응답(액션 태그 봉투) 및 응답 검증기. 응답 형식에 키 값을 담을 자리가 없다.

/** The six actions that carry no value, the two that carry a preview token, and the one that carries the key. */
export type JevPlainKind = "status" | "prepare" | "disable" | "key-state" | "delete-key" | "prepare-test";
export type JevTokenKind = "enable" | "test";
export type JevAction = JevPlainKind | JevTokenKind | "store-key";

/** `project_jev_action`'s `input`. Only `store-key` can carry a key, and only that invocation ever does. */
export type JevInput =
  | { kind: JevPlainKind }
  | { kind: JevTokenKind; token: string }
  | { kind: "store-key"; key: string };

/** The one argument `req` of the command `project_jev_action`. */
export interface JevActionReq {
  readonly project_id: string;
  readonly input: JevInput;
}

/** Where a request would go. Text only: it is never a link or an address the UI can open. */
export interface JevDestination {
  readonly kind: "official" | "custom" | "refused";
  readonly url: string;
  readonly model: string;
}

export type JevConsent = "none" | "enabled" | "disabled" | "consent-stale" | "device-state";
export type JevDeviceKey = "present" | "missing" | "locked" | "denied" | "unsupported" | "error";

/** What one approval scope sends, as core words it; `state` appears on a status answer only. */
export interface JevScope {
  readonly scope: string;
  readonly title?: string;
  readonly sends?: readonly string[];
  readonly labels?: readonly string[];
  readonly never?: readonly string[];
  readonly bounds?: Readonly<Record<string, string | number>>;
  readonly state?: "granted" | "consent-stale";
}

export interface JevKeyState {
  readonly environment: boolean;
  readonly device: JevDeviceKey;
}

/** The optional safe fields of an answer. There is no field that can hold a key. */
export interface JevFields {
  readonly message?: string;
  readonly destination?: JevDestination;
  readonly consent?: JevConsent;
  readonly scopes?: readonly JevScope[];
  readonly token?: string;
  readonly key_state?: JevKeyState;
  readonly source?: "environment" | "device";
  readonly requests?: number;
  readonly timeout_seconds?: number;
}

/** One answer: the project and the action it answers, whether it succeeded, a status word and the safe fields. */
export interface JevEnvelope<A extends JevAction = JevAction> extends JevFields {
  readonly project_id: string;
  readonly action: A;
  readonly ok: boolean;
  /** A fixed word such as "ready", "enabled", "no-consent", "added" or "timeout"; core defines the set. */
  readonly status: string;
}

export type JevReply = { [A in JevAction]: JevEnvelope<A> }[JevAction];

const ACTIONS: readonly JevAction[] = [
  "status", "prepare", "disable", "key-state", "delete-key", "prepare-test", "enable", "test", "store-key",
];
const FIELD_NAMES = [
  "project_id", "action", "ok", "status", "message", "destination", "consent", "scopes", "token", "key_state",
  "source", "requests", "timeout_seconds",
];
const CONSENTS = ["none", "enabled", "disabled", "consent-stale", "device-state"];
const DEVICE_KEYS = ["present", "missing", "locked", "denied", "unsupported", "error"];

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isText = (value: unknown): value is string => typeof value === "string";
const isTexts = (value: unknown): value is string[] => Array.isArray(value) && value.every(isText);

function scopeOf(value: unknown): JevScope | null {
  if (!isRecord(value) || !isText(value.scope)) return null;
  const out: { -readonly [K in keyof JevScope]: JevScope[K] } = { scope: value.scope };
  if (value.title !== undefined) { if (!isText(value.title)) return null; out.title = value.title; }
  for (const name of ["sends", "labels", "never"] as const) {
    const list = value[name];
    if (list !== undefined) { if (!isTexts(list)) return null; out[name] = list; }
  }
  if (value.bounds !== undefined) {
    if (!isRecord(value.bounds) || !Object.values(value.bounds).every((v) => isText(v) || typeof v === "number")) return null;
    out.bounds = value.bounds as Record<string, string | number>;
  }
  if (value.state !== undefined) {
    if (value.state !== "granted" && value.state !== "consent-stale") return null;
    out.state = value.state;
  }
  return out;
}

/**
 * The only way an answer reaches the screen: a record of known field names with the right types, else `null`.
 * A field this contract does not name (a key, for one) makes the whole answer unusable, so it is never kept.
 */
export function parseJevReply(raw: unknown): JevReply | null {
  if (!isRecord(raw)) return null;
  if (Object.keys(raw).some((name) => !FIELD_NAMES.includes(name))) return null;
  if (!isText(raw.project_id) || !ACTIONS.includes(raw.action as JevAction)) return null;
  if (typeof raw.ok !== "boolean" || !isText(raw.status)) return null;
  const out: { -readonly [K in keyof JevFields]: JevFields[K] } & { project_id: string; action: JevAction; ok: boolean; status: string } = {
    project_id: raw.project_id, action: raw.action as JevAction, ok: raw.ok, status: raw.status,
  };
  if (raw.message !== undefined) { if (!isText(raw.message)) return null; out.message = raw.message; }
  if (raw.destination !== undefined) {
    const dest = raw.destination;
    if (!isRecord(dest) || !isText(dest.url) || !isText(dest.model)) return null;
    if (dest.kind !== "official" && dest.kind !== "custom" && dest.kind !== "refused") return null;
    out.destination = { kind: dest.kind, url: dest.url, model: dest.model };
  }
  if (raw.consent !== undefined) {
    if (!isText(raw.consent) || !CONSENTS.includes(raw.consent)) return null;
    out.consent = raw.consent as JevConsent;
  }
  if (raw.scopes !== undefined) {
    if (!Array.isArray(raw.scopes)) return null;
    const scopes = raw.scopes.map(scopeOf);
    if (scopes.some((s) => s === null)) return null;
    out.scopes = scopes as JevScope[];
  }
  if (raw.token !== undefined) { if (!isText(raw.token)) return null; out.token = raw.token; }
  if (raw.key_state !== undefined) {
    const ks = raw.key_state;
    if (!isRecord(ks) || typeof ks.environment !== "boolean" || !isText(ks.device) || !DEVICE_KEYS.includes(ks.device)) return null;
    out.key_state = { environment: ks.environment, device: ks.device as JevDeviceKey };
  }
  if (raw.source !== undefined) {
    if (raw.source !== "environment" && raw.source !== "device") return null;
    out.source = raw.source;
  }
  for (const name of ["requests", "timeout_seconds"] as const) {
    const n = raw[name];
    if (n !== undefined) { if (typeof n !== "number" || !Number.isFinite(n)) return null; out[name] = n; }
  }
  return out as JevReply;
}
