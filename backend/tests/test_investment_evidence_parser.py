"""Synthetic evidence keeps source facts separate from ledger proposals."""
from decimal import Decimal

import pytest

from app.services.investment_evidence_parser import parse_observations_csv


def test_source_money_and_original_timestamp_survive_without_rounding():
    result = parse_observations_csv(
        b"ID,Asset,Timestamp,Quantity Transacted,Transaction Type,Total,Subtotal,Fees and/or Spread,Price Currency\n"
        b"csv-row-Q,TKN,2026-01-02T12:30:40.123456789+02:30,12.00000000000000000123456789,Buy,84,82,3,USD\n",
        provider="coinbase", source_account_id="invented-account",
    )
    assert result["errors"] == []
    observation = result["observations"][0]
    leg = observation.legs[0]
    assert observation.source_local_id == "csv-row-Q"
    assert observation.event_time_raw == "2026-01-02T12:30:40.123456789+02:30"
    assert observation.time_precision == "fractional"
    assert observation.event_at.utcoffset().total_seconds() == 9000
    assert leg.quantity == Decimal("12.00000000000000000123456789")
    assert (leg.total, leg.subtotal, leg.fee) == (Decimal("84"), Decimal("82"), Decimal("3"))
    assert leg.valuation_amount is None
    assert leg.external_funding_amount is None
    assert leg.execution_currency == "USD"
    assert leg.valuation_currency is None


def test_a_withdrawal_value_is_neither_acquisition_basis_nor_funding():
    result = parse_observations_csv(
        b"ID,Instrument,Activity Date,Quantity,Trans Code,Market Value,Status,Network Status\n"
        b"withdrawal-W,TKN,2026-01-02,12,Withdrawal,84,completed,unconfirmed\n",
        provider="robinhood",
    )
    observation = result["observations"][0]
    leg = observation.legs[0]
    assert (leg.classification, leg.direction) == ("transfer", "out")
    assert leg.valuation_amount == Decimal("84")
    assert leg.total is leg.unit_price is leg.acquisition_basis is leg.external_funding_amount is None
    assert leg.fee is None
    assert (observation.provider_status, observation.network_status) == ("completed", "unconfirmed")
    assert observation.settlement_status == "pending"


@pytest.mark.parametrize("header,field", [
    ("USD Value", "valuation_amount"), ("Native Amount", "valuation_amount"),
    ("Fair Market Value", "valuation_amount"), ("Subtotal", "subtotal"),
])
def test_legacy_value_aliases_do_not_masquerade_as_acquisition_basis(header, field):
    result = parse_observations_csv(
        f"Asset,Date,Quantity,Kind,{header}\nTKN,2026-01-02,12,Withdrawal,84\n".encode(),
    )
    leg = result["observations"][0].legs[0]
    assert getattr(leg, field) == Decimal("84")
    assert leg.acquisition_basis is leg.external_funding_amount is None


@pytest.mark.parametrize("raw,precision,reason", [
    ("2026-01-02", "date", None),
    ("2026-01-02T12:30:40", "second", "unresolved_timezone"),
    ("", "unknown", "missing_event_time"),
    ("not-a-date", "unknown", "invalid_event_time"),
])
def test_a_clock_never_gets_an_invented_timezone_or_midnight(raw, precision, reason):
    result = parse_observations_csv(f"Asset,Date,Quantity\nTKN,{raw},12\n".encode())
    observation = result["observations"][0]
    assert observation.event_time_raw == (raw or None)
    assert observation.time_precision == precision
    assert observation.event_at is None
    assert observation.timezone is None
    if reason:
        assert reason in observation.reason_codes


def test_a_tax_workpaper_row_retains_both_dates_without_synthesizing_orders():
    result = parse_observations_csv(
        b"ID,Coin,Date Acquired,Date Sold,Quantity,Cost Basis,Proceeds\n"
        b"lot-L,TKN,2025-01-02,2026-01-02,12,84,96\n",
        source_kind="tax_workpaper",
    )
    assert len(result["observations"]) == 1
    observation = result["observations"][0]
    assert observation.source_local_id == "lot-L"
    assert observation.source_kind == "tax_workpaper"
    assert observation.legs[0].acquisition_basis == Decimal("84")
    assert observation.source_fields["disposal_date"] == "2026-01-02"
    assert observation.source_fields["proceeds"] == "96"


