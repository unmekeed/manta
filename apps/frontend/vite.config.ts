import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev/preview проксируют /api в шлюз — фронтенд не знает про CORS.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8080" } },
  preview: { proxy: { "/api": "http://localhost:8080" } },
});
