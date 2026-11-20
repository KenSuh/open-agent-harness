// 한국어: 테스트 전용 DOM 도우미 (react act 기반, 추가 의존성 없음).
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { ReactElement } from "react";

let mounted: { root: Root; host: HTMLElement } | null = null;

export async function mount(element: ReactElement): Promise<HTMLElement> {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted = { root, host };
  await act(async () => root.render(element));
  await flush();
  return host;
}

export async function unmount(): Promise<void> {
  if (!mounted) return;
  const { root, host } = mounted;
  await act(async () => root.unmount());
  host.remove();
  mounted = null;
  delete document.documentElement.dataset.theme;
}

export async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

export function byText(selector: string, text: string | RegExp): HTMLElement | null {
  const nodes = Array.from(document.querySelectorAll<HTMLElement>(selector));
  return nodes.find((node) => (typeof text === "string" ? node.textContent?.trim() === text : text.test(node.textContent ?? ""))) ?? null;
}

export function button(text: string | RegExp): HTMLButtonElement {
  const found = byText("button", text);
  if (!found) throw new Error(`button not found: ${String(text)}`);
  return found as HTMLButtonElement;
}

export async function click(element: HTMLElement): Promise<void> {
  await act(async () => element.click());
  await flush();
}

export async function press(init: KeyboardEventInit): Promise<boolean> {
  let notCancelled = true;
  await act(async () => {
    notCancelled = window.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
  });
  await flush();
  return notCancelled;
}

export async function emit(action: () => void): Promise<void> {
  await act(async () => action());
  await flush();
}
