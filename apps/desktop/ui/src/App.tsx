// 한국어: 레이아웃 A(타이틀 바 + 레일 + 본문 + 토스트) 최상위 화면. 이번 슬라이스에 없는 기능의 컨트롤은 만들지 않는다.
import { useEffect, useState } from "react";
import type { ErrorDto, ProjectDto, Theme } from "./contract";
import type { Transport } from "./transport/types";
import { AgentRoster, AgentsView } from "./agents";
import { TeamWorkflow } from "./workflow";
import { useController, type Load, type RefreshTask, type Toast } from "./useController";

type Page = "explore" | "installed" | "team-workflow";

/** Where the main area is: one of the section pages, or the Agents view (all agents, or one agent's detail). */
type View = { page: Page } | { page: "agents"; agent: string | null };

const PAGES: ReadonlyArray<{ id: Page; label: string; title: string; body: string }> = [
  {
    id: "explore",
    label: "Explore",
    title: "Explore",
    body: "Skill search is not available yet. Nothing to show.",
  },
  {
    id: "installed",
    label: "Installed",
    title: "Installed",
    body: "No skills are managed yet. Nothing is installed.",
  },
  {
    id: "team-workflow",
    label: "Team Workflow",
    title: "Team Workflow",
    body: "Open a project to choose which agent does each section. Nothing to show.",
  },
];

