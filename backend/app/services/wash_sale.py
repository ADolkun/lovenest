"""Cross-account Wash Sale exposure (#66).

A Wash Sale is selling at a loss and acquiring the same security within 30 days
either side, which disallows the loss (CONTEXT.md). Two things make it worth
warning about rather than merely computing:

- The rule spans *every* account a person holds, so a check that only looked at
  the selling wallet would miss the shape that matters most.
- When the replacement is bought inside an IRA the disallowed loss does not move
  into the replacement shares' basis — it is gone outright. That is the case
  this warning exists for.

This reports exposure only. No disallowed amount is rolled into any basis and no
adjusted basis is reported (#47, Out of Scope).
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.schemas.asset_group import REPORTABLE_TAX_TREATMENTS
from app.services.asset_transaction_service import _d

# 30 days either side of the sale, both ends inclusive — the 61-day window.
WINDOW_DAYS = 30

# Asset classes the rule reaches. An allowlist, like REPORTABLE_TAX_TREATMENTS:
# a class nobody has ruled on stays silent rather than warning wrongly. Crypto
# is deliberately absent — it is property, not a security, so the rule does not
# apply, and warning about it would teach the user to ignore the warning.
# `investment` is the generic bucket every synced brokerage holding lands in
# (`connection_service._upsert_asset_from_holding`), so leaving it out would
# make this check inert on exactly the portfolios it exists for.
COVERED_ASSET_TYPES = frozenset({"stock", "etf", "fund", "investment"})

# Wallets where a disallowed loss is forfeited rather than deferred: the basis
# adjustment that would normally recover it has nowhere to land.
UNRECOVERABLE_TAX_TREATMENTS = frozenset({"roth", "traditional", "hsa"})


def is_covered(asset_type: Optional[str]) -> bool:
    return (asset_type or "") in COVERED_ASSET_TYPES


def window(sell_date: date) -> tuple[date, date]:
    return sell_date - timedelta(days=WINDOW_DAYS), sell_date + timedelta(days=WINDOW_DAYS)


def in_window(acquired: date, sell_date: date) -> bool:
    start, end = window(sell_date)
    return start <= acquired <= end


def is_unrecoverable(tax_treatment: Optional[str]) -> bool:
    return (tax_treatment or "") in UNRECOVERABLE_TAX_TREATMENTS


def assess(
    *,
    asset_type: Optional[str],
    reportable: bool,
    at_loss: bool,
    sell_date: date,
    acquisitions: list[dict],
    holdings: list[dict],
) -> dict:
    """Decide whether a candidate sale is exposed, from facts already gathered.

    `acquisitions` are every buy of the same instrument in *any* wallet, each
    `{date, quantity, wallet, wallet_id, tax_treatment, same_wallet}`; only
    those inside the window match. `holdings` are the *other* wallets holding it
    right now, each `{wallet, wallet_id, tax_treatment}` — they matter whether
    or not they bought inside the window, since a wallet already holding the
    instrument is where a replacement would come from.

    `reportable` gates the warning on the *selling* wallet being Taxable: a loss
    realised in an IRA is no deduction to begin with, so there is nothing for a
    wash sale to disallow (CONTEXT.md, Reportable Gain).

    `wallets` is the list the warning names: every wallet that bought inside the
    window — the selling one included, since a repurchase there is a wash sale
    too — plus every other wallet holding it. Deduplicated on wallet identity
    rather than name, so two wallets sharing a name stay two warnings.
    """
    start, end = window(sell_date)
    covered = is_covered(asset_type)
    matches = sorted(
        (a for a in acquisitions if covered and in_window(a["date"], sell_date)),
        key=lambda a: a["date"],
    )
    named: dict = {}
    for entry in [*matches, *holdings]:
        named.setdefault(
            entry["wallet_id"],
            {
                "wallet": entry["wallet"],
                "wallet_id": entry["wallet_id"],
                "tax_treatment": entry["tax_treatment"],
                "unrecoverable": is_unrecoverable(entry.get("tax_treatment")),
            },
        )
    return {
        "asset_type": asset_type,
        "covered": covered,
        "reportable": reportable,
        "at_loss": at_loss,
        "sell_date": sell_date,
        "window_start": start,
        "window_end": end,
        "warning": bool(covered and reportable and at_loss and matches),
        "acquisitions": [
            {**a, "unrecoverable": is_unrecoverable(a.get("tax_treatment"))} for a in matches
        ],
        "wallets": list(named.values()),
    }


def _serialise(exposure: dict) -> dict:
    return {
        **exposure,
        "sell_date": exposure["sell_date"].isoformat(),
        "window_start": exposure["window_start"].isoformat(),
        "window_end": exposure["window_end"].isoformat(),
        "acquisitions": [
            {**a, "date": a["date"].isoformat(), "quantity": float(a["quantity"])}
            for a in exposure["acquisitions"]
        ],
    }


async def wash_sale_exposure(
    session: AsyncSession,
    asset_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    sell_date: Optional[date] = None,
    price: Optional[Decimal] = None,
) -> Optional[dict]:
    """Wash Sale exposure of selling this Holding on `sell_date` at `price`.

    The candidate price defaults to the last quote, and the sale counts as a
    loss when it lands below Average Price — the same weighted-average basis the
    ledger books a Realised Gain against, so the warning agrees with the figure
    the sale would actually produce.

    The instrument is matched by ticker across the whole workspace; a Holding
    with no ticker can only match its own ledger. "Substantially identical" is
    not attempted — no data here could support that judgement.
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

    siblings = (
        await session.execute(
            select(Asset, AssetGroup.name, AssetGroup.tax_treatment)
            .outerjoin(
                AssetGroup,
                (Asset.group_id == AssetGroup.id) & (AssetGroup.workspace_id == workspace_id),
            )
            .where(
                Asset.workspace_id == workspace_id,
                (Asset.ticker == asset.ticker) if asset.ticker else (Asset.id == asset.id),
            )
        )
    ).all()
    # A Holding in no wallet is its own identity, so two ungrouped Holdings of
    # the same ticker stay two entries instead of collapsing into one "no wallet".
    def _identity(sibling: Asset) -> str:
        return str(sibling.group_id or sibling.id)

    wallets = {
        sibling.id: (name, _identity(sibling), tax) for sibling, name, tax in siblings
    }

    buys = (
        (
            await session.execute(
                select(AssetTransaction).where(
                    AssetTransaction.asset_id.in_(list(wallets)),
                    AssetTransaction.kind == "buy",
                )
            )
        )
        .scalars()
        .all()
    )

    sell_date = sell_date or date.today()
    price = price if price is not None else asset.last_price
    average = asset.average_price
    exposure = assess(
        asset_type=asset.type,
        reportable=treatment in REPORTABLE_TAX_TREATMENTS,
        at_loss=price is not None and average is not None and _d(price) < _d(average),
        sell_date=sell_date,
        acquisitions=[
            {
                "date": buy.date,
                "quantity": _d(buy.quantity),
                "wallet": wallets[buy.asset_id][0],
                "wallet_id": wallets[buy.asset_id][1],
                "tax_treatment": wallets[buy.asset_id][2],
                "same_wallet": _identity(asset) == wallets[buy.asset_id][1],
            }
            for buy in buys
        ],
        holdings=[
            {"wallet": name, "wallet_id": _identity(sibling), "tax_treatment": tax}
            for sibling, name, tax in siblings
            if sibling.id != asset.id and not sibling.is_archived and _d(sibling.units) > 0
        ],
    )
    return _serialise(
        {
            **exposure,
            "asset_id": str(asset.id),
            "ticker": asset.ticker,
            "price": float(price) if price is not None else None,
            "average_price": float(average) if average is not None else None,
        }
    )
