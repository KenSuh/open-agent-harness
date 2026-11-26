import { afterEach, expect, it } from "vitest";
import { App } from "../src/App";
import type { InstalledSkillDto, RemoveInstalledSkillReq } from "../src/contract";
import { button, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleAgents, sampleProject } from "./support/fakeTransport";

afterEach(unmount);

it("asks before removing a shared path and keeps a separately installed copy visible", async () => {
  const transport = createFakeTransport();
  const location = "~/.agents/skills/tidy";
  const independent = "~/.config/opencode/skills/tidy";
  const requests: RemoveInstalledSkillReq[] = [];
  let skills: InstalledSkillDto[] = [
    { name: "tidy", engine: "codex", compatible_agents: ["opencode", "antigravity"], scope: "user", location },
    { name: "tidy", engine: "opencode", scope: "user", location: independent },
  ];
  transport.override.skillsInstalled = async () => skills;
  transport.override.inspectInstalledSkill = async (target) => ({
    location: target.location, revision: "shared-preview", agents: ["codex", "opencode", "antigravity"], source: null,
  });
  transport.override.removeInstalledSkill = async (request) => {
    requests.push(request);
    skills = skills.filter((skill) => skill.location !== request.target.location);
    return { archive: "~/.agents/skills/.oah-removed-skills/backup/tidy" };
  };

  await mount(<App transport={transport} />);
  await click(button("Installed"));
  await click(button("Manage…"));
  await click(button("Remove…"));
  const confirmation = document.querySelector('[aria-label="Confirm skill removal"]');
  expect(confirmation?.textContent).toContain("Remove this shared skill for all its agents?");
  expect(confirmation?.textContent).toContain("Codex, OpenCode, Antigravity");
  expect(confirmation?.textContent).toContain(location);
  expect(document.activeElement).toBe(button("Cancel"));
  await click(button("Cancel"));
  expect(requests).toEqual([]);

  await click(button("Remove…"));
  await click(button("Remove for all agents"));
  expect(requests).toEqual([{ target: { project: null, location }, revision: "shared-preview", confirm_shared: true }]);
  const rows = document.querySelector(".installed-list");
  expect(rows?.textContent).toContain(independent);
  expect(rows?.textContent).not.toContain(location);
  expect(document.querySelector(".installed")?.textContent).toContain("Recover it from");
});

it("reloads the local inventory and renders skill names as text", async () => {
  const transport = createFakeTransport();
  transport.agents = [sampleAgents.codexSignedOut];
  let skills: InstalledSkillDto[] = [];
  transport.override.skillsInstalled = async () => skills;
  await mount(<App transport={transport} />);
  await click(button("Installed"));
  await click(button("Codex 0"));
  expect(document.querySelector("main .empty")?.textContent).toBe("No Codex skills found in your user folders.");

  skills = [{ name: "<img src=x>", engine: "codex", scope: "user", location: "~/.agents/skills/<img src=x>" }];
  await click(button("Refresh inventory"));

  const rows = document.querySelector('ul[aria-label="Installed skills"]');
  expect(rows?.textContent).toContain(skills[0]?.name);
  expect(rows?.textContent).toContain("Codex · User");
  expect(rows?.textContent).toContain(skills[0]?.location);
  expect(rows?.querySelector("img")).toBeNull();
});

