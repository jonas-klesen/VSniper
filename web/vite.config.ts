import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  const proxyTarget = env.VITE_DEV_PROXY_TARGET || 'http://127.0.0.1:8000';

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
          configure: (proxy) => {
            proxy.on('proxyReq', (_proxyReq, req) => {
              console.info(`[vite proxy] request ${req.method ?? 'UNKNOWN'} ${req.url ?? ''} -> ${proxyTarget}`);
            });
            proxy.on('proxyRes', (proxyRes, req) => {
              console.info(
                `[vite proxy] response ${req.method ?? 'UNKNOWN'} ${req.url ?? ''} status=${proxyRes.statusCode ?? 0}`,
              );
            });
            proxy.on('error', (error, req) => {
              console.error(`[vite proxy] error ${req.method ?? 'UNKNOWN'} ${req.url ?? ''}: ${error.message}`);
            });
          },
        },
        '/healthz': {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
  };
});
