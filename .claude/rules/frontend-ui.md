---
paths:
  - "frontend/src/components/**"
  - "frontend/src/pages/**"
---

# Frontend UI

- **Shared primitives first.** Buttons, dialogs, tables, selects and tooltips come from `src/components/ui/` (Radix-backed); compose classes with `cn`, never template strings.
- **Links that look like buttons** are `<Button asChild><Link to="...">` with `Link` from `react-router-dom` (`src/components/balance-details.tsx`).
- **Icon-only controls need an accessible name.** `jsx-a11y/control-has-associated-label` in `frontend/eslint.config.js` fails `npm run lint` for a `Button` without one; use `aria-label` or visible text.
- **Selectable buttons set `aria-pressed`** (`src/components/transactions-view-switcher.tsx`, `src/components/positions-tab.tsx`).
- **Touch targets** that would otherwise be small use `min-h-11` (44px) (`src/components/owned-coin-timeline.tsx`).
- **Feedback uses `toast` from `sonner`.** The single `<Toaster />` is mounted in `src/App.tsx`.
- **Tooltips** need the `TooltipProvider` that `src/App.tsx` mounts; a component test that renders one must wrap it itself (`src/components/transaction-drill-down.test.tsx`).
- **Money is formatted with `formatCurrency`** (`src/lib/format.ts`), which keeps each currency's own precision. Do not hand-roll `toFixed(2)` for currency.
- **Masking.** Balances and amounts on the dashboard, accounts, assets, budgets and goals pages go through `mask` from `usePrivacyMode` (`src/hooks/use-privacy-mode.ts`), so privacy mode hides them.
- **Responsive JS** uses `useIsMobile` (`src/hooks/use-mobile.ts`, 768px); layout differences belong in Tailwind breakpoints.
