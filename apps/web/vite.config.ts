import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// dev proxy 遵循契约：/api/v1 -> 127.0.0.1:8000，组件内不写后端地址
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/healthz': {
        target: process.env.P2R_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
      '/api/v1': {
        target: process.env.P2R_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
});
