import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In production the bundle will be served at https://reviewdr.kr/stampport-control/
// and the API lives at https://reviewdr.kr/stampport-control-api/, both behind
// the same nginx — so a relative path works without env vars.
//
// In dev (`npm run dev`) the API base is overridden inside controlTowerApi.js
// to point at the local FastAPI on :8000, so this proxy is only here so the
// same `/stampport-control-api/...` path also works if a developer prefers it.
export default defineConfig({
  base: "/stampport-control/",
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/stampport-control-api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/stampport-control-api/, ""),
      },
    },
  },
});
