import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// Bootstrap values live in backend/.env (optional), with their defaults in
// backend/app/config.py. Reading them here keeps the dev server and its API
// proxy on the ports start.sh actually launched, instead of a second hard-coded
// copy that silently disagrees with the first one.
const BACKEND_DIR = fileURLToPath(new URL("../backend", import.meta.url));

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, BACKEND_DIR, "ALERT_WORKBENCH_"), ...process.env };
  const host = env.ALERT_WORKBENCH_HOST ?? "127.0.0.1";
  const backendPort = Number(env.ALERT_WORKBENCH_PORT ?? 8000);
  const frontendPort = Number(env.ALERT_WORKBENCH_FRONTEND_PORT ?? 5173);

  return {
    plugins: [react()],
    server: {
      port: frontendPort,
      strictPort: true,
      host,
      proxy: {
        "/api": {
          target: `http://${host}:${backendPort}`,
          changeOrigin: true,
        },
      },
    },
    test: {
      // Pure logic only for now: no jsdom, so component rendering is out of
      // scope and stays covered by the scripted browser acceptance run.
      environment: "node",
      include: ["src/**/*.test.ts"],
    },
  };
});
