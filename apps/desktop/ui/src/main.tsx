// 한국어: 웹뷰 진입점 — 운영 Tauri 전송 계층을 App 에 주입한다.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";
import { createTauriTransport } from "./transport/tauri";

const root = document.getElementById("root");
if (!root) throw new Error("root element missing");
createRoot(root).render(
  <StrictMode>
    <App transport={createTauriTransport()} />
  </StrictMode>,
);
