# Investment tracking lives on the assets page, not a hidden second frontend

Issue #47 and ticket #62 specified the investment tracker as a second Vite entry
point, served at a path absent from navigation with the workspace pinned in code,
so that investment data would stay concealed within a household that shares the
app. We are instead building it as tabs on the existing `/assets` route, and
closing #62.

The concealment requirement is real but it is a *membership* question, and #46
already answered it: a Workspace you are not a member of is absent from the
application, enforced at the tenancy chokepoint, returning 404. Nothing a
frontend does adds to that. Conversely, nothing a frontend does can substitute
for it — an unlisted URL is visible the moment you switch workspaces, and this
codebase already has unlisted routes (`/collections` appears in neither the
sidebar nor the command palette) precisely because they cost one line and prove
nothing.

## Considered options

- **Second Vite entry point** (as specified). Rejected: a whole build target, an
  auth bootstrap and a nav story, bought to duplicate a guarantee the server
  already makes unconditionally.
- **New `/investments` route reusing the asset components.** Rejected: two routes
  over one model. Wallets and Holdings would be rendered from both, and every
  later change would have to decide which route owned it.
- **Tabs on `/assets`** (chosen). The page already renders Wallets, Holdings, a
  portfolio chart and a trade ledger. Investment views are more views of the same
  aggregate, so they belong behind the same route.

## Consequences

`frontend/src/pages/assets.tsx` is already the largest page in the repo at ~2900
lines with a hand-rolled, non-URL-synced tab pair. New tabs are URL-synced via
`useSearchParams`, matching every other page, and components are extracted as
they are touched rather than in a preparatory refactor.

Because isolation now rests entirely on workspace scoping being correct in code,
the unfiltered cross-workspace matchers in `asset_group_service` and
`connection_service` stop being latent bugs and become the thing holding the
guarantee up. They are fixed first, ahead of any investment work.

This decision assumes investment data stays in a single Workspace. Wash Sale
detection spans every account a person holds; if investments were ever split
across two Workspaces, detecting one would require crossing the boundary, and
that crossing is not available.

## Amendment (2026-08-23, ticket #67): the importer sits on `/import`

The v0.14.3 upstream sync brought an investment-order importer mounted at
`/import?tab=investments`, with `/assets` linking across to it. That is a
deliberate exception to this ADR, not an oversight to be corrected.

The rule above is about *views of the portfolio* — a Wallet, a Holding, a Lot,
a chart — which are views of one aggregate and belong behind one route. An
import is not a view of the portfolio: it is a file-shaped operation that
produces one, and it is the same operation, with the same drop zone, mapping
dropdowns, preview table and undo, whether the file holds transactions or
orders. Splitting it would give the same person two habits for one task, which
is the argument this ADR makes in the other direction.

Investment *views* stay on `/assets`. Only the importer lives on `/import`, and
only as a tab beside the transaction importer it shares its shape with.
