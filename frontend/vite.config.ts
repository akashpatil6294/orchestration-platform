import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The dev server port matches the origins the backend allows through CORS
// (see CORS_ORIGINS in the root .env.example).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
  preview: {
    port: 5173,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // Values a test run needs. They are not secrets: the real pair lives in
    // frontend/.env.local and is never committed.
    env: {
      VITE_SUPABASE_URL: 'https://testproject.supabase.co',
      VITE_SUPABASE_ANON_KEY: 'test-anon-key',
      VITE_API_BASE_URL: 'http://127.0.0.1:8000',
    },
  },
})
