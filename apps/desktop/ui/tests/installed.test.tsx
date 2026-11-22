import { afterEach, expect, it } from "vitest";
import { App } from "../src/App";
import type { InstalledSkillDto } from "../src/contract";
import { button, click, emit, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred, sampleProject } from "./support/fakeTransport";

afterEach(unmount);

it("reloads the local inventory and renders skill names as text", async () => {
  const transport = createFakeTransport();
  let skills: InstalledSkillDto[] = [];
  transport.override.skillsInstalled = async () => skills;
  await mount(<App transport={transport} />);
  await click(button("Installed"));
  expect(document.querySelector("main .empty")?.textContent).toContain("user folders");

  skills = [{ name: "<img src=x>", engine: "codex", scope: "user", location: "~/.agents/skills/<img src=x>" }];
  await click(button("Refresh inventory"));

  const rows = document.querySelector('ul[aria-label="Installed skills"]');
  expect(rows?.textContent).toContain(skills[0]?.name);
  expect(rows?.textContent).toContain("Codex · User");
  expect(rows?.textContent).toContain(skills[0]?.location);
  expect(rows?.querySelector("img")).toBeNull();
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
