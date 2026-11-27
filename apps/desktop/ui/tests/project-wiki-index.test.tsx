// 한국어: 로컬 Wiki 색인 컨트롤 테스트 — 접힌 채 호출 0, 명시 클릭만 요청, 일치하는 결과만 표시, 프로젝트 전환·늦은 응답 폐기 (독립 테스트 전송 계층).
import { useState } from "react";
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { WikiIndexReply } from "../src/contract";
import { ProjectWikiIndex } from "../src/project-wiki-index";
import { button, click, emit, flush, mount, unmount } from "./support/dom";
import { createFakeTransport, sampleProject, type FakeTransport } from "./support/fakeTransport";

afterEach(async () => {
  await unmount();
});

const reply = (extra: Partial<WikiIndexReply> & Pick<WikiIndexReply, "action">, project = "p1"): WikiIndexReply => ({
  project_id: project,
  ok: true,
  state: "ready",
  ...extra,
});
const done = (t: FakeTransport, data: WikiIndexReply) => emit(() => t.emitWikiIndex({ event: "done", data }));
const feedback = () => document.querySelector(".wiki-index-feedback")?.textContent ?? "";
const buttons = () => Array.from(document.querySelectorAll(".wiki-index button")).map((b) => b.textContent);

let setProject: (id: string) => void = () => undefined;
function Harness({ t }: { t: FakeTransport }) {
  const [id, set] = useState("p1");
  setProject = set;
  return <ProjectWikiIndex key={id} transport={t} projectId={id} />;
}

async function openIndex(t = createFakeTransport()) {
  await mount(<Harness t={t} />);
  await click(button("Local Wiki Index"));
  await done(t, reply({ action: "status", state: "not_built", source: { commit: "0123456789abcdef", notes: 12 } }));
  return t;
}

