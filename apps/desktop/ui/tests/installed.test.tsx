import { afterEach, expect, it } from "vitest";
import { App } from "../src/App";
import type { InstalledSkillDto } from "../src/contract";
import { button, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleAgents, sampleProject } from "./support/fakeTransport";

afterEach(unmount);

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

it("filters the same inventory by agent with counts and a scoped empty state", async () => {
  const transport = createFakeTransport({ project: sampleProject });
  transport.agents = [{ ...sampleAgents.codexSignedOut, id: "antigravity", installed: false }];
  const shared = `${sampleProject.root}/.agents/skills/shared`;
  transport.override.skillsInstalled = async () => [
    { name: "shared", engine: "codex", compatible_agents: ["opencode", "antigravity", "gemini", "copilot", "cursor"], scope: "project", location: shared },
    { name: "open-skill", engine: "opencode", scope: "user", location: "~/.config/opencode/skills/open-skill" },
    { name: "gravity-skill", engine: "antigravity", scope: "user", location: "~/.gemini/config/skills/gravity-skill" },
  ];
  await mount(<App transport={transport} />);
  await click(button("Installed"));

  const filterGroup = document.querySelector('[role="group"][aria-label="Filter installed skills by agent"]');
  const all = button("All 3");
  const open = button("OpenCode 2");
  const listing = () => [...document.querySelectorAll(".installed-list li code")].map((row) => row.textContent);
  expect(filterGroup?.contains(all)).toBe(true);
  expect(all.getAttribute("aria-pressed")).toBe("true");
  expect(listing()).toEqual([shared, "~/.config/opencode/skills/open-skill", "~/.gemini/config/skills/gravity-skill"]);

  open.focus();
  expect(document.activeElement).toBe(open);
  expect(open.tabIndex).toBe(0);
  await click(open);
  expect(open.getAttribute("aria-pressed")).toBe("true");
  expect(all.getAttribute("aria-pressed")).toBe("false");
  expect(listing()).toEqual([shared, "~/.config/opencode/skills/open-skill"]);

  await click(button("Antigravity 2"));
  expect(listing()).toEqual([shared, "~/.gemini/config/skills/gravity-skill"]);
  await click(all);
  expect(listing()).toHaveLength(3);
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
