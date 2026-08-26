"""Tax Lots, Holding Period and Realised Gain split by holding period (#65).

Lots are derived by replaying the trade ledger and never stored (ADR 0002), so
a Holding with no Trades — a Snapshot Holding — has no Lots at all, which is a
different answer from "one Lot of unknown date".

Two conventions meet here and neither wins outright:

- *How much* a sell gained is the weighted average (preço médio) figure the rest
  of the app already books, so a sale has exactly one Realised Gain regardless
  of who asks (CONTEXT.md, `asset_transaction_service._recompute`).
- *Which* units a sell consumed is FIFO, because holding period is per-lot and
  the average blends dates away. FIFO is the broker default and the only order
  that needs no input from the user.

So a sale's gain is the average-cost gain, attributed to long and short in the
proportion of the quantity FIFO consumed from each. The parts always sum back to
the Realised Gain the ledger reports. See ADR 0003.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.schemas.asset_group import REPORTABLE_TAX_TREATMENTS
from app.services.asset_transaction_service import _d, _recompute

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# Per-unit prices are carried at the scale `Asset.average_price` is stored at,
# so a lot price divided out of a fee is not reported to 28 digits.
_UNIT_PRICE = Decimal("0.000001")


def long_term_on(acquired: date) -> date:
    """The first day a lot bought on `acquired` reads as long-term.

    One year *or more* is long (CONTEXT.md), so the anniversary itself counts.
    29 February has no anniversary in a common year; 1 March is then the first
    day a full year has passed.
    """
    try:
        return acquired.replace(year=acquired.year + 1)
    except ValueError:
        return date(acquired.year + 1, 3, 1)


def is_long_term(acquired: date, as_of: date) -> bool:
    return as_of >= long_term_on(acquired)


def days_until_long_term(acquired: date, as_of: date) -> int:
    """How many days the lot still has to be held. Zero once it is long-term."""
    return max((long_term_on(acquired) - as_of).days, 0)


def _lot_view(acquired: date, quantity: Decimal, unit_price: Decimal, as_of: date) -> dict:
    return {
        "acquired": acquired,
        "quantity": quantity,
        "unit_price": unit_price.quantize(_UNIT_PRICE),
        # From the unrounded price, so a partially sold lot still reports the
        # cost of what is left of it rather than a rounded multiple.
        "cost": quantity * unit_price,
        # A buy dated in the future has been held no time at all, not a
        # negative one; `days_until_long_term` still counts from its own date.
        "holding_days": max((as_of - acquired).days, 0),
        "long_term": is_long_term(acquired, as_of),
        "days_until_long_term": days_until_long_term(acquired, as_of),
    }


def build_lots(transactions: list[AssetTransaction], *, as_of: date) -> dict:
    """Replay a Holding's ledger → its open Lots and its realised gain by
    holding period.

    Open lots are measured against `as_of` (today, for a position still open);
    a sale is measured against its own sell date. Fees are folded into the unit
    price of the lot they were paid on, matching how the ledger books cost.

    Returns open `lots` oldest-first, the long/short split of the open quantity
    and cost, and one record per `sale`.
    """
    txs = sorted(transactions, key=lambda t: (t.date, t.created_at or _EPOCH))
    # The ledger owns what a sell gained; this replay only decides which units
    # it took. Both sort on (date, created_at), so the nth sell reached here is
    # the nth realised event — asserted below rather than assumed.
    realised = _recompute(transactions)["realized_events"]
    open_lots: list[dict] = []  # {acquired, quantity, unit_price}
    sales: list[dict] = []

    for tx in txs:
        q = _d(tx.quantity)
        if tx.kind == "buy":
            gross = q * _d(tx.price) + _d(tx.fee)
            open_lots.append({"acquired": tx.date, "quantity": q, "unit_price": gross / q})
        elif tx.kind == "sell":
            sold_on, gain = realised[len(sales)]
            assert sold_on == tx.date, "ledger and lot replay disagree on sell order"

            remaining = q
            long_qty = Decimal("0")
            short_qty = Decimal("0")
            while remaining > 0 and open_lots:
                lot = open_lots[0]
                taken = lot["quantity"] if lot["quantity"] <= remaining else remaining
                if is_long_term(lot["acquired"], tx.date):
                    long_qty += taken
                else:
                    short_qty += taken
                lot["quantity"] -= taken
                remaining -= taken
                if lot["quantity"] == 0:
                    open_lots.pop(0)

            # What FIFO could actually take — the ledger clamps an oversell the
            # same way, by holding the position at zero rather than going short.
            sell_qty = long_qty + short_qty
            long_gain = (gain * long_qty / sell_qty) if sell_qty > 0 else Decimal("0")
            sales.append({
                "date": tx.date,
                "quantity": sell_qty,
                "gain": gain,
                "long_quantity": long_qty,
                "short_quantity": short_qty,
                "long_gain": long_gain,
                # Subtracted rather than computed, so the two parts always sum
                # back to the gain even where the ratio does not divide evenly.
                "short_gain": gain - long_gain,
            })

    lots = [_lot_view(lot["acquired"], lot["quantity"], lot["unit_price"], as_of) for lot in open_lots]
    return {
        "as_of": as_of,
        "lots": lots,
        "long_quantity": sum((lot["quantity"] for lot in lots if lot["long_term"]), Decimal("0")),
        "short_quantity": sum((lot["quantity"] for lot in lots if not lot["long_term"]), Decimal("0")),
        "long_cost": sum((lot["cost"] for lot in lots if lot["long_term"]), Decimal("0")),
        "short_cost": sum((lot["cost"] for lot in lots if not lot["long_term"]), Decimal("0")),
        "sales": sales,
        "realised_long": sum((s["long_gain"] for s in sales), Decimal("0")),
        "realised_short": sum((s["short_gain"] for s in sales), Decimal("0")),
    }


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _split_to_cents(total: Decimal, long_part: Decimal) -> tuple[float, float]:
    """Round a gain and its long half to cents, then take the short half as
    what is left — so the two parts still add up to the whole after rounding.
    Rounding each half on its own loses a cent on any 50/50 split of an odd
    number of them, which is the invariant ADR 0003 rests on."""
    whole = _money(total)
    long_cents = _money(long_part)
    return long_cents, round(whole - long_cents, 2)


def _serialise(position: dict) -> dict:
    realised_long, realised_short = _split_to_cents(
        sum((sale["gain"] for sale in position["sales"]), Decimal("0")),
        position["realised_long"],
    )
    sales = []
    for sale in position["sales"]:
        long_gain, short_gain = _split_to_cents(sale["gain"], sale["long_gain"])
        sales.append({
            "date": sale["date"].isoformat(),
            "quantity": float(sale["quantity"]),
            "gain": _money(sale["gain"]),
            "long_quantity": float(sale["long_quantity"]),
            "short_quantity": float(sale["short_quantity"]),
            "long_gain": long_gain,
            "short_gain": short_gain,
        })
    return {
        "as_of": position["as_of"].isoformat(),
        "lots": [
            {
                "acquired": lot["acquired"].isoformat(),
                "quantity": float(lot["quantity"]),
                "unit_price": float(lot["unit_price"]),
                "cost": _money(lot["cost"]),
                "holding_days": lot["holding_days"],
                "long_term": lot["long_term"],
                "days_until_long_term": lot["days_until_long_term"],
            }
            for lot in position["lots"]
        ],
        "long_quantity": float(position["long_quantity"]),
        "short_quantity": float(position["short_quantity"]),
        "long_cost": _money(position["long_cost"]),
        "short_cost": _money(position["short_cost"]),
        "sales": sales,
        "realised_long": realised_long,
        "realised_short": realised_short,
    }


_EMPTY = {
    "lots": [],
    "long_quantity": 0.0,
    "short_quantity": 0.0,
    "long_cost": 0.0,
    "short_cost": 0.0,
    "sales": [],
    "realised_long": 0.0,
    "realised_short": 0.0,
}


async def asset_tax_lots(
    session: AsyncSession,
    asset_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    as_of: Optional[date] = None,
) -> Optional[dict]:
    """The Lots of one Holding, with tax character only where it has one.

    A Holding in a wallet that is not Taxable reports `tax_character: false` and
    no lots: its gains are never Reportable, so a long-versus-short answer there
    is noise dressed as advice. The Lots themselves stay derivable — the Wash
    Sale rule reaches into an IRA (ADR 0002) — they just do not surface here.

    `snapshot` marks a Holding whose quantity came from a provider with no
    Trades behind it: Holding Period is *unknown*, not short (ADR 0002).

    `no_wallet` separates the third empty case: a Holding in no wallet has no
    treatment to gate on, so it falls into the same silent branch as a
    Tax-Advantaged one while meaning something else entirely.
    """
    row = (
        await session.execute(
            select(Asset, AssetGroup.tax_treatment)
            .outerjoin(
                AssetGroup,
                (Asset.group_id == AssetGroup.id) & (AssetGroup.workspace_id == workspace_id),
            )
            .where(Asset.id == asset_id, Asset.workspace_id == workspace_id)
        )
    ).first()
    if row is None:
        return None
    asset, treatment = row

    txs = list(
        (
            await session.execute(
                select(AssetTransaction).where(AssetTransaction.asset_id == asset_id)
            )
        )
        .scalars()
        .all()
    )
    as_of = as_of or date.today()
    head = {
        "asset_id": str(asset.id),
        "ticker": asset.ticker,
        "tax_character": treatment in REPORTABLE_TAX_TREATMENTS,
        "snapshot": not txs and _d(asset.units) > 0,
        # Distinct from a tax-advantaged wallet: there is no wallet to read a
        # treatment from, so the emptiness is a missing answer, not an answer.
        "no_wallet": asset.group_id is None,
    }
    if not head["tax_character"]:
        return {**head, "as_of": as_of.isoformat(), **_EMPTY}
    return {**head, **_serialise(build_lots(txs, as_of=as_of))}
