import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = Object.hasOwn(process.env, 'TCODE_URL')
    ? process.env.TCODE_URL || 'http://127.0.0.1:8000'
    : env.TCODE_URL || 'http://127.0.0.1:8000';
  // An explicitly empty process value disables development proxy injection.
  // This is useful for local auth-disabled smoke tests even when .env.local
  // contains a key for another server instance.
  const apiKey = Object.hasOwn(process.env, 'TCODE_API_KEY')
    ? process.env.TCODE_API_KEY || ''
    : env.TCODE_API_KEY || '';
  const headers: Record<string, string> | undefined = apiKey
    ? { Authorization: `Bearer ${apiKey}` }
    : undefined;

  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api/ag-ui': {
          target,
          changeOrigin: true,
          headers,
        },
        '/api/workdirs': {
          target,
          changeOrigin: true,
          headers,
        },
        '/api/sessions': {
          target,
          changeOrigin: true,
          headers,
        },
      },
    },
  };
});
