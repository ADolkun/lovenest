import { configDefaults, defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// These three library suites exercise browser storage, styles, or i18n.
const nodeTests = ['src/lib/!(onchain-api|theme-utils|workspace-kinds).test.ts']

export default defineConfig({
  plugins: [react()],
  define: {
    // vite.config.ts injects this from the package version at build time.
    // Without it here, anything importing lib/build-info throws on import,
    // which takes the whole app shell down with it.
    __APP_VERSION__: JSON.stringify('0.0.0-test'),
  },
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  test: {
    // Vite serves the app's CSS through Tailwind; none of it affects what the
    // tests assert, so skip the transform and keep the suite fast.
    css: false,
    projects: [
      {
        extends: true,
        test: {
          name: 'node',
          environment: 'node',
          include: nodeTests,
        },
      },
      {
        extends: true,
        test: {
          name: 'dom',
          environment: 'jsdom',
          setupFiles: ['./src/test/setup.ts'],
          // Every other discovered test keeps the browser environment.
          exclude: [...configDefaults.exclude, ...nodeTests],
        },
      },
    ],
  },
})
