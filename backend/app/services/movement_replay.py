"""Pure extension of the asset ledger for reviewed, non-sale movements."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from graphlib import TopologicalSorter
from heapq import heappop, heappush
from typing import Any

ZERO = Decimal("0")
KINDS = {"move_in", "move_out", "fee"}


def ordered(transactions):
    def key(tx):
        instant = tx.created_at or datetime.min.replace(tzinfo=timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return tx.date, instant, str(tx.id or "")
    rows = sorted(transactions, key=key)
    indices = {str(tx.id): index for index, tx in enumerate(rows) if tx.id}
    # A reviewed date-only ordering is a recorded relation, never a fake instant.
    dependencies = {index: {indices[identifier] for identifier in (tx.movement or {}).get("predecessor_transaction_ids", [])
                            if identifier in indices and indices[identifier] != index}
                    for index, tx in enumerate(rows)}
    order = TopologicalSorter(dependencies)
    order.prepare()
    ready, result = [], []
    while order.is_active():
        for index in order.get_ready():
            heappush(ready, index)
        index = heappop(ready)
        result.append(rows[index])
        order.done(index)
    return result


def replay(transactions):
    with localcontext(prec=128):
        return _replay(transactions)


def _replay(transactions):
    quantity = cost = realized = unknown_disposed = ZERO
    performance_complete = settlement_complete = True
    first_open = last_close = None
    lots, sales, events, missing = [], [], [], set()
    before = {}
    for tx in ordered(transactions):
        if quantity == 0 and settlement_complete:
            cost = ZERO
            performance_complete = True
        before[str(tx.id)] = deepcopy(lots)
        q = Decimal(str(tx.quantity))
        fee = Decimal(str(tx.fee or 0))
        movement = getattr(tx, "_movement_read", None) or tx.movement or {}
        if tx.kind in KINDS:
            reasons = movement.get("missing_links", [])
            missing.update(reasons)
            if movement.get("settlement_complete") is False:
                settlement_complete = performance_complete = False
                continue
            basis_valid = movement.get("basis_complete", True)
            carried = movement.get("performance_basis") if basis_valid else None
            if tx.kind == "move_in":
                fragments: list[dict[str, Any]] = deepcopy(movement.get("lots", []))
                if not fragments:
                    fragments = [{"lot_id": str(tx.id), "root_transaction_id": None,
                                  "source_leg_id": movement.get("leg_id"), "quantity": str(q),
                                  "acquired": None, "acquisition_cost": None, "lineage": [],
                                  "missing_links": ["acquisition_missing"]}]
                for index, lot in enumerate(fragments):
                    lot["quantity"] = Decimal(lot["quantity"])
                    invalid_roots = movement.get("invalid_root_transaction_ids", [])
                    lot_valid = basis_valid or bool(invalid_roots) and lot.get("root_transaction_id") not in invalid_roots
                    lot["acquisition_cost"] = Decimal(lot["acquisition_cost"]) if lot.get("acquisition_cost") is not None and lot_valid else None
                    if not lot_valid:
                        lot["acquired"] = None
                        lot["missing_links"] = sorted(set(lot.get("missing_links", [])) | set(reasons))
                    lot["lot_id"] = f"{tx.id}:{index}"
                    if lot["acquisition_cost"] is None:
                        missing.update(lot.get("missing_links", []) or ["acquisition_missing"])
                    lots.append(lot)
                quantity += q
                if carried is None:
                    performance_complete = False
                else:
                    cost += Decimal(carried)
                first_open = first_open or tx.date
                continue
            selections = movement.get("allocations", [])
            remaining = q
            for selection in selections:
                lot = next((lot for lot in lots if lot["lot_id"] == selection["lot_id"]), None)
                amount = Decimal(selection["quantity"])
                if lot is None or amount > lot["quantity"] or amount > remaining:
                    missing.add("recorded_lot_unavailable")
                    settlement_complete = performance_complete = False
                    continue
                if lot["acquisition_cost"] is not None:
                    lot["acquisition_cost"] -= lot["acquisition_cost"] * amount / lot["quantity"]
                lot["quantity"] -= amount
                remaining -= amount
            if remaining or q > quantity:
                missing.add("source_quantity_unavailable")
                settlement_complete = performance_complete = False
            # A quantity-only outbound can be supported while its tax effect is unknown.
            if quantity:
                cost -= cost * min(q, quantity) / quantity
            quantity -= min(q, quantity)
            lots = [lot for lot in lots if lot["quantity"] > 0]
            continue  # A transfer or fee is never a sale at a made-up price.
        if tx.kind == "buy":
            price = Decimal(str(tx.price)) if tx.price is not None else None
            basis = price * q + fee if price is not None else None
            lots.append({"lot_id": str(tx.id), "root_transaction_id": str(tx.id) if tx.id else None,
                         "source_leg_id": None, "quantity": q, "acquired": tx.date.isoformat(),
                         "acquisition_cost": basis, "lineage": [],
                         "missing_links": [] if basis is not None else ["acquisition_missing"]})
            quantity += q
            cost += basis if basis is not None else ZERO
            performance_complete &= basis is not None
            first_open = first_open or tx.date
            continue
        if tx.kind != "sell":
            raise ValueError(f"Unsupported asset ledger kind: {tx.kind}")
        closed = min(q, quantity)
        basis_unknown = sum((lot["quantity"] for lot in lots if lot["acquisition_cost"] is None), ZERO)
        gain = (Decimal(str(tx.price)) - cost / quantity) * closed - fee if quantity and performance_complete else None
        remaining = closed
        consumed = []
        for lot in sorted(lots, key=lambda lot: (lot.get("acquired") or "", lot["lot_id"])):
            take = min(lot["quantity"], remaining)
            if not take:
                continue
            piece = deepcopy(lot)
            piece["quantity"] = take
            piece["acquisition_cost"] = lot["acquisition_cost"] * take / lot["quantity"] if lot["acquisition_cost"] is not None else None
            consumed.append(piece)
            if lot["acquisition_cost"] is not None:
                lot["acquisition_cost"] -= piece["acquisition_cost"]
            lot["quantity"] -= take
            remaining -= take
        if gain is None:
            unknown_disposed += q
            missing.add("disposition_basis_unknown")
        else:
            realized += gain
        if q > quantity:
            settlement_complete = False
            missing.add("source_quantity_unavailable")
        sales.append({"transaction_id": str(tx.id), "date": tx.date, "quantity": q,
                      "gain": gain, "lots": consumed, "unknown_basis_quantity": min(q, basis_unknown)})
        events.append((tx.date, gain))
        if quantity:
            cost -= cost * closed / quantity
        quantity -= closed
        lots = [lot for lot in lots if lot["quantity"] > 0]
        last_close = tx.date
    known_quantity = sum((lot["quantity"] for lot in lots if lot["acquisition_cost"] is not None), ZERO)
    unknown_quantity = sum((lot["quantity"] for lot in lots if lot["acquisition_cost"] is None), ZERO)
    return {"units": quantity, "cost_basis": cost if performance_complete and settlement_complete else None,
            "average_price": cost / quantity if quantity and performance_complete and settlement_complete else None,
            "realized_gain": realized if not unknown_disposed and settlement_complete else None,
            "known_realized_gain": realized, "unknown_disposition_quantity": unknown_disposed,
            "realized_events": events, "first_open": first_open, "last_close": last_close,
            "lots": lots, "sales": sales, "before": before, "known_basis_quantity": known_quantity,
            "unknown_basis_quantity": unknown_quantity,
            "known_acquisition_cost": sum((lot["acquisition_cost"] for lot in lots if lot["acquisition_cost"] is not None), ZERO),
            "basis_complete": not unknown_quantity and settlement_complete,
            "settlement_complete": settlement_complete, "missing_links": sorted(missing)}