it("shows an agent's own skills first and identifies shared skills while keeping All in inventory order", async () => {
  const transport = createFakeTransport({ project: sampleProject });
  transport.agents = [{ ...sampleAgents.codexSignedOut, id: "antigravity", installed: false }];
  const shared = `${sampleProject.root}/.agents/skills/shared`;
  const openUser = "~/.config/opencode/skills/open-skill";
  const openProject = `${sampleProject.root}/.opencode/skills/project-skill`;
  const claudeUser = "~/.claude/skills/claude-skill";
  const gravity = "~/.gemini/config/skills/gravity-skill";
  const allLocations = [shared, openUser, claudeUser, openProject, gravity];
  transport.override.skillsInstalled = async () => [
    { name: "shared", engine: "codex", compatible_agents: ["opencode", "antigravity", "gemini", "copilot", "cursor"], scope: "project", location: shared },
    { name: "open-skill", engine: "opencode", scope: "user", location: openUser },
    { name: "claude-skill", engine: "claude", compatible_agents: ["opencode"], scope: "user", location: claudeUser },
    { name: "project-skill", engine: "opencode", scope: "project", location: openProject },
    { name: "gravity-skill", engine: "antigravity", scope: "user", location: gravity },
  ];
  await mount(<App transport={transport} />);
  await click(button("Installed"));

  const filterGroup = document.querySelector('[role="group"][aria-label="Filter installed skills by agent"]');
  const all = button("All 5");
  const open = button("OpenCode 4");
  const listing = () => [...document.querySelectorAll(".installed-list li code")].map((row) => row.textContent);
  expect(filterGroup?.contains(all)).toBe(true);
  expect(all.getAttribute("aria-pressed")).toBe("true");
  expect(listing()).toEqual(allLocations);

  open.focus();
  expect(document.activeElement).toBe(open);
  expect(open.tabIndex).toBe(0);
  await click(open);
  expect(open.getAttribute("aria-pressed")).toBe("true");
  expect(all.getAttribute("aria-pressed")).toBe("false");
  expect(listing()).toEqual([openUser, openProject, shared, claudeUser]);
  expect(document.querySelector(".installed-list")?.textContent).toContain("Shared from Claude Code · User");
  expect(document.querySelector(".installed-list")?.textContent).toContain("Shared from Codex · Project");

  await click(button("Antigravity 2"));
  expect(listing()).toEqual([gravity, shared]);
  await click(all);
  expect(listing()).toEqual(allLocations);
  expect(document.querySelector(".installed-list")?.textContent).not.toContain("Shared from");
});

it("offers installed agents that have no skills for the open project or user", async () => {
  const transport = createFakeTransport({ project: sampleProject });
  transport.agents = [
    { ...sampleAgents.codexSignedOut, id: "copilot", installed: true },
    { ...sampleAgents.codexSignedOut, id: "cursor", installed: false },
  ];
  transport.override.skillsInstalled = async () => [
    { name: "gravity-skill", engine: "antigravity", scope: "user", location: "~/.gemini/config/skills/gravity-skill" },
  ];
  await mount(<App transport={transport} />);
  await click(button("Installed"));
  expect(document.querySelector(".installed-filters")?.textContent).not.toContain("Cursor");
  await click(button("Copilot CLI 0"));
  expect(document.querySelector("main [role=status].empty")?.textContent)
    .toBe("No Copilot CLI skills found in this project or your user folders.");
  expect(document.querySelector(".installed-list")).toBeNull();
  await click(button("All 1"));
  expect(document.querySelector(".installed-list")?.textContent).toContain("Antigravity · User");
});

it("shows an inventory failure and retries from the same control", async () => {
  const transport = createFakeTransport();
  const pending = deferred<InstalledSkillDto[]>();
  transport.override.skillsInstalled = () => pending.promise;
  await mount(<App transport={transport} />);
  await click(button("Installed"));
  expect(button("Refresh inventory").disabled).toBe(true);

  await emit(() => pending.reject({ code: "OAH_FILE_IO", message: "Skill folder unavailable.", hint: null }));
  expect(document.querySelector('[role="alert"]')?.textContent).toContain("OAH_FILE_IO");
  expect(document.querySelector("main .empty")).toBeNull();

  transport.override.skillsInstalled = undefined;
  await click(button("Refresh inventory"));
  expect(document.querySelector('[role="alert"]')).toBeNull();
  expect(document.querySelector("main .empty")).not.toBeNull();
});

it("ignores an older inventory response when the selected project changes", async () => {
  const transport = createFakeTransport();
  const pending = deferred<InstalledSkillDto[]>();
  transport.override.skillsInstalled = () => pending.promise;
  await mount(<App transport={transport} />);
  await click(button("Installed"));

  transport.override.skillsInstalled = async () => [
    { name: "chosen-project-skill", engine: "claude", scope: "project", location: `${sampleProject.root}/.claude/skills/chosen-project-skill` },
  ];
  await click(button("+ Open…"));
  await emit(() => pending.resolve([{ name: "old-skill", engine: "codex", scope: "user", location: "~/.agents/skills/old-skill" }]));

  const rows = document.querySelector('ul[aria-label="Installed skills"]');
  expect(rows?.textContent).toContain("chosen-project-skill");
  expect(rows?.textContent).toContain("Claude Code · Project");
  expect(rows?.textContent).not.toContain("old-skill");
});
