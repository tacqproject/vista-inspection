// Serve the built RAW UI without writing a bundled Vite config into the checkout.
import { preview } from 'vite'

const target = process.env.VISTA_API_TARGET || 'http://127.0.0.1:8001'
await preview({
  configFile: false,
  preview: {
    host: '0.0.0.0', port: 5174, strictPort: true,
    proxy: {
      '/api': { target, changeOrigin: true },
      '/swagger': { target, changeOrigin: true, rewrite: path => path.replace(/^\/swagger/, '/docs') },
      '/openapi.json': { target, changeOrigin: true },
      '/uploads': { target, changeOrigin: true },
      '/ws': { target, changeOrigin: true, ws: true },
    },
  },
})
