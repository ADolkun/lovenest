"""Current holding values shared by asset reads and portfolio rollups."""
from decimal import Decimal

from app.models.asset import Asset
from app.services.option_contract import multiplier_for


def current_value_amount(asset: Asset, latest_amount: Decimal | None) -> Decimal | None:
    if asset.valuation_method == "market_price":
        if asset.last_price is not None and asset.units is not None:
            return (
                Decimal(str(asset.last_price))
                * Decimal(str(asset.units))
                * multiplier_for(asset.type)
            )
        return latest_amount
    if latest_amount is not None:
        return latest_amount
    return None if asset.purchase_price is None else Decimal(str(asset.purchase_price))
