import { useEffect, useState } from "react";
import type { InstalledSkillDto } from "./contract";
import { toErrorDto, type Transport } from "./transport/types";
import type { Load } from "./useController";

export function InstalledView({ transport, projectOpen }: { transport: Transport; projectOpen: boolean }) {
  const [load, setLoad] = useState<Load<InstalledSkillDto[]>>({ status: "loading" });
  const [read, setRead] = useState(0);

  useEffect(() => {
    let current = true;
    transport.skillsInstalled().then(
      (value) => {
        if (current) setLoad({ status: "ready", value });
      },
      (error) => {
        if (current) setLoad({ status: "error", error: toErrorDto(error) });
      },
    );
    return () => {
      current = false;
    };
  }, [transport, read]);

  const refresh = () => {
    setLoad({ status: "loading" });
    setRead((previous) => previous + 1);
  };

  return (
    <div className="installed">
      <div className="installed-controls">
        <p className="muted">
          {projectOpen
            ? "Showing skills in this project and your user folders."
            : "Showing user skills. Open a project to see its skills."}
        </p>
        <button type="button" onClick={refresh} disabled={load.status === "loading"}>
          Refresh inventory
        </button>
      </div>
      <details className="agent-card installed-help">
        <summary>Manage skills with the source CLI</summary>
        <p>
          Get the full Python CLI with <code>install.sh</code> (macOS) or <code>install.ps1</code> (Windows) from the OAH release.
          Run the commands below from your project folder. Updates apply to OAH toolkit copies.
        </p>
        <dl className="agent-facts">
          <dt>Install in this project</dt>
          <dd><code>oah skills install NAME</code></dd>
          <dt>Install for your user</dt>
          <dd><code>oah skills install NAME --global</code></dd>
          <dt>Update project copies</dt>
          <dd><code>oah skills update --project</code></dd>
          <dt>Update user copies</dt>
          <dd><code>oah skills update --global</code></dd>
        </dl>
        <p>
          If <code>oah</code> runs the desktop helper, use <code>uv tool run --from "/path/to/open-agent-harness" oah</code>
          {" "}in place of <code>oah</code>. Use the absolute path to your OAH source checkout or unpacked source archive.
        </p>
      </details>
      {load.status === "loading" ? <p className="muted" role="status">Reading installed skills…</p> : null}
      {load.status === "error" ? (
        <div className="error-box" role="alert">
          <strong>{load.error.code}</strong>
          <p>{load.error.message}</p>
          {load.error.hint ? <p>{load.error.hint}</p> : null}
        </div>
      ) : null}
      {load.status === "ready" && (load.value.length === 0 ? (
        <p className="empty" role="status">
          {projectOpen ? "No skills installed in this project or your user folders." : "No skills installed in your user folders."}
        </p>
      ) : (
        <ul className="installed-list" aria-label="Installed skills">
          {load.value.map((skill, index) => (
            <li className="agent-card" key={`${skill.scope}:${skill.location}:${index}`}>
              <div className="installed-heading">
                <strong>{skill.name}</strong>
                <span className="muted">
                  {skill.engine === "claude" ? "Claude Code" : "Codex"} · {skill.scope === "project" ? "Project" : "User"}
                </span>
              </div>
              <code className="muted">{skill.location}</code>
            </li>
          ))}
        </ul>
      ))}
    </div>
  );
}