const THEMES: ReadonlyArray<{ value: Theme; label: string }> = [
  { value: "auto", label: "Auto" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

function projectName(project: ProjectDto): string {
  const parts = project.root.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? project.root;
}

function ErrorBox({ error, onRetry }: { error: ErrorDto; onRetry?: () => void }) {
  return (
    <div className="error-box" role="alert">
      <strong>{error.code}</strong>
      <p>{error.message}</p>
      {error.hint ? <p className="muted">{error.hint}</p> : null}
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

function RefreshStatus({ task, onCancel }: { task: RefreshTask; onCancel: (id: string) => void }) {
  if (task.state !== "running") return null;
  const progress = task.progress;
  const text = progress
    ? `Refreshing (${progress.phase}) ${progress.done}${progress.total === null ? "" : ` / ${progress.total}`}`
    : "Refreshing…";
  return (
    <div className="task-bar" role="status" aria-label="Refresh task">
      <span>{text}</span>
      <button type="button" disabled={task.id === null} onClick={() => task.id && onCancel(task.id)}>
        Cancel
      </button>
    </div>
  );
}

function ToastStack({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (key: number) => void }) {
  return (
    <div className="toast-stack" role="status" aria-live="polite" aria-atomic="false">
      {toasts.map((toast) => (
        <div key={toast.key} className={`toast toast-${toast.level}`}>
          <div>
            <span>{toast.text}</span>
            {toast.hint ? <span className="muted"> {toast.hint}</span> : null}
          </div>
          <button type="button" aria-label="Dismiss message" onClick={() => onDismiss(toast.key)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

function AppInfoLine({ info, onRetry }: { info: Load<import("./contract").AppInfoDto>; onRetry: () => void }) {
  if (info.status === "loading") return <p className="muted">Loading app information…</p>;
  if (info.status === "error") return <ErrorBox error={info.error} onRetry={onRetry} />;
  const { version, os, cli } = info.value;
  return (
    <p className="muted">
      Version {version} · {os} · CLI copy: {cli ? cli.state : "not run yet"}
      {cli?.version ? ` (oah ${cli.version})` : ""}
    </p>
  );
}

export function App({ transport }: { transport: Transport }) {
  const c = useController(transport);
  const [view, setView] = useState<View>({ page: "explore" });
  const page = view.page;
  const agentsSelected = view.page === "agents" ? view.agent : undefined;

  const theme: Theme = c.settings.status === "ready" ? c.settings.value.ui.theme : "auto";
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const { startRefresh } = c;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "r") {
        event.preventDefault(); // also stops the webview's own reload
        void startRefresh();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [startRefresh]);

  const current = PAGES.find((item) => item.id === page) ?? PAGES[0]!;
  const { agents } = c;
  const refreshing = c.refresh?.state === "running";
  const agentRows = agents.agents.status === "ready" ? agents.agents.value : [];
  const workflowKey = `${c.project?.id ?? ""}|${agentRows.map((a) => `${a.id}:${a.installed}:${a.sign_in}`).join(",")}`;
  const quickStartVisible = c.settings.status === "ready" && !c.settings.value.ui.quick_start_dismissed;

  return (
    <div className="app">
      <header className="titlebar">
        <span className="brand">Open Agent Harness</span>
        <span className="project-name" aria-live="polite">
          {c.project ? projectName(c.project) : "No project open"}
        </span>
        <div className="titlebar-actions">
          <button type="button" onClick={() => void c.openProject()} disabled={c.projectBusy}>
            {c.projectBusy ? "Opening…" : "+ Open…"}
          </button>
          <button
            type="button"
            onClick={() => void c.startRefresh()}
            disabled={!c.project || refreshing}
            aria-keyshortcuts="Control+R Meta+R"
            title="Refresh (Cmd/Ctrl+R)"
          >
            ↻ Refresh
          </button>
          <label className="theme-control">
            <span>Theme</span>
            <select
              value={theme}
              disabled={c.settings.status !== "ready"}
              onChange={(event) => void c.setTheme(event.target.value as Theme)}
            >
              {THEMES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </header>

      <div className="body">
        <div className="rail-column">
          <nav className="rail" aria-label="Sections">
            {PAGES.map((item) => (
              <button
                key={item.id}
                type="button"
                className={item.id === page ? "rail-item active" : "rail-item"}
                aria-current={item.id === page ? "page" : undefined}
                onClick={() => setView({ page: item.id })}
              >
                {item.label}
              </button>
            ))}
          </nav>
          <AgentRoster
            agents={agents.agents}
            checking={agents.checking}
            selected={agentsSelected}
            onOpen={(agent) => setView({ page: "agents", agent })}
            onReload={agents.reload}
          />
        </div>

        <main className="content">
          {c.settings.status === "error" ? <ErrorBox error={c.settings.error} onRetry={c.reloadSettings} /> : null}
          {quickStartVisible ? (
            <section className="quick-start" aria-label="Quick start">
              <span>Quick start: open a project folder with “+ Open…”, then press Cmd/Ctrl+R to re-read it.</span>
              <button type="button" onClick={() => void c.dismissQuickStart()}>
                Dismiss
              </button>
            </section>
          ) : null}
          {c.refresh ? <RefreshStatus task={c.refresh} onCancel={(id) => void c.cancelRefresh(id)} /> : null}
          {c.project ? (
            <section className="project-card" aria-label="Open project">
              <h2>{projectName(c.project)}</h2>
              <p className="muted">
                {c.project.root} · {c.project.kind === "git" ? "Git work tree" : "Plain folder"} · config:{" "}
                {c.project.config.state}
              </p>
              {c.project.config.state === "invalid" ? <ErrorBox error={c.project.config.error} /> : null}
            </section>
          ) : null}
          {c.notices.length > 0 ? (
            <ul className="notices" aria-label="Notices">
              {c.notices.map((notice) => (
                <li key={notice.id} className={`notice notice-${notice.level}`}>
                  <strong>{notice.code}</strong> {notice.message}
                  {notice.hint ? <span className="muted"> {notice.hint}</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
          {view.page === "agents" ? (
            <AgentsView
              agents={agents.agents}
              check={agents.check}
              checking={agents.checking}
              selected={view.agent}
              onRescan={() => void agents.rescan()}
              onCancel={(id) => void agents.cancelCheck(id)}
              onShowAll={() => setView({ page: "agents", agent: null })}
              onReload={agents.reload}
            />
          ) : (
            <section className="page" aria-labelledby="page-title">
              <h1 id="page-title">{current.title}</h1>
              {page === "team-workflow" && c.project ? (
                <TeamWorkflow transport={transport} reloadKey={workflowKey} />
              ) : (
                <p className="empty">{current.body}</p>
              )}
            </section>
          )}
          <AppInfoLine info={c.info} onRetry={c.reloadInfo} />
        </main>
      </div>

      <ToastStack toasts={c.toasts} onDismiss={c.dismissToast} />
    </div>
  );
}
