"""Read source facts without turning transfers or lot assertions into orders."""
import csv
import hashlib
import io
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from app.schemas.investment_evidence import EvidenceLegInput, EvidenceObservationInput


def evidence_time(raw: str | None, formats=(), zone: str | None = None) -> tuple[dict, list[str]]:
    """Keep the original clock; only an explicit timezone establishes an instant."""
    if raw is not None and not isinstance(raw, str):
        return {"event_time_raw": None, "time_precision": "unknown"}, ["invalid_event_time"]
    result = {"event_time_raw": raw or None, "time_precision": "unknown"}
    if not raw:
        return result, ["missing_event_time"]
    value = raw.strip()
    parsed = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" UTC", "+00:00"))
    except ValueError:
        for fmt in formats:
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return result, ["invalid_event_time"]
    result["event_date"] = parsed.date()
    if not re.search(r"\d:\d|T\d", value):
        result["time_precision"] = "date"
        return result, []
    result["time_precision"] = (
        "fractional" if re.search(r":\d\d[.,]\d", value)
        else "second" if re.search(r"\d:\d\d:\d\d", value) else "minute"
    )
    if parsed.tzinfo is None and zone in {"UTC", "GMT", "Z"}:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.utcoffset() is None:
        result["timezone"] = zone
        return result, ["unresolved_timezone"]
    result["event_at"] = parsed
    result["timezone"] = parsed.tzname()
    return result, []


