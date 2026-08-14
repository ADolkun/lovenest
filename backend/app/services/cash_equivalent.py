"""Opening guess at which tickers are Cash Equivalents (CONTEXT.md).

A Cash Equivalent is a Holding that behaves as Liquid Cash — a government
money-market fund or a fiat-pegged stablecoin — so it must never count as an
invested position in allocation or return figures.

Whether a ticker is one is the user's call. This list only seeds the guess
when a holding is first created: the verdict is stored on ``Asset.type`` and
no later sync overwrites it, so a correction from the asset editor sticks.
"""

from typing import Optional

CASH_EQUIVALENT_TYPE = "cash_equivalent"

# Government/treasury money-market funds and the major fiat-pegged stablecoins.
# Deliberately short: a ticker missing here costs one edit, a ticker wrongly
# here silently removes a real position from allocation.
CASH_EQUIVALENT_TICKERS = frozenset(
    {
        # Fidelity
        "SPAXX", "FDRXX", "FZFXX", "SPRXX", "FDLXX", "FZDXX",
        # Vanguard
        "VMFXX", "VMRXX", "VUSXX", "VMSXX",
        # Schwab
        "SWVXX", "SNVXX", "SNSXX", "SWGXX",
        # Stablecoins
        "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "PYUSD", "FDUSD",
        "GUSD", "EURC",
    }
)


def is_cash_equivalent_ticker(ticker: Optional[str]) -> bool:
    return bool(ticker) and ticker.strip().upper() in CASH_EQUIVALENT_TICKERS
