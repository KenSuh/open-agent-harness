// 한국어: 레일 에이전트 목록(설치됨·접힌 미설치 묶음)과 읽기 전용 에이전트 화면. 코어 상태만 보이고 설치·로그인은 하지 않는다.
import type { AgentId, AgentStatus, AgentStatusDto, Capabilities, CapabilityState } from "./contract";
import type { AgentCheck, AgentsLoad } from "./useAgents";

const STATUS_LABEL: Record<AgentStatus, string> = {
  ok: "Ready",
  missing: "Not installed",
  "too-old": "Too old",
  "not-signed-in": "Not signed in",
  "sign-in-unknown": "Sign-in unknown",
  "unsupported-shim": "Unsupported launcher",
  error: "Check failed",
};

/** The rail's small sign-in mark (§6.3): a symbol plus its spoken label. */
const MARK: Record<AgentStatus, { symbol: string; label: string; tone: string }> = {
  ok: { symbol: "✓", label: "Signed in", tone: "good" },
  "not-signed-in": { symbol: "✕", label: "Not signed in", tone: "bad" },
  "sign-in-unknown": { symbol: "?", label: "Sign-in unknown", tone: "neutral" },
  "too-old": { symbol: "!", label: "Too old", tone: "bad" },
  error: { symbol: "!", label: "Check failed", tone: "bad" },
  missing: { symbol: "–", label: "Not installed", tone: "neutral" },
  "unsupported-shim": { symbol: "!", label: "Unsupported launcher", tone: "bad" },
};

const CAPABILITIES: ReadonlyArray<{ key: keyof Capabilities; label: string }> = [
  { key: "detect", label: "Detect install" },
  { key: "sign_in", label: "Check sign-in" },
  { key: "skills_link", label: "Skill links" },
  { key: "native_switch", label: "Native skill switch" },
  { key: "model_read", label: "Read model" },
  { key: "model_write", label: "Set model" },
  { key: "hook_prompt", label: "Prompt hook" },
  { key: "pack_update", label: "Plugin pack updates" },
];

/** `unknown` is the neutral "not checked" state of §4.1: OAH never acts on it. */
const CAPABILITY_LABEL: Record<CapabilityState, string> = {
  verified: "Verified",
  unknown: "Not checked",
  none: "Not supported",
};

const OPENCODE_STATUS: Partial<Record<keyof Capabilities, string>> = {
  sign_in: "Live sign-in verification unavailable",
  skills_link: "Automatic linking unverified",
  native_switch: "Skill permission changes unverified",
  model_read: "Default model unavailable",
  model_write: "Model changes unverified",
  hook_prompt: "Prompt hook integration unverified",
  pack_update: "OpenCode plugin updates are managed in OpenCode",
};

function capabilityLabel(agent: AgentStatusDto, key: keyof Capabilities): string {
  const state = agent.capabilities[key];
  if (agent.id !== "opencode" || !agent.installed || state !== "unknown") return CAPABILITY_LABEL[state];
  if (key === "model_read" && agent.opencode?.default_model) return "Default model read";
  if (key === "sign_in" && agent.opencode?.saved_connections != null) return "Saved connections checked";
  return OPENCODE_STATUS[key] ?? CAPABILITY_LABEL[state];
}

function SignInMark({ status }: { status: AgentStatus }) {
  const mark = MARK[status];
  return (
    <span className={`sign-in-mark mark-${mark.tone}`} role="img" aria-label={mark.label} title={mark.label}>
      {mark.symbol}
    </span>
  );
}

function isHttpsUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.username === "" && parsed.password === "";
  } catch {
    return false;
  }
}

function hasModifier(event: { metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; altKey: boolean }): boolean {
  return event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
}

/**
 * Official install link (M1-S2 PACKET §9, AC1). Rendered as an ordinary link without `target="_blank"` so activation
 * goes through Tauri's native `on_navigation` policy, which checks the seven canonical catalog URLs, delegates the
 * click to the OS default browser, and cancels webview navigation so OAH stays on the current view. Modified clicks,
 * middle-clicks, and modified Enter are normalized onto the same single-window activation path so the webview does not
 * swallow them as denied popups or downloads.
 */
function InstallUrl({ name, url }: { name: string; url: string }) {
  if (!isHttpsUrl(url)) {
    return <span className="install-url">{url}</span>;
  }
  return (
    <a
      className="install-url"
      href={url}
      rel="noopener noreferrer"
      aria-label={`Install ${name} (${url}) — opens in your browser`}
      title={`Open ${name} install page in your browser`}
      onClick={(event) => {
        if (hasModifier(event)) {
          event.preventDefault();
          event.currentTarget.click();
        }
      }}
      onAuxClick={(event) => {
        if (event.button === 1) {
          event.preventDefault();
          event.currentTarget.click();
        }
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" && hasModifier(event)) {
          event.preventDefault();
          event.currentTarget.click();
        }
      }}
    >
      {url}
    </a>
  );
}

