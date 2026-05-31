import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// ArcUI frontend. Builds into the Python package's `static/` dir so the
// existing Starlette server serves it unchanged (`Route("/", _index)` +
// `Mount("/assets", static/assets)`). Dev mode proxies the API + WS to a
// running `arc ui start` backend on :8420.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    // Emit straight into the package static dir the server already serves.
    outDir: '../src/arcui/static',
    emptyOutDir: true,
    // index.html at root + hashed assets under assets/ — matches the
    // server's `/` route and `/assets` mount with zero backend changes.
    assetsDir: 'assets',
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8420', changeOrigin: true, ws: true },
      '/ws': { target: 'http://127.0.0.1:8420', changeOrigin: true, ws: true },
      '/sw.js': 'http://127.0.0.1:8420',
    },
  },
})
