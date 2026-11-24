// services/web-frontend/vite.config.js -- TaskFlow
// In development, /api is proxied to the gateway (the Compose harness
// publishes it on localhost:8000). In the cluster, the Ingress sends /api to
// api-gateway and everything else to this frontend, so the browser only
// ever talks to one origin and no CORS is needed.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
  build: { outDir: "dist", sourcemap: false },
});
