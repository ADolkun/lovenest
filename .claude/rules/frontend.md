---
paths:
  - "frontend/**"
---

# Frontend

- **One HTTP client.** Calls go through the `api` axios instance in `src/lib/api.ts` (`baseURL: '/api'`), which adds the bearer token and `X-Workspace-Id` and signs out on 401. Add endpoints there or in a `src/lib/*-api.ts` module that imports it. The only raw `fetch` for the backend is the chat stream in `src/lib/agents-stream.ts`; `src/hooks/use-latest-release.ts` fetches GitHub.
- **Read-only POSTs pass `readOnly`** (`src/lib/api.ts`). Without it a successful POST calls `markDataChanged()`, which refreshes other tabs through `useCrossTabRefresh`; a preview that does so can loop between two open tabs.
- **Strings are translated.** UI text goes through `t()` from `useTranslation`, with keys in `src/locales/en.json` and the other locale files. `src/locales/i18n.test.ts` fails on a key missing from a locale, an extra key, a duplicate key or a changed `{{placeholder}}`; the locales listed in `LOCALES_WITH_DOWNSTREAM_FALLBACK` there may omit lovenest-only keys.
- **Import alias.** `@/` maps to `src` (`tsconfig.app.json`, `vitest.config.ts`).
- **Type check.** `npm run typecheck` runs the native TypeScript compiler from `@typescript/native`, not `tsc` from the `typescript` package.
- **Class names** are composed with `cn` from `@/lib/utils`.
