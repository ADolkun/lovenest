# A Transfer Trace ends where funds are pooled

Ticket #127 asked for watch-only crypto wallets and the ability to trace an
address's funds through the chain.

Following stolen funds is not the same problem as drawing an address's
transaction graph. The graph is unbounded and mostly noise — dust spam,
airdrops, unrelated history — and a view that renders all of it answers no
question anyone asked. These are the decisions that turn the second thing into
the first, and the one property they all serve: **a Trace may return less than
the whole trail, but it may never present less as the whole.**

## A hop inherits the previous transfer's instant

Every address reached by a Trace carries the timestamp of the transfer that
reached it, and only its movements after that instant (before it, tracing
backwards) are followed — `onchain_trace.trace` sets the child's window from
`transfer.occurred_at`, and `_within` applies it inside
`providers/onchain.py` before any transaction is fetched.

Without this the second hop returns the counterparty's entire history, and the
trail is lost in it — worse, the result *asserts* that unrelated earlier
payments were destinations of the traced funds. The window is not an
optimisation that happens to reduce requests; it is what makes the output a
claim about the money rather than a list of an address's acquaintances.

The same reasoning governs which transfers survive a cap. `_closest_to_horizon`
keeps the ones nearest the window's anchor, not the newest, because money
leaves an address soon after it arrives. Keeping the newest traces a real but
later movement of the same wallet and reports it as this money's path — a
wrong answer, not a partial one.

## Only the largest branches are followed

`_pick` ranks a node's in-window transfers by amount and the top few are
expanded. A drainer sweeps a balance; it does not dribble it. Following
everything would spend the request budget on dust and return a fan-out no
reader can hold, while the sweep — the one edge that matters — sits in it
unmarked.

This ranking runs over the transfers that were fetched, which is not always
every transfer in the window: `TRANSFERS_PER_NODE` caps how many a node may
cost. So volume *can* hide size, and the honest response is not to pretend
otherwise but to say so — see the next section.

## A Pooled Address is recognised by rate, not by volume

`_saturation` calls an address pooled when its history comes back at the page
cap *and* spans less than `POOLED_SPAN_SECONDS`. One whose thousand
transactions span months is not pooled, however busy it looks.

The first version of this keyed on volume alone, and it was wrong in the case
that matters: a scam aggregator collecting from many victims over months hit
the cap and was reported as an exchange, ending the Trace one hop before the
exchange it actually forwarded to. Volume measures how much an address is
used. Rate measures whether a human is using it.

The two chain families do not measure the same thing here, and Solana is the
looser of the two: `getSignaturesForAddress` returns every signature an address
appears in, as fee payer or program account included, not only the ones that
moved its balance. A bot-run or DEX-heavy Solana wallet can fill the page in a
day and be called pooled when it is not. Erring that way is deliberate — the
Trace stops and says it stopped, which a reader can act on, whereas expanding a
genuine exchange invents a trail.

## "Nothing moved" is only sayable on complete evidence

`Transfers` reports three separate ways a page can fall short of the truth —
`saturated`, `trimmed`, `unreadable` — and `Transfers.complete` is true only
when none of them fired. `onchain_trace` consults it before choosing between
`no_movement` (the address really is a dead end), `no_match` (transfers existed
but the caller's own filters excluded them) and `partial` (the walk could not
see far enough to say).

Collapsing those into one message is the failure mode this exists to prevent,
and it is not hypothetical. A drained wallet gets dust-spammed afterwards; the
sweep then sits below thirty dust transactions in a newest-first page. A reader
told "nothing has moved out of this address" about a wallet that was emptied
has been given a false exoneration by a tool they trusted — the single most
damaging thing this surface could do.

`_solana_deltas` answers to the same rule at the level of one transaction. It
returns no transfer, rather than a guess, when it cannot pair this address's
movement with a counterparty whose movement matches it. Taking the largest
account moving the other way — which it used to do — attaches a real signature
to a wallet that received nothing, and every hop after that is fiction carrying
the same confidence as fact.

## Reaching a Pooled Address is the answer, not a failure

A Trace that stops at an exchange has succeeded. Custody changed there: the
next movement is the operator's internal accounting, and a further hop would
assert something the chain does not record. What the Trace has produced is the
name of the party who can be asked whose account received the funds — which is
the only thing that was ever actionable. The UI gives that ending the prominent
treatment for the same reason.

## Balances are read over public RPC; history sometimes cannot be

Solana's JSON-RPC answers "what did this address do" directly, so Solana needs
no key for either balances or Traces. EVM JSON-RPC has no such call — an
address's history exists only in an indexer — so `native_balance` works on a
public node while `_evm_transfers` requires an Etherscan key.

Where the key is absent the Trace raises rather than returning an empty walk,
and `trace` deliberately lets `ProviderNotConfiguredError` and
`ProviderRateLimited` past its per-node handler: both are true of every address
the walk would visit, so recording them against one node would dress a total
failure up as a trail that happens to end early. Returning an empty history
would be indistinguishable from an address that never transacted, which on this
surface is not a missing feature but a false exoneration.

EVM history reads two Etherscan lists, not one. `txlist` covers what the
address itself sent and received; `txlistinternal` covers native coin moved by
a contract. The dominant EVM drain is a victim signing a zero-value call whose
sweep happens inside the contract, so reading only the first reports the
drained wallet as untouched.
