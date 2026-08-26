import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: Vite serves the SPA on 5173 and proxies /api to the BFF, so the
// browser sees one origin in both dev and prod (prod: BFF serves dist/).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: `http://localhost:${process.env.CLOUDOPS_BFF_PORT ?? 8080}`,
        changeOrigin: false,
      },
    },
  },
});