def evidence_decimal(raw) -> Decimal | None:
    """Parse a reported number directly; a JSON float already lost its precision."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (float, bool)):
        raise ValueError("Financial amounts must not pass through floats")
    # Reuse the established decimal/thousands separator convention.
    from app.services.import_service import normalize_amount

    value = str(raw).strip()
    value = re.sub(r"^[\$€£¥]\s*", "", value)
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1].lstrip("$€£¥ ")
    if re.fullmatch(r"[+-]?\d{1,3},\d{3}", value):
        raise ValueError("Ambiguous decimal separator")
    try:
        number = Decimal(normalize_amount(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid decimal amount") from exc
    if not number.is_finite():
        raise ValueError("Financial amounts must be finite")
    return number


def evidence_settlement(
    provider_status: str | None, network_status: str | None,
) -> Literal["settled", "pending", "failed", "unknown"]:
    provider = (provider_status or "").strip().lower()
    network = (network_status or "").strip().lower()
    failed = {"failed", "canceled", "cancelled", "rejected", "expired", "reversed"}
    pending = {"pending", "unconfirmed", "created", "processing", "waiting"}
    if provider in failed or network in failed:
        return "failed"
    if provider in pending or network in pending:
        return "pending"
    if provider in {"completed", "complete", "settled", "confirmed", "success", "filled"}:
        return "settled"
    return "unknown"


_EXTRA_COLUMNS = {
    "ticker": ("instrument",),
    "date": ("activity date",),
    "quantity": ("quantity transacted",),
    "price": ("price at transaction",),
    "kind": ("trans code",),
    "currency": ("price currency",),
    "execution_currency": ("execution currency",),
    "valuation_currency": ("valuation currency", "native currency", "value currency"),
    "unit_price_currency": ("unit price currency",),
    "subtotal_currency": ("subtotal currency",),
    "total_currency": ("total currency",),
    "external_id": ("external id", "id", "internal id", "row id", "record id"),
    "execution_id": ("execution id", "fill id", "trade id"),
    "transaction_ref": ("transaction id", "transaction hash", "txid", "tx hash"),
    "order_ref": ("order id", "order reference"),
    "leg_ref": ("leg id", "instruction index", "log index", "output index"),
    "total": ("total", "total (inclusive of fees and/or spread)", "total amount"),
    "subtotal": ("subtotal",),
    "fee": ("fees and/or spread",),
    "fee_currency": ("fee currency",),
    "valuation_amount": ("valuation", "market value", "fair market value", "usd value", "native amount"),
    "external_funding_amount": ("external funding amount", "funding amount"),
    "external_funding_currency": ("external funding currency", "funding currency"),
    "provider_status": ("status", "provider status"),
    "network_status": ("network status",),
    "chain": ("chain", "blockchain",),
    "token_address": ("token address", "mint", "contract address"),
    "provider_asset_id": ("asset id", "currency id"),
    "isin": ("isin",),
    "timezone": ("timezone", "time zone"),
    "historical_workspace_label": ("source workspace", "historical workspace"),
}
_MONEY_FIELDS = {
    "price": "unit_price", "cost_basis": "acquisition_basis", "total": "total",
    "subtotal": "subtotal", "fee": "fee", "valuation_amount": "valuation_amount",
    "external_funding_amount": "external_funding_amount",
}


def parse_observations_csv(
    content: bytes,
    column_mapping: dict[str, str] | None = None,
    date_format: str | None = None,
    *,
    source_kind: str = "primary_activity",
    provider: str = "csv",
    source_account_id: str | None = None,
    source_locator: str | None = None,
) -> dict:
    """Retain one observation per CSV row, including malformed/secondary evidence.

    Empty mappings explicitly unmap a field. The returned mapping and unused
    headers let a person correct a valid but wrongly detected mapping too.
    """
    from app.services.asset_import_service import (
        _COLUMN_CANDIDATES, _classify_kind, _date_formats, _decode, _normalize_header,
    )
    from app.services.import_service import _sniff_csv_dialect, infer_date_order

    validated_kind = TypeAdapter(Literal[
        "primary_activity", "balance_snapshot", "remaining_lots", "tax_workpaper", "recovery_notice",
    ]).validate_python(source_kind)
    text = _decode(content)
    reader = csv.DictReader(io.StringIO(text), dialect=_sniff_csv_dialect(text))
    reader.fieldnames = [f.strip() if f else f for f in (reader.fieldnames or [])]
    headers = [f for f in reader.fieldnames if f]
    if not headers or len(set(headers)) != len(headers):
        raise ValueError("CSV requires distinct column headers")
    candidates = {
        field: names for field, names in _COLUMN_CANDIDATES.items()
        if field not in {"external_id", "notes", "name"}
    }
    # An unqualified Amount is often money, and ISIN is not a ticker.
    candidates["quantity"] = tuple(n for n in candidates["quantity"] if n != "amount")
    candidates["ticker"] = tuple(n for n in candidates["ticker"] if n != "isin")
    candidates["cost_basis"] = ("cost basis", "cost basis remaining", "total cost", "basis", "book cost")
    for field, names in _EXTRA_COLUMNS.items():
        candidates[field] = candidates.get(field, ()) + names
    normalized = {_normalize_header(header): header for header in headers}
    mapping = dict(column_mapping or {})
    if mapping.keys() - candidates.keys():
        raise ValueError("Unknown evidence column mapping field")
    if any(header and header not in headers for header in mapping.values()):
        raise ValueError("Mapped evidence column is absent from the file")
    taken = {header for header in mapping.values() if header}
    for field, names in candidates.items():
        if field in mapping:
            continue
        header = next((normalized[n] for n in names if n in normalized and normalized[n] not in taken), None)
        if header:
            mapping[field] = header
            taken.add(header)
    if not any(mapping.values()):
        raise ValueError("No evidence columns recognized; choose a column mapping")

    def cell(row, field):
        value = row.get(mapping.get(field))
        return value.strip() if isinstance(value, str) else ""

    rows = list(reader)
    raw_dates = [cell(r, field) for r in rows for field in ("date", "date_sold")]
    formats = _date_formats(date_format, raw_dates)
    ambiguous_dates = not date_format and infer_date_order(raw_dates) is None
    digest = hashlib.sha256(content).hexdigest()
    unmapped = [header for header in headers if header not in taken]
    observations, errors = [], []
    observed_at = datetime.now(timezone.utc)
    for index, row in enumerate(rows, 2):
        if not any(value for value in row.values()):
            continue
        reasons = []
        if None in row:
            reasons.append("extra_csv_cells")
        time_fields, time_reasons = evidence_time(cell(row, "date"), formats, cell(row, "timezone") or None)
        reasons.extend(time_reasons)
        if ambiguous_dates and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", cell(row, "date")):
            time_fields["event_date"] = None
            reasons.append("ambiguous_date_order")
            errors.append({"row": index, "reason": "ambiguous_date_order", "field": "date"})
        values = {}
        for source_field, target_field in {"quantity": "quantity", **_MONEY_FIELDS}.items():
            raw = cell(row, source_field)
            try:
                values[target_field] = evidence_decimal(raw)
            except ValueError as exc:
                values[target_field] = None
                reason = "ambiguous_decimal_separator" if str(exc) == "Ambiguous decimal separator" else "invalid_decimal"
                reasons.append(reason if reason == "ambiguous_decimal_separator" else f"invalid_{target_field}")
                errors.append({"row": index, "reason": reason, "field": source_field})
        quantity = values.pop("quantity")
        kind = cell(row, "kind")
        meaning = _classify_kind(_normalize_header(kind)) if kind else None
        direction = "out" if quantity is not None and quantity < 0 else "in" if quantity else "unknown"
        if meaning in {"buy", "acquire"}:
            if quantity is not None and quantity < 0:
                reasons.append("direction_conflict")
            direction = "in"
        elif meaning == "sell":
            direction = "out"
        elif meaning == "transfer":
            words = set(_normalize_header(kind).split())
            if words & {"out", "withdraw", "withdrawal", "send", "sent"}:
                direction = "out"
            elif words & {"in", "deposit", "receive", "received"}:
                direction = "in"
        classification = (
            "buy" if meaning == "buy" else "sell" if meaning == "sell"
            else "acquisition" if meaning == "acquire" else "transfer" if meaning == "transfer"
            else "conversion" if meaning == "signed" else meaning or "unknown"
        )
        for field, value in values.items():
            if value is not None and value < 0:
                # Keep original monetary signs below, separate from magnitude/direction.
                values[field] = value.copy_abs()
                if field not in {"total", "subtotal", "valuation_amount", "external_funding_amount"}:
                    reasons.append(f"negative_{field}")
        execution_currencies = {
            cell(row, field).upper() for field in (
                "currency", "execution_currency", "unit_price_currency", "subtotal_currency", "total_currency",
            ) if cell(row, field)
        }
        if len(execution_currencies) > 1:
            reasons.append("monetary_currency_conflict")
        execution_currency = next(iter(execution_currencies)) if len(execution_currencies) == 1 else None
        valuation_currency = cell(row, "valuation_currency") or None
        # A generic row currency labels all its money; Price Currency only
        # labels execution pricing, not a separate native/displayed valuation.
        if not valuation_currency and _normalize_header(mapping.get("currency", "")) == "currency":
            valuation_currency = cell(row, "currency") or None
        source_fields = {
            target: cell(row, source_field) or None
            for source_field, target in {
                "quantity": "quantity", "date": "acquisition_date", "date_sold": "disposal_date",
                "proceeds": "proceeds", "kind": "classification",
                "currency": "execution_currency", "fee_currency": "fee_currency",
                "execution_currency": "execution_currency", "valuation_currency": "valuation_currency",
                "unit_price_currency": "unit_price_currency", "subtotal_currency": "subtotal_currency",
                "total_currency": "total_currency",
                "external_funding_currency": "external_funding_currency", **_MONEY_FIELDS,
            }.items() if mapping.get(source_field)
        }
        provider_status, network_status = cell(row, "provider_status") or None, cell(row, "network_status") or None
        external_id = cell(row, "external_id") or cell(row, "execution_id") or None
        try:
            observations.append(EvidenceObservationInput(
                reference=f"csv:{digest}:{index}", source="csv", provider=provider,
                source_kind=validated_kind, source_account_id=source_account_id,
                source_local_id=external_id, source_locator=f"{source_locator or 'csv'}#row={index}",
                observed_at=observed_at, **time_fields,
                provider_status=provider_status, network_status=network_status,
                settlement_status=evidence_settlement(provider_status, network_status),
                order_ref=cell(row, "order_ref") or None,
                historical_workspace_label=cell(row, "historical_workspace_label") or None,
                coverage=["source_history_bounds_unknown"] + (["unmapped_columns"] if unmapped else []),
                reason_codes=list(dict.fromkeys(reasons)), source_fields=source_fields,
                legs=[EvidenceLegInput(
                    key=cell(row, "leg_ref") or "row", asset_symbol=cell(row, "ticker").upper() or None,
                    direction=direction, classification=classification,
                    quantity=quantity.copy_abs() if quantity is not None else None, **values,
                    execution_currency=execution_currency, valuation_currency=valuation_currency,
                    unit_price_origin="reported" if values.get("unit_price") is not None else "unknown",
                    fee_currency=cell(row, "fee_currency") or None,
                    external_funding_currency=cell(row, "external_funding_currency") or None,
                    chain=cell(row, "chain") or None, token_address=cell(row, "token_address") or None,
                    provider_asset_id=cell(row, "provider_asset_id") or None, isin=cell(row, "isin") or None,
                    transaction_ref=cell(row, "transaction_ref") or None,
                    leg_ref=cell(row, "leg_ref") or None,
                    execution_id=cell(row, "execution_id") or None,
                )],
            ))
        except ValidationError as exc:
            for error in exc.errors(include_input=False, include_url=False):
                errors.append({"row": index, "reason": "invalid_source_field", "field": ".".join(map(str, error["loc"]))})
    return {
        "observations": observations, "errors": errors, "csv_columns": headers,
        "column_mapping": mapping, "unmapped_columns": unmapped,
    }
