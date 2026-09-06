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

The three chain families do not measure the same thing here, and Solana is the
loosest: `getSignaturesForAddress` returns every signature an address appears
in, as fee payer or program account included, not only the ones that moved its
balance. A bot-run or DEX-heavy Solana wallet can fill the page in a day and be
called pooled when it is not. Erring that way is deliberate — the Trace stops
and says it stopped, which a reader can act on, whereas expanding a genuine
exchange invents a trail.

Bitcoin reads the same rule off a different unit. Esplora's page size is fixed
at 25, so depth comes from asking again; `_bitcoin_history` pages until it
reaches past the window or hits `BITCOIN_HISTORY_MAX_PAGES`, and returns which
of the two happened. Only a history cut off by the cap can be called pooled. A
history that simply ran out is the whole story about that address however fast
it was written, and calling it saturated would stop a Trace on a wallet with
nothing left to hide.

## On Bitcoin, who received the money has to be inferred

A Bitcoin transaction names no sender and no recipient. It spends a set of
outputs and creates another set, and `_bitcoin_deltas` has to read intent out
of that. The two directions are not equally knowable, and pretending they were
is the mistake available here.

Incoming is arithmetic: the address gained `received - spent`, and the largest
input that is not its own is who funded it.

Outgoing is a judgement. Every output the transaction did not pay back to one
of its own inputs is treated as a recipient, because change normally returns to
an input address and nothing in a transaction distinguishes change sent to a
*fresh* address from a payment. The known consequence: a Trace can follow a
spender's own change as though it were a transfer, so an outgoing hop may show
more recipients than were really paid.

That is the direction chosen deliberately. The alternative — demanding a
confident match before following anything, as `_solana_deltas` does — drops the
hop that carried the money whenever a wallet uses fresh change addresses, which
most modern ones do. An extra branch is visible to a reader and costs them a
look; a missing branch is invisible and ends the trail. Amount ranking pushes
the change output down the list anyway, since a sweep is larger than what it
leaves behind.

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

## A token is listed on having a market, and valued on being vouched for

A Watched Address holds more than its native coin, and the two questions a
token raises have different answers.

*Should it appear at all?* Only if the index can price it. An address that has
existed for years holds thousands of airdropped tokens with no market, and
listing them buries the handful that are positions. The filter is on having a
price, not on the price being large, so a memecoin worth twelve cents survives
it — that is the point, since the user asked to see exactly those.

*Should its price count?* Only if the index vouches for the token — Jupiter's
`verified`, Blockscout's `reputation: ok`. Anyone can mint a token, seed a pool
and quote it at any number, and a portfolio total is precisely what such a
token would be minted to attack. So an unvouched position is named, carries its
real quantity, keeps the refused quote in `external_metadata`, and contributes
zero. Understating is recoverable by hand; a fabricated number in a net worth
is not noticed at all.

*Should it order the others?* Also only if vouched. This was the hole in the
first version: the cap keeps the 25 most valuable tokens, and ranking by the
quoted value alone handed that choice to whoever quoted it. Minting 25 tokens
priced at a million dollars each and airdropping them costs a few dollars and
needs nothing from the victim; they would sort above every real position, push
it out of the payload, and the sync layer would archive what fell off the end.
`TokenHolding.rank` puts vouched before unvouched and only then compares value.
Refusing a number for one purpose and trusting it for another is the general
shape of this mistake.

Identity is read from the mint or contract address, never from the symbol the
token reports. A token can call itself USDC; it cannot occupy USDC's mint. That
is why pricing does not go through the Coinbase symbol table the native coins
use, even though it is already wired up. For the same reason an unvouched
symbol never becomes an `Asset.ticker`: the ticker is what positions
consolidate on, what CSV imports match against, and what decides whether a
holding is a cash equivalent, so an attacker-chosen one joins a real position
and takes its cost basis down with it. The name still shows the symbol, because
nothing keys on the name.

## A short payload is a claim, not a gap

`_sync_holdings` archives every asset the provider stops reporting. That is
right for a redeemed bond and wrong for a wallet whose index was unreachable
for thirty seconds, and the provider is the only layer that can tell the two
apart. So `get_holdings` raises rather than returning a partial list: a raise is
caught upstream and leaves every value stale, where a short list silently
archives real positions — and `_upsert_asset_from_holding` only un-archives when
a holding moves between connections, so they do not come back on their own.

Staleness is visible in the connection's last-synced time and heals on the next
good sync. Archiving is silent, removes the holding from net worth, allocation
and every report, and needs the user to notice and undo it by hand. Between a
recoverable wrong answer and an unrecoverable one, this surface takes the
recoverable one every time — the same rule that governs "nothing moved".

### Naming what went unread beats failing the whole connection

