---
paths:
  - "frontend/src/**/*.test.ts"
  - "frontend/src/**/*.test.tsx"
  - "frontend/src/test/**"
  - "frontend/vitest.config.ts"
---

# Frontend tests

- **Two Vitest projects** (`frontend/vitest.config.ts`). `node` runs the `nodeTests` glob, which is `src/lib/*.test.ts` minus four files; everything else runs in `dom` (jsdom, with `src/test/setup.ts`). A `src/lib` test that needs the DOM either starts with `// @vitest-environment jsdom` (`src/lib/api.test.ts`) or is added to the exclusion list in `nodeTests`.
- **Render with `renderWithProviders`** from `@/test/utils`. It wires TanStack Query (no retries, no cache), the router, the real English i18n bundle and `SidebarStateProvider`, and returns a bound `user`. Pass `route` and `path` for components that read params.
- **Assert on what a user sees.** Prefer `getByRole` and label queries; `getByTestId` is a last resort. English strings are real, so a deleted locale key fails the test.
- **Mock at the API module.** Tests stub `@/lib/api` (`vi.mock('@/lib/api', ...)`) instead of axios or `fetch`.
- **What `src/test/setup.ts` provides.** It cleans up the DOM and clears `localStorage` and `sessionStorage` after each test, and polyfills `matchMedia`, `ResizeObserver`, pointer capture, `scrollIntoView` and `getAnimations`, which Radix and Recharts need. Add a missing browser API there, not in individual tests.
- **Run one file** with `npx vitest run src/<path>.test.tsx` from `frontend/`.
