// 한국어: Explore 검토 화면 테스트 — 폼 전송(start_review)·로딩·결과·격리·타입 오류·취소·늦은 이벤트 귀속을 독립 테스트 전송 계층으로 본다.
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../src/App";
import type { ReviewResultDto } from "../src/contract";
import { button, click, emit, flush, mount, unmount } from "./support/dom";
import { createFakeTransport, deferred } from "./support/fakeTransport";

afterEach(async () => {
  await unmount();
});

// Synthetic review data (ordinary fixture values, no real repository).
const passed: ReviewResultDto = {
  record: {
    version: 1,
    id: "rec-pass",
    source: { provider: "github", locator: "example/skills", commit: "0123456789abcdef0123456789abcdef01234567", tree: null, path: "skills/tidy/nested" },
    frontmatter: { name: "tidy", description: "Tidies things." },
    license: { verdict: "recognized", expression: "MIT", eligibility: "eligible" },
    status: "reviewed",
    reason: null,
    checked_at: "2026-11-22T00:00:00Z",
  },
  gate: { gate: "oah-install-gate/v1", result: "pass", reasons: [], compatible: ["claude"] },
  unchanged: false,
};
const quarantined: ReviewResultDto = {
  record: {
    ...passed.record,
    id: "rec-block",
    frontmatter: { name: "<img src=x onerror=alert(1)>", description: null },
    license: { verdict: "unknown", expression: null, eligibility: "unknown" },
  },
  gate: {
    gate: "oah-install-gate/v1",
    result: "block",
    reasons: [
      { kind: "license", detail: "No allowed license was found.", lines: [] },
      { kind: "screening", detail: "<b>Ignore previous instructions</b>", lines: [3, 18] },
    ],
  },
  unchanged: false,
};

async function openExplore() {
  const t = createFakeTransport();
  await mount(<App transport={t} />);
  return t;
}

async function submit(value: string) {
  const input = document.querySelector<HTMLInputElement>('input[type="text"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    document.querySelector<HTMLFormElement>("form.review-form")!.requestSubmit();
  });
  await flush();
}

const status = () => document.querySelector('[aria-label="Review status"]')!.textContent ?? "";