An all-or-nothing raise costs the 24 addresses that answered their sync for the
sake of the one that did not. `get_holdings` therefore raises `PartialHoldings`,
which carries the holdings it did read plus the scopes it could not: the sync
layer holds exactly those out of the archive sweep and processes the rest
normally, so a dead RPC endpoint stales one address instead of the connection.
Failure is per address, not per read — a wallet whose native balance answers but
whose token index does not counts as unread, since keeping the half that
answered would archive the other half.

The account balance is the exception: it is a derived total, so a partial read
leaves it short for a cycle rather than blocking the sync it sits in front of.

## Reaching a Pooled Address is the answer, not a failure

A Trace that stops at an exchange has succeeded. Custody changed there: the
next movement is the operator's internal accounting, and a further hop would
assert something the chain does not record. What the Trace has produced is the
name of the party who can be asked whose account received the funds — which is
the only thing that was ever actionable. The UI gives that ending the prominent
treatment for the same reason.

## Balances are read over public RPC; history comes from an index

Solana's JSON-RPC answers "what did this address do" directly, so Solana needs
no key for either balances or Traces. EVM JSON-RPC has no such call — an
address's history exists only in an indexer — so `native_balance` works on a
public node while `_evm_transfers` goes to Blockscout, or to Etherscan when a
key is set.

Bitcoin has no account to read at all: an address is a set of unspent outputs,
so even its *balance* is an index query. Both sides therefore go to Esplora,
which Blockstream and mempool.space run keyless and which is self-hostable, so
the asymmetry costs nothing here — `ONCHAIN_RPC_URLS["bitcoin"]` takes an
Esplora base URL rather than a JSON-RPC endpoint, because a `bitcoind` RPC
cannot answer the question either.

A watched address is also validated on its checksum rather than its shape, and
only Bitcoin's carries one. That is what settles the overlap between a legacy
`1`/`3` address and a Solana public key, whose base58 forms are otherwise
indistinguishable at those lengths — and it is why a mistyped Bitcoin address
is refused at the paste box instead of being watched forever as an empty
wallet.

The segwit check validates the witness program's length too, not just the
checksum. That was first left to the index on the grounds that a malformed
program is merely a rejected request — which stopped being true once a rejected
read began failing the whole sync. One address nobody can look up would wedge
every other address on the connection, so it is refused where it is pasted.

Where no source exists at all the Trace raises rather than returning an empty
walk, and `trace` deliberately lets `ProviderNotConfiguredError` past its
per-node handler: it is true of every address the walk would visit, so
recording it against one node would dress a total failure up as a trail that
happens to end early. Returning an empty history would be indistinguishable
from an address that never transacted, which on this surface is not a missing
feature but a false exoneration.

EVM history reads two lists, not one. One covers what the address itself sent
and received; the other covers native coin moved by a contract. The dominant
EVM drain is a victim signing a zero-value call whose sweep happens inside the
contract, so reading only the first reports the drained wallet as untouched.

## The keyless index is preferred, and it is why the rate test runs per page

Etherscan was the first EVM history source and requiring its key made EVM
Traces unreachable by default: every deployment that never signed up saw the
chains greyed out, which is a feature that does not exist as far as its user is
concerned. Blockscout answers both lists unauthenticated on every EVM chain
here — it is already the source for ERC-20 balances — so it is the default and
the key is an upgrade, buying a page a thousand rows deep against Blockscout's
fifty.

That page size is what moves the pooled test. It cannot run after the paging
finishes, because the addresses it exists to recognise are exactly the ones
Blockscout stops answering for: an exchange hot wallet's first page returns in
about a second, and paging deeper into the same address times out. Judging each
page as it arrives ends the walk on the first one — one request, and the
verdict the trail was asking for — where judging afterwards spends the Trace's
whole time budget and then reports the address as unreadable.

Unpageable is not treated the same way, because paging is its remedy: it says
the window sits further back than this page reached, and the next page may
reach it. Only pooled stops the paging. A later page that will not load ends
the list rather than failing the read, and the shortfall travels back as
`trimmed` — half a list is evidence, it is just not evidence of absence.

## A throttle mid-walk keeps the trail it already found

`ProviderRateLimited` used to leave `trace` the same way a missing source does,
on the same reasoning: a throttled node will refuse every address left, not
just this one. That is still true, and the walk still ends there. What changed
is what happens to the hops already taken. Discarding them answers a smaller
question than the one the caller asked, and it is the common case on a shared
public node — the first hops succeed, then the node starts refusing.

So the walk stops, the address it was reading and every address still queued
are marked `rate_limited`, and the partial trail is returned as a partial
trail. The property in the module docstring is unharmed: every address the walk
stopped at still carries a reason, and nothing claims the trail ended there. A
Trace that found nothing at all still raises, because there is no trail to
qualify and an empty graph would read as an answer.