describe("Local Wiki Index controls", () => {
  it("is collapsed on project open and makes no call until the person opens it", async () => {
    const t = createFakeTransport({ project: sampleProject });
    await mount(<App transport={t} />);
    await click(button("Team Workflow"));
    expect(t.calls).not.toContain("start_wiki_index");
    expect(document.querySelector(".wiki-index-toggle")?.getAttribute("aria-expanded")).toBe("false");
    await click(button("Local Wiki Index"));
    expect(t.wikiIndexRequests).toEqual([{ project_id: sampleProject.id, input: { action: "status" } }]);
    expect(document.querySelector(".wiki-index-toggle")?.getAttribute("aria-expanded")).toBe("true");
  });

  it("shows the saved state and only offers the four typed actions, with no install, download or approval control", async () => {
    await openIndex();
    expect(document.querySelector(".wiki-index-state")?.textContent).toContain("No local index has been built yet.");
    expect(document.querySelector(".wiki-index-state")?.textContent).toContain("committed snapshot 0123456789ab, 12 notes");
    expect(buttons()).toEqual(["Local Wiki Index", "Check Tools", "Build Index", "Search"]);
    expect(document.querySelector(".wiki-index input")?.getAttribute("type")).toBe("search");
  });

  it("checks tools on request and reports only the matching result", async () => {
    const t = await openIndex();
    await click(button("Check Tools"));
    expect(t.wikiIndexRequests.at(-1)?.input).toEqual({ action: "probe" });
    // A build answer cannot be shown as the tool check.
    await done(t, reply({ action: "build", message: "built" }));
    expect(feedback()).toContain("did not finish");
    expect(feedback()).not.toContain("built");
    await click(button("Check Tools"));
    await done(t, reply({ action: "probe", model: { name: "bge-m3", dimensions: 1024 } }));
    expect(feedback()).toContain("The local tools are ready.");
    expect(feedback()).toContain("model bge-m3 (1024 dimensions)");
  });

  it("gives the concrete next step when Ollama is offline and returns to Wiki Search", async () => {
    const t = createFakeTransport();
    let used = 0;
    await mount(<ProjectWikiIndex transport={t} projectId="p1" onUseWikiSearch={() => { used += 1; }} />);
    await click(button("Local Wiki Index"));
    await done(t, reply({ action: "status", state: "not_built" }));
    await click(button("Check Tools"));
    await done(t, reply({ action: "probe", ok: false, state: "not_built", code: "OAH_WIKI_INDEX_OLLAMA_DOWN", message: "Ollama did not answer." }));
    expect(document.querySelector(".wiki-index-feedback")?.getAttribute("role")).toBe("alert");
    expect(feedback()).toContain("Ollama did not answer.");
    expect(feedback()).toContain("Start Ollama on this computer");
    await click(button("Use Wiki Search"));
    expect(used).toBe(1);
  });

  it("builds with progress, stops through the task, and refreshes only from a ready index", async () => {
    const t = await openIndex();
    await click(button("Build Index"));
    expect(t.wikiIndexRequests.at(-1)?.input).toEqual({ action: "build" });
    await emit(() => t.emitWikiIndex({ event: "progress", data: { phase: "embed", done: 3, total: 10 } }));
    expect(feedback()).toContain("embed 3/10");
    await click(button("Stop"));
    expect(t.calls).toContain("task_cancel:wiki-index-2");
    await emit(() => t.emitWikiIndex({ event: "cancelled" }));
    expect(feedback()).toContain("Index build was stopped.");
    await click(button("Build Index"));
    await done(t, reply({ action: "build", state: "ready", source: { commit: "fedcba9876543210", notes: 12 } }));
    expect(feedback()).toContain("The local index was built.");
    expect(document.querySelector(".wiki-index-state")?.textContent).toContain("The local index is current.");
    expect(buttons()).toContain("Refresh Index");
  });

  it("does not start a second run while one is active", async () => {
    const t = await openIndex();
    await click(button("Build Index"));
    await click(button("Check Tools"));
    await click(button("Build Index"));
    expect(t.wikiIndexRequests.map((r) => r.input.action)).toEqual(["status", "build"]);
  });

  it("searches with only the query and shows title, project-relative file, line, snippet and the text notice", async () => {
    const t = await openIndex();
    const input = document.querySelector<HTMLInputElement>(".wiki-index input")!;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "  retry policy ");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(button("Search"));
    expect(t.wikiIndexRequests.at(-1)).toEqual({ project_id: "p1", input: { action: "search", query: "retry policy" } });
    await done(
      t,
      reply({
        action: "search",
        mode: "text",
        message: "The local index is out of date, so these are text matches.",
        results: [{ file: "docs/wiki/retry.md", line: 14, title: "Retry policy", heading: "Backoff", snippet: "Retries use backoff." }],
        truncated: true,
      }),
    );
    const results = document.querySelector(".wiki-index-results")!;
    expect(results.textContent).toContain("The local index is out of date, so these are text matches.");
    expect(results.querySelector("strong")?.textContent).toBe("Retry policy");
    expect(results.querySelector("code")?.textContent).toBe("docs/wiki/retry.md:14");
    expect(results.textContent).toContain("Retries use backoff.");
    expect(results.textContent).toContain("Only the first results are shown.");
  });

  it("drops answers for another project and answers that arrive after a project switch", async () => {
    const t = await openIndex();
    await click(button("Check Tools"));
    await done(t, reply({ action: "probe", model: { name: "zzmodel" } }, "p2"));
    expect(feedback()).toContain("another project");
    expect(feedback()).not.toContain("zzmodel");
    await click(button("Check Tools"));
    await emit(() => setProject("p2"));
    expect(buttons()).toEqual(["Local Wiki Index"]);
    const before = t.wikiIndexRequests.length;
    await done(t, reply({ action: "probe", model: { name: "late" } }));
    expect(document.body.textContent).not.toContain("late");
    expect(t.wikiIndexRequests).toHaveLength(before);
    await flush();
  });
});
