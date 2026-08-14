# FIFO decides which Lots a sale consumed; the average still decides what it gained

Splitting a Realised Gain into long-term and short-term needs two answers a
weighted average cannot give on its own: *which* units left, and *what* each one
cost. We take the first from FIFO and keep the second on the average.

The app books gains at the weighted average (preço médio) deliberately, and
`asset_transaction_service` is the one place that computes them. Deriving a
second, FIFO-cost gain for the same sale would give one trade two different
Realised Gains — the tax split saying 240 while the position says 250 — and the
user has no way to tell which is the real one.

So a sale's gain stays the average-cost figure, attributed to long and short in
the proportion of the quantity FIFO consumed from each Lot. FIFO is the broker
default and the only consumption order that asks nothing of the user.

## Consequences

`realised_long + realised_short` always equals the Realised Gain the ledger
reports; a test pins that, including where the ratio does not divide into cents.

An open Lot's `unit_price` is what that acquisition actually cost, not the
average, because a Lot is one acquisition at one price (CONTEXT.md). After a
partial sale the Lot costs therefore no longer sum to the position's Cost Basis:
the sale removed `average × quantity` from the basis while leaving the surviving
Lots at their own prices. Both figures are right for what they answer, and Cost
Basis remains the one shown against the position.

Specific-identification is not offered. When it is, it belongs here as a
per-sale choice of Lots — the Lots this derives are exactly what such a choice
would pick from — and the same reconciliation question returns with it.
