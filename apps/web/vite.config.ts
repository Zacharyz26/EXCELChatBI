import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 前端开发服务器：/api 代理到后端 FastAPI（含 SSE）
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": "/src" } },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
