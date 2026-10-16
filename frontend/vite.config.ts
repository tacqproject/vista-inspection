import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': { target: process.env.VISTA_API_TARGET || 'http://127.0.0.1:8001', changeOrigin: true },
      '/swagger': { target: process.env.VISTA_API_TARGET || 'http://127.0.0.1:8001', changeOrigin: true, rewrite: path => path.replace(/^\/swagger/, '/docs') },
      '/openapi.json': { target: process.env.VISTA_API_TARGET || 'http://127.0.0.1:8001', changeOrigin: true },
      '/uploads': { target: process.env.VISTA_API_TARGET || 'http://127.0.0.1:8001', changeOrigin: true },
      '/ws': { target: process.env.VISTA_API_TARGET || 'http://127.0.0.1:8001', ws: true, changeOrigin: true },
    },
  },
})
