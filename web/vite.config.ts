import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API serves the build: index.html at "/", the assets under "/static/" (see
// src/relay/app/routes/ui.py). `npm run dev` proxies the API to a local `relay serve`.
const api = process.env.RELAY_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  base: "/static/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  server: { proxy: { "/v1": api, "/healthz": api } },
});
