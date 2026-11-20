// 한국어: Vite 빌드와 Vitest 설정 (Tauri 번들용 상대 경로, 외부 자산 없음).
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "dist", target: "es2022" },
  server: { port: 1420, strictPort: true },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    setupFiles: ["tests/support/setup.ts"],
  },
});