def test_mapping_can_be_corrected_or_unmapped_even_when_autodetection_succeeded():
    result = parse_observations_csv(
        b"Asset,Date,Quantity,Price,Transaction ID,Order ID,Private Notes\n"
        b"TKN,2026-01-02,12,7,chain-ref,order-ref,not-retained\n",
        column_mapping={"price": ""},
    )
    assert result["column_mapping"]["price"] == ""
    assert "Price" in result["unmapped_columns"]
    observation = result["observations"][0]
    assert observation.legs[0].unit_price is None
    assert observation.source_local_id is None
    assert observation.order_ref == "order-ref"
    assert observation.legs[0].transaction_ref == "chain-ref"
    assert "not-retained" not in observation.model_dump_json()


def test_equal_rows_keep_distinct_replay_locators_and_malformed_numbers_stay_visible():
    content = b"Asset,Date,Quantity,Price\nTKN,2026-01-02,12,7\nTKN,2026-01-02,12,7\nTKN,2026-01-02,NaN,7\n"
    first = parse_observations_csv(content, source_locator="one.csv")
    repeated = parse_observations_csv(content, source_locator="renamed.csv")
    refs = [observation.reference for observation in first["observations"]]
    assert len(set(refs)) == 3
    assert refs == [observation.reference for observation in repeated["observations"]]
    assert first["errors"] == [{"row": 4, "reason": "invalid_decimal", "field": "quantity"}]
    assert first["observations"][2].legs[0].quantity is None
    assert first["observations"][2].source_fields["quantity"] == "NaN"


def test_an_ambiguous_decimal_separator_requires_review_instead_of_changing_units():
    result = parse_observations_csv(b"Asset;Date;Quantity;Price\nTKN;2026-01-02;0,123;7\n")
    observation = result["observations"][0]
    assert observation.legs[0].quantity is None
    assert observation.source_fields["quantity"] == "0,123"
    assert "ambiguous_decimal_separator" in observation.reason_codes
    assert result["errors"] == [{"row": 2, "reason": "ambiguous_decimal_separator", "field": "quantity"}]


def test_an_ambiguous_date_keeps_its_raw_value_until_the_file_order_is_chosen():
    content = b"Asset,Date,Quantity\nTKN,01/02/2026,12\n"
    uncertain = parse_observations_csv(content)["observations"][0]
    assert uncertain.event_date is None
    assert uncertain.event_time_raw == "01/02/2026"
    assert "ambiguous_date_order" in uncertain.reason_codes
    chosen = parse_observations_csv(content, date_format="MM/DD/YYYY")["observations"][0]
    assert str(chosen.event_date) == "2026-01-02"


def test_execution_currency_and_native_valuation_currency_remain_separate():
    result = parse_observations_csv(
        b"Asset,Date,Quantity,Kind,Price,Price Currency,Native Amount,Native Currency\n"
        b"TKN,2026-01-02,12,Buy,7,USD,70,EUR\n",
    )
    leg = result["observations"][0].legs[0]
    assert (leg.unit_price, leg.execution_currency, leg.unit_price_origin) == (Decimal("7"), "USD", "reported")
    assert (leg.valuation_amount, leg.valuation_currency) == (Decimal("70"), "EUR")


def test_disagreeing_execution_currencies_are_retained_as_a_conflict():
    result = parse_observations_csv(
        b"Asset,Date,Quantity,Kind,Total,Total Currency,Subtotal,Subtotal Currency\n"
        b"TKN,2026-01-02,12,Buy,84,USD,82,EUR\n",
    )
    observation = result["observations"][0]
    assert observation.legs[0].execution_currency is None
    assert "monetary_currency_conflict" in observation.reason_codes
    assert observation.source_fields["total_currency"] == "USD"
    assert observation.source_fields["subtotal_currency"] == "EUR"