export interface RosterProps {
  agents: AgentsLoad;
  checking: boolean;
  /** The agent whose detail is shown; `null` for the whole list. Only meaningful while the Agents page is open. */
  selected: string | null | undefined;
  onOpen: (agent: string | null) => void;
  onReload: () => void;
}

function RosterBody({ agents, checking, selected, onOpen, onReload }: RosterProps) {
  if (agents.status === "loading") return <p className="roster-note">Loading agents…</p>;
  if (agents.status === "error") {
    return (
      <div className="roster-note roster-error">
        <span>
          {agents.error.code}: {agents.error.message}
        </span>
        <button type="button" onClick={onReload}>
          Reload agents
        </button>
      </div>
    );
  }
  if (agents.value.length === 0) {
    return <p className="roster-note">{checking ? "Checking agents…" : "No agent check has run yet."}</p>;
  }
  const installed = agents.value.filter((agent) => agent.installed);
  const missing = agents.value.filter((agent) => !agent.installed);
  return (
    <>
      {installed.length > 0 ? (
        <ul className="roster-list" aria-label={`Installed agents (${installed.length})`}>
          {installed.map((agent) => (
            <li key={agent.id}>
              <button
                type="button"
                className={selected === agent.id ? "rail-item roster-item active" : "rail-item roster-item"}
                aria-current={selected === agent.id ? "page" : undefined}
                onClick={() => onOpen(agent.id)}
              >
                <span>{agent.name}</span>
                <SignInMark status={agent.status} />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="roster-note">No supported agent is installed.</p>
      )}
      {missing.length > 0 ? (
        <details className="not-installed">
          <summary>Not installed ({missing.length})</summary>
          <ul>
            {missing.map((agent) => (
              <li key={agent.id}>
                <span className="not-installed-name">
                  {agent.name}
                  {agent.status !== "missing" ? <span className="muted"> · {STATUS_LABEL[agent.status]}</span> : null}
                </span>
                <InstallUrl name={agent.name} url={agent.install_url} />
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </>
  );
}

export function AgentRoster(props: RosterProps) {
  const active = props.selected === null;
  return (
    <section className="roster" aria-label="Agents">
      <button
        type="button"
        className={active ? "rail-item active" : "rail-item"}
        aria-current={active ? "page" : undefined}
        onClick={() => props.onOpen(null)}
      >
        Agents{props.checking ? <span className="muted"> · checking</span> : null}
      </button>
      <RosterBody {...props} />
    </section>
  );
}

function signInText(agent: AgentStatusDto): string {
  const saved = agent.opencode?.saved_connections;
  if (saved === 0) return "No saved provider connections. OpenCode can also use local models or credentials from the environment.";
  if (saved != null) return `${saved} saved provider ${saved === 1 ? "connection" : "connections"}. Live sign-in is unverified.`;
  if (agent.id === "opencode" && agent.installed) {
    return "Saved provider connections could not be read. Use /connect in OpenCode to check.";
  }
  if (agent.status === "sign-in-unknown") return `OAH cannot check sign-in for ${agent.name}.`;
  if (!agent.installed) return "Not checked: the agent is not installed.";
  switch (agent.sign_in) {
    case "signed-in":
      return "Signed in";
    case "signed-out":
      return "Signed out";
    case "unknown":
      return "Not checked";
  }
}

function AgentDetail({ agent, onViewSkills }: { agent: AgentStatusDto; onViewSkills: (id: AgentId) => void }) {
  const saved = agent.opencode?.saved_connections;
  const statusLabel = saved == null ? STATUS_LABEL[agent.status] : saved > 0 ? "Connections found" : "No saved connections";
  return (
    <article className="agent-card" aria-labelledby={`agent-${agent.id}`}>
      <header className="agent-card-head">
        <h2 id={`agent-${agent.id}`}>{agent.name}</h2>
        <span className={`agent-status status-${agent.status}`}>{statusLabel}</span>
      </header>
      <dl className="agent-facts">
        <dt>Sign-in</dt>
        <dd>{signInText(agent)}</dd>
        <dt>How to sign in</dt>
        <dd>{agent.login_hint.is_command ? <code>{agent.login_hint.text}</code> : agent.login_hint.text}</dd>
        <dt>Version</dt>
        <dd>{agent.version ?? "—"}</dd>
        {agent.opencode?.default_model ? (
          <>
            <dt>Default model</dt>
            <dd><code>{agent.opencode.default_model}</code></dd>
          </>
        ) : null}
        <dt>Found at</dt>
        <dd>{agent.source ?? "—"}</dd>
        <dt>Kind</dt>
        <dd>{agent.kind === "ide" ? "App (IDE)" : "Command-line tool"}</dd>
        <dt>Can take a section</dt>
        <dd>{agent.can_take_section ? "Yes" : "No (skills and rules only)"}</dd>
        <dt>Checked</dt>
        <dd>
          <time dateTime={agent.checked_at}>{agent.checked_at}</time>
        </dd>
        {agent.installed && agent.id === "opencode" ? (
          <>
            <dt>Skills</dt>
            <dd>
              <button type="button" className="link-button" aria-label={`View ${agent.name} installed skills`}
                onClick={() => onViewSkills(agent.id)}>
                View installed skills
              </button>
            </dd>
          </>
        ) : null}
        {agent.installed ? null : (
          <>
            <dt>Install page</dt>
            <dd>
              <InstallUrl name={agent.name} url={agent.install_url} />
            </dd>
          </>
        )}
      </dl>
      {agent.problem ? (
        <div className="agent-problem">
          <strong>{agent.problem.code}</strong>
          <p>{agent.problem.message}</p>
          {agent.problem.hint ? <p className="muted">{agent.problem.hint}</p> : null}
        </div>
      ) : null}
      <table className="capabilities">
        <caption>What OAH can do with {agent.name} on this computer</caption>
        <tbody>
          {CAPABILITIES.map(({ key, label }) => (
            <tr key={key}>
              <th scope="row">{label}</th>
              <td className={`capability-${agent.capabilities[key]}`}>{capabilityLabel(agent, key)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  );
}

function CheckBar({ check, onCancel }: { check: AgentCheck; onCancel: (id: string) => void }) {
  const progress = check.progress;
  const text = progress
    ? `Checking agents (${progress.phase}) ${progress.done}${progress.total === null ? "" : ` / ${progress.total}`}`
    : "Checking agents…";
  return (
    <div className="task-bar" role="status" aria-label="Agent check">
      <span>{text}</span>
      <button type="button" disabled={check.id === null} onClick={() => check.id && onCancel(check.id)}>
        Cancel check
      </button>
    </div>
  );
}

export interface AgentsViewProps {
  agents: AgentsLoad;
  check: AgentCheck | null;
  checking: boolean;
  selected: string | null;
  onRescan: () => void;
  onCancel: (id: string) => void;
  onShowAll: () => void;
  onReload: () => void;
  onViewSkills: (id: AgentId) => void;
}

export function AgentsView({ agents, check, checking, selected, onRescan, onCancel, onShowAll, onReload, onViewSkills }: AgentsViewProps) {
  const ownCheckRunning = check?.state === "running";
  const list = agents.status === "ready" ? agents.value : [];
  const shown = selected === null ? list : list.filter((agent) => agent.id === selected);
  return (
    <section className="page agents-page" aria-labelledby="page-title">
      <div className="agents-head">
        <h1 id="page-title">Agents</h1>
        <button type="button" onClick={onRescan} disabled={ownCheckRunning}>
          ↻ Rescan agents
        </button>
      </div>
      <p className="muted">
        Read-only. OAH checks installed agents and reports the connection details each agent provides.
        Coming back to this window checks again.
      </p>
      {check && ownCheckRunning ? <CheckBar check={check} onCancel={onCancel} /> : null}
      {agents.status === "loading" ? <p className="agents-note">Loading agents…</p> : null}
      {agents.status === "error" ? (
        <div className="agent-problem">
          <strong>{agents.error.code}</strong>
          <p>{agents.error.message}</p>
          {agents.error.hint ? <p className="muted">{agents.error.hint}</p> : null}
          <button type="button" onClick={onReload}>
            Reload agents
          </button>
        </div>
      ) : null}
      {agents.status === "ready" && list.length === 0 ? (
        <p className="agents-note">{checking ? "Checking agents…" : "No agent check has run yet. Use Rescan agents."}</p>
      ) : null}
      {selected !== null && agents.status === "ready" && list.length > 0 ? (
        <button type="button" className="link-button" onClick={onShowAll}>
          ← All agents
        </button>
      ) : null}
      {selected !== null && agents.status === "ready" && list.length > 0 && shown.length === 0 ? (
        <p className="agents-note">This agent has not been checked.</p>
      ) : null}
      {shown.map((agent) => (
        <AgentDetail key={agent.id} agent={agent} onViewSkills={onViewSkills} />
      ))}
    </section>
  );
}