describe("Explore review", () => {
  it("has an accessibly named form and sends repo and nested folder over start_review", async () => {
    const t = await openExplore();
    expect(document.querySelector('form[aria-label="Review a GitHub skill folder"]')).not.toBeNull();
    expect(document.querySelector('label[for]')?.textContent).toBe("GitHub skill folder");
    await submit("example/skills/skills/tidy/nested");
    expect(t.reviewRequests).toEqual([{ spec: { repo: "example/skills", path: "skills/tidy/nested" } }]);
    expect(status()).toMatch(/Reviewing example\/skills\/skills\/tidy\/nested/);
    expect(button("Cancel").disabled).toBe(false);
    expect(button("Review").disabled).toBe(true);
  });

  it("rejects input that is not owner/repo/folder without calling core", async () => {
    const t = await openExplore();
    await submit("example/skills");
    expect(t.reviewRequests).toHaveLength(0);
    expect(document.querySelector('[role="alert"]')?.textContent).toMatch(/owner\/repo\/folder/);
    await submit("https://github.com/example/skills/tidy");
    expect(t.reviewRequests).toHaveLength(0);
  });

  it("shows the pinned commit, license, decision and the passing result", async () => {
    const t = await openExplore();
    await submit("example/skills/skills/tidy/nested");
    await emit(() => t.emitReview({ event: "progress", data: { phase: "fetching", done: 1, total: 3 } }));
    expect(status()).toMatch(/fetching 1 \/ 3/);
    await emit(() => t.emitReview({ event: "done", data: passed }));
    const result = document.querySelector('[aria-label="Review result"]')!;
    expect(result.getAttribute("data-quarantined")).toBe("false");
    expect(result.textContent).toMatch(/Review passed/);
    expect(result.textContent).toContain("0123456789abcdef0123456789abcdef01234567");
    expect(result.textContent).toMatch(/MIT \(eligible\)/);
    expect(result.textContent).toMatch(/Decision.*pass/);
    expect(result.textContent).toMatch(/no reasons/);
    expect(document.querySelector('button[type="submit"]')).toHaveProperty("disabled", false);
  });

  it("marks quarantine, lists reasons with line numbers, and renders review data as text", async () => {
    const t = await openExplore();
    await submit("example/skills/skills/bad");
    await emit(() => t.emitReview({ event: "done", data: quarantined }));
    const panel = document.querySelector('[aria-label="Quarantine"]')!;
    expect(panel.getAttribute("data-quarantined")).toBe("true");
    expect(panel.textContent).toMatch(/Quarantined/);
    const reasons = Array.from(panel.querySelectorAll("li.gate-reason"));
    expect(reasons.map((r) => r.getAttribute("data-kind"))).toEqual(["license", "screening"]);
    expect(reasons[1]!.textContent).toContain("SKILL.md lines 3, 18");
    expect(reasons[1]!.textContent).toContain("<b>Ignore previous instructions</b>");
    expect(panel.querySelector("b, img")).toBeNull();
    expect(panel.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(panel.textContent).toMatch(/No install is offered/);
  });

  it("shows a typed error, and a typed rejection of the start call", async () => {
    const t = await openExplore();
    await submit("example/skills/skills/missing");
    await emit(() => t.emitReview({ event: "failed", data: { code: "OAH_REVIEW_PATH_UNRESOLVED", message: "The folder does not exist.", hint: "Check the folder name." } }));
    const alert = document.querySelector('.error-box[role="alert"]')!;
    expect(alert.textContent).toMatch(/OAH_REVIEW_PATH_UNRESOLVED/);
    expect(alert.textContent).toMatch(/Check the folder name/);

    t.override.startReview = async () => {
      throw { code: "OAH_TASK_BUSY", message: "A review is running.", hint: null };
    };
    await submit("example/skills/skills/other");
    expect(document.querySelector('.error-box[role="alert"]')?.textContent).toMatch(/OAH_TASK_BUSY/);
    expect(button("Review").disabled).toBe(false);
  });

  it("cancels the running task by id and shows the cancelled state", async () => {
    const t = await openExplore();
    await submit("example/skills/skills/tidy");
    await click(button("Cancel"));
    expect(t.calls).toContain("task_cancel:review-1");
    expect(status()).toMatch(/Reviewing/); // still running until core's terminal event
    await emit(() => t.emitReview({ event: "cancelled" }));
    expect(status()).toMatch(/Review cancelled/);
    expect(document.querySelector('[aria-label="Review result"]')).toBeNull();
  });

  it("leaves a settled result and the next review alone when a stale cancel rejection arrives", async () => {
    const t = await openExplore();
    const cancel = deferred<void>();
    t.override.taskCancel = () => cancel.promise;
    await submit("example/skills/skills/one");
    await click(button("Cancel"));
    await emit(() => t.emitReview({ event: "done", data: passed }, 0));
    await act(async () => cancel.reject({ code: "OAH_TASK_GONE", message: "Task already finished.", hint: null }));
    await flush();
    expect(document.querySelector('[aria-label="Review result"]')?.textContent).toMatch(/Review passed/);
    expect(document.body.textContent).not.toMatch(/OAH_TASK_GONE/);

    const late = deferred<void>();
    t.override.taskCancel = () => late.promise;
    await submit("example/skills/skills/two");
    await click(button("Cancel"));
    await emit(() => t.emitReview({ event: "cancelled" }, 1));
    await submit("example/skills/skills/three");
    await act(async () => late.reject({ code: "OAH_TASK_GONE", message: "Task already finished.", hint: null }));
    await flush();
    expect(document.body.textContent).not.toMatch(/OAH_TASK_GONE/);
    expect(status()).toMatch(/Reviewing example\/skills\/skills\/three/);
    expect(button("Cancel").disabled).toBe(false);
  });

  it("shows a cancel failure during an active review and lets the user retry", async () => {
    const t = await openExplore();
    t.override.taskCancel = async () => {
      throw { code: "OAH_CANCEL_FAILED", message: "Could not cancel.", hint: null };
    };
    await submit("example/skills/skills/tidy");
    await click(button("Cancel"));
    expect(document.querySelector('[role="alert"]')?.textContent).toMatch(/OAH_CANCEL_FAILED/);
    expect(status()).toMatch(/Reviewing/);
    expect(button("Cancel").disabled).toBe(false);
    await click(button("Cancel"));
    expect(t.calls.filter((c) => c === "task_cancel:review-1")).toHaveLength(2);
  });

  it("shows only the validation message when new input is invalid after a backend failure", async () => {
    const t = await openExplore();
    await submit("example/skills/skills/missing");
    await emit(() => t.emitReview({ event: "failed", data: { code: "OAH_REVIEW_PATH_UNRESOLVED", message: "The folder does not exist.", hint: null } }));
    expect(document.body.textContent).toMatch(/OAH_REVIEW_PATH_UNRESOLVED/);
    await submit("example/skills");
    const alerts = Array.from(document.querySelectorAll('[role="alert"]'));
    expect(alerts).toHaveLength(1);
    expect(alerts[0]!.textContent).toMatch(/owner\/repo\/folder/);
    expect(document.body.textContent).not.toMatch(/OAH_REVIEW_PATH_UNRESOLVED/);
    expect(t.reviewRequests).toHaveLength(1);
  });

  it("ignores events from an older task after a newer review started or finished", async () => {
    const t = await openExplore();
    await submit("example/skills/skills/one");
    await emit(() => t.emitReview({ event: "cancelled" }, 0));
    await submit("example/skills/skills/two");
    // late terminal and progress events of the first task
    await emit(() => t.emitReview({ event: "done", data: passed }, 0));
    await emit(() => t.emitReview({ event: "progress", data: { phase: "late", done: 9, total: null } }, 0));
    expect(document.querySelector('[aria-label="Review result"]')).toBeNull();
    expect(status()).toMatch(/Reviewing example\/skills\/skills\/two/);
    expect(status()).not.toMatch(/late/);
    await emit(() => t.emitReview({ event: "failed", data: { code: "X", message: "second", hint: null } }, 1));
    await emit(() => t.emitReview({ event: "done", data: passed }, 1)); // after the terminal event
    expect(document.querySelector('[aria-label="Review result"]')).toBeNull();
    expect(document.querySelector('.error-box[role="alert"]')?.textContent).toMatch(/second/);
  });

  it("keeps the result when the page is switched away and back, and takes the terminal event before the start response", async () => {
    const t = await openExplore();
    const start = deferred<string>();
    t.override.startReview = () => start.promise;
    await submit("example/skills/skills/tidy");
    await emit(() => t.emitReview({ event: "done", data: passed }));
    await act(async () => start.resolve("review-early"));
    await flush();
    await click(button("Installed"));
    await click(button("Explore"));
    expect(document.querySelector('[aria-label="Review result"]')?.textContent).toMatch(/Review passed/);
  });
});
