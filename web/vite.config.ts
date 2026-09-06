import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// The backend has no CORS middleware, so the browser must never call :8300
// directly in development - everything goes through this proxy and stays
// same-origin. Production is same-origin too: build web/dist and serve it from
// the same Caddy/uvicorn host as the API.
const API = process.env.PI_API ?? "http://127.0.0.1:8300";

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      // SSE rides the same proxy. http-proxy streams the body through and the
      // backend already sends X-Accel-Buffering: no, so frames are not buffered;
      // do not add a compression middleware here or streaming will stall.
      "/v1": { target: API, changeOrigin: true },
      "/healthz": { target: API, changeOrigin: true },
      "/readyz": { target: API, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
