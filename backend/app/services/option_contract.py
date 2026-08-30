"""Equity option contracts as Holdings, identified by their OCC symbol.

`NVDA250117C00140000` is the root, the expiry as YYMMDD, C or P, then the
strike times a thousand in eight digits. The tail is exactly fifteen
characters, so everything the contract *is* — underlying, expiry, right,
strike — is recoverable from the ticker and none of it needs a column of its
own. That is the whole reason the symbol is the identity here: an option
holding costs no migration and no new table.

Two facts separate an option from every other Holding in the portfolio:

- **A contract is a hundred shares.** The ledger books cost, average price and
  cost basis per *contract*, while `price` stays per share because that is what
  a broker quotes. So a contract bought at $4.00 reports an `average_price` of
  $400.03 and a `last_price` of $4.00, and `cost_basis = average_price x
  quantity` — the invariant every read surface is built on — still holds.
- **The position may be negative.** Writing a call or a put is a position, not
  a mistake: the user receives a credit and owes the contract until it is
  bought back or expires. The rest of the portfolio stays buy-and-hold.

`multiplier_for` answers 1 for every other asset type, which is what keeps
this module inert on the holdings that already exist.
"""
import re
from datetime import date
from decimal import Decimal
from typing import Optional

OPTION_TYPE = "option"

#: Shares per contract. Equity options are 100 by convention; a contract
#: adjusted by a split or a merger deliverable is not, and nothing here can
#: tell — the OCC symbol of an adjusted contract looks exactly like a standard
#: one. The user would have to correct such a position by hand.
CONTRACT_MULTIPLIER = Decimal("100")

#: The fixed tail: six digits of expiry, the right, eight digits of strike.
_TAIL = 15

#: A root of one to six characters, then that tail. Tight enough that no
#: ticker shape the portfolio already holds can match it — not `NVDA`, not
#: `BTC-USD`, not `PETR4.SA`, and not the Tesouro form `TD:...:2029-01-01`,
#: all of which carry a separator or run out of digits.
_OCC = re.compile(r"^[A-Z][A-Z0-9]{0,5}\d{6}[CP]\d{8}$")


def is_option_symbol(ticker: Optional[str]) -> bool:
    return bool(ticker) and _OCC.match(ticker.strip().upper()) is not None


def is_option(asset_type: Optional[str]) -> bool:
    return asset_type == OPTION_TYPE


def multiplier_for(asset_type: Optional[str]) -> Decimal:
    """Shares a unit of this asset class represents. One, unless it is a
    contract — the reason every existing holding is untouched by this."""
    return CONTRACT_MULTIPLIER if is_option(asset_type) else Decimal("1")


def _parsed(symbol: Optional[str]) -> Optional[tuple[str, date, str, Decimal]]:
    """Root, expiry, right and strike, or None when this is not a contract.

    One narrowing point. Every accessor below is a projection of this tuple
    rather than its own parse, so the Optional resolves once instead of at
    four call sites that each have to re-prove the symbol is a string.
    """
    if symbol is None:
        return None
    normalized = symbol.strip().upper()
    if not _OCC.match(normalized):
        return None
    tail = normalized[-_TAIL:]
    return (
        normalized[:-_TAIL],
        date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6])),
        "Call" if tail[6] == "C" else "Put",
        Decimal(tail[7:]) / 1000,
    )


def underlying_of(symbol: Optional[str]) -> Optional[str]:
    parsed = _parsed(symbol)
    return parsed[0] if parsed else None


def expiry_of(symbol: Optional[str]) -> Optional[date]:
    parsed = _parsed(symbol)
    return parsed[1] if parsed else None


def strike_of(symbol: Optional[str]) -> Optional[Decimal]:
    parsed = _parsed(symbol)
    return parsed[3] if parsed else None


def describe(symbol: Optional[str]) -> Optional[str]:
    """`NVDA250117C00140000` -> `NVDA 1/17/2025 Call $140.00`.

    Byte-identical to the Description column Robinhood exports, thousands
    separator included, so a holding created from a file reads the same as the
    row it came from and the two can be matched by eye.
    """
    parsed = _parsed(symbol)
    if parsed is None:
        return None
    root, expiry, right, strike = parsed
    return f"{root} {expiry.month}/{expiry.day}/{expiry.year} {right} ${strike:,.2f}"
