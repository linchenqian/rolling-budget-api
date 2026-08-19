import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.DEMO_API_TARGET || "http://127.0.0.1:8080";
const readKey = process.env.BUDGET_READ_API_KEY || process.env.API_KEY;

export default defineConfig({
  build: {
    outDir: "dist/client",
  },
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: false,
        rewrite: (path) => path.replace(/^\/api/, ""),
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyRequest) => {
            if (readKey) {
              proxyRequest.setHeader("Authorization", `Bearer ${readKey}`);
            }
          });
        },
      },
    },
    warmup: {
      clientFiles: ["./src/main.jsx"],
    },
  },
  plugins: [react()],
});
