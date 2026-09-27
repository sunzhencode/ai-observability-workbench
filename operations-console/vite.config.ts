import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// Bootstrap values live in backend/.env (optional), with their defaults in
// backend/app/config.py. Reading them here keeps the dev server and its API
// proxy on the ports start.sh actually launched, instead of a second hard-coded
// copy that silently disagrees with the first one.
const BACKEND_DIR = fileURLToPath(new URL("../backend", import.meta.url));

export default defineConfig(({ command, mode }) => {
  const env = { ...loadEnv(mode, BACKEND_DIR, "INCIDENT_OPERATIONS_"), ...process.env };
  const host = env.INCIDENT_OPERATIONS_HOST ?? "127.0.0.1";
  const backendPort = Number(env.INCIDENT_OPERATIONS_PORT ?? 8100);
  const frontendPort = Number(env.INCIDENT_OPERATIONS_FRONTEND_PORT ?? 5174);
  const trustedHosts = (env.INCIDENT_OPERATIONS_TRUSTED_HOSTS ?? "")
    .split(",").map((item) => item.trim()).filter(Boolean);
  const connectHost = host === "0.0.0.0" ? "127.0.0.1" : host;
  const publicHost = host === "0.0.0.0" ? trustedHosts[0] : host;
  if (!publicHost) throw new Error("remote access requires an explicit trusted host");
  const backendOrigin = `http://${publicHost}:${backendPort}`;
  const backendTarget = `http://${connectHost}:${backendPort}`;

  const operationsBootstrap = {
    name: "operations-bootstrap",
    async transformIndexHtml(html: string) {
      const response = await fetch(`${backendTarget}/`, { headers: { Host: `${publicHost}:${backendPort}` } });
      if (!response.ok) throw new Error("Incident Operations backend page is unavailable");
      const backendHtml = await response.text();
      const match = backendHtml.match(/<meta name="csrf-token" content="([^"]+)" \/>/);
      if (!match) throw new Error("Incident Operations backend did not provide a CSRF token");
      return html.replace("</head>", `<meta name="csrf-token" content="${match[1]}" /></head>`);
    },
  };

  return {
    plugins: command === "serve" ? [react(), operationsBootstrap] : [react()],
    server: {
      port: frontendPort,
      strictPort: true,
      host,
      allowedHosts: trustedHosts,
      proxy: {
        "/api": {
          target: backendTarget,
          changeOrigin: true,
          configure(proxy) {
            proxy.on("proxyReq", (request) => {
              request.setHeader("Host", `${publicHost}:${backendPort}`);
              request.setHeader("Origin", backendOrigin);
            });
          },
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
