"""Synthetic Coinbase HTTP -> sync -> CSV HTTP review/application boundaries."""
import csv
import io
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from app.agents.services.crypto import encrypt
from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.bank_connection import BankConnection
from app.models.investment_evidence import InvestmentEvent, InvestmentLeg, InvestmentObservation
from app.models.workspace import Workspace, WorkspaceMember
from app.providers import register_provider
from app.providers.coinbase import CoinbaseProvider
from app.services.connection_service import _sync_holdings, _sync_trades

pytestmark = pytest.mark.asyncio

PORTFOLIO = "synthetic-portfolio"
WALLET = "synthetic-currency-wallet"
ASSET_ID = "synthetic-currency-identity"
WHEN = "2026-01-02T12:00:00Z"


def _api_row(identifier="api-trade-A", *, quantity="12", amount="84", kind="buy", **changes):
    return {
        "id": identifier, "type": kind, "status": "completed", "created_at": WHEN,
        "amount": {"amount": quantity, "currency": "SYN"},
        "native_amount": {"amount": amount, "currency": "USD"}, **changes,
    }


def _csv_row(identifier="csv-row-Q", **changes):
    return {
        "ID": identifier, "Asset": "SYN", "Date": WHEN, "Quantity": "12", "Price": "7",
        "Fee": "0", "Currency": "USD", "Kind": "Buy", "Status": "completed",
        "Execution ID": f"execution-{identifier}", "Asset ID": ASSET_ID, **changes,
    }


def _csv_bytes(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(dict.fromkeys(key for row in rows for key in row)))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def _json(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest_asyncio.fixture
async def venue(session, test_workspace, test_user, client, auth_headers, monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()
    credentials = {"key_name": "organizations/synthetic/apiKeys/transient", "private_key_enc": encrypt(private)}
    connection = BankConnection(
        workspace_id=test_workspace.id, user_id=test_user.id, provider="coinbase",
        external_id="synthetic-connection", institution_name="Synthetic Exchange", credentials=credentials,
    )
    session.add(connection)
    await session.flush()
    group = AssetGroup(
        workspace_id=test_workspace.id, user_id=test_user.id, name="Synthetic exchange wallet",
        connection_id=connection.id, source="coinbase",
        external_id=f"{connection.external_id}::{PORTFOLIO}",
    )
    session.add(group)
    await session.commit()
    state = SimpleNamespace(
        session=session, workspace=test_workspace, user=test_user, client=client,
        headers={**auth_headers, "X-Workspace-Id": str(test_workspace.id)}, connection=connection,
        group=group, credentials=credentials, history={WALLET: [_api_row()]}, requests=[],
        accounts=[{
            "id": WALLET, "name": "Synthetic coin wallet", "type": "wallet", "portfolio_id": PORTFOLIO,
            "balance": {"amount": "12", "currency": "SYN"},
            "currency": {"code": "SYN", "type": "crypto", "id": ASSET_ID},
        }],
    )

    def transport(request):
        state.requests.append(str(request.url))
        if request.url.path == "/v2/accounts":
            return httpx.Response(200, json={"data": state.accounts, "pagination": {}})
        if request.url.path == "/v2/exchange-rates":
            return httpx.Response(200, json={"data": {"currency": "USD", "rates": {"SYN": "1", "ALT": "1", "USD": "1"}}})
        if request.url.path.endswith("/transactions"):
            key = request.url.path.split("/")[3]
            assert key in state.history, f"Unexpected synthetic wallet: {key}"
            return httpx.Response(200, json={"data": state.history[key], "pagination": {}})
        raise AssertionError(f"Unexpected network request: {request.url}")

    async def fake_client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url="https://api.coinbase.com")

    monkeypatch.setattr(CoinbaseProvider, "_client", fake_client)
    register_provider("coinbase", CoinbaseProvider)
    return state


async def _sync(v):
    await _sync_holdings(v.session, v.user.id, v.connection, v.credentials, {PORTFOLIO})
    await _sync_trades(v.session, v.connection, v.credentials, {PORTFOLIO})
    await v.session.commit()


async def _rows(v, model):
    return list((await v.session.scalars(
        select(model).execution_options(populate_existing=True),
    )).all())


async def _preview(v, rows, *, source_kind="primary_activity", mode="evidence", opening_boundary=None):
    data = {
        "mode": mode, "provider": "coinbase", "group_id": str(v.group.id),
        "connection_id": str(v.connection.id), "source_account_id": WALLET, "source_kind": source_kind,
    }
    if opening_boundary is not None:
        data["opening_boundary"] = json.dumps(opening_boundary)
    return _json(await v.client.post(
        "/api/assets/import/preview", headers=v.headers, data=data,
        files={"file": ("synthetic.csv", _csv_bytes(rows), "text/csv")},
    ))


async def _upload(v, rows, **kwargs):
    preview = await _preview(v, rows, **kwargs)
    payload = {
        "mode": kwargs.get("mode", "evidence"), "group_id": str(v.group.id),
        "connection_id": str(v.connection.id), "filename": "synthetic.csv", "allow_unpriced": True,
        "observations": preview["evidence"]["observations"], "expected_revision": preview["evidence"]["revision"],
    }
    if kwargs.get("opening_boundary"):
        payload["opening_boundary"] = kwargs["opening_boundary"]
    return _json(await v.client.post("/api/assets/import", headers=v.headers, json=payload))


async def _evidence(v, opening_boundary=None):
    params = {"group_id": str(v.group.id)}
    if opening_boundary:
        params.update({
            "opening_as_of": opening_boundary["as_of"], "opening_assumption": opening_boundary["assumption"],
            "overlap_reviewed": str(opening_boundary["overlap_reviewed"]).lower(),
        })
    return _json(await v.client.get("/api/assets/evidence", headers=v.headers, params=params))


def _record(preview, source_id):
    observation = next(o for o in preview["observations"] if o["source_local_id"] == source_id)
    return next(r for r in preview["records"] if r["observation_ref"] == observation["reference"])


async def _confirm(v, preview, decisions, status=200, **kwargs):
    return _json(await v.client.post("/api/assets/evidence/confirm", headers=v.headers, json={
        "group_id": str(v.group.id), "expected_revision": preview["revision"],
        "decisions": decisions, "allow_unpriced": True, **kwargs,
    }), status)


def _apply_decision(record, **kwargs):
    return {"observation_ref": record["observation_ref"], "leg_key": record["leg_key"], "action": "apply", **kwargs}


def _link_decision(record, allocations=None):
    return {
        "observation_ref": record["observation_ref"], "leg_key": record["leg_key"], "action": "link",
        "reason": "Reviewed synthetic source order documents these executions",
        "allocations": allocations or [{"leg_id": record["candidate_legs"][0]["leg_id"], "quantity": "12"}],
    }


@pytest.mark.parametrize("csv_first", [False, True])
async def test_real_provider_and_csv_api_apply_documented_purchase_once_in_either_order(venue, csv_first):
    v = venue
    if csv_first:
        stored = await _upload(v, [_csv_row()])
        await _confirm(v, stored["evidence"], [_apply_decision(_record(stored["evidence"], "csv-row-Q"))])
    await _sync(v)
    if not csv_first:
        await _upload(v, [_csv_row()])
    preview = await _evidence(v)
    pending_id = "api-trade-A" if csv_first else "csv-row-Q"
    candidate = _record(preview, pending_id)
    assert candidate["match_status"] == "candidate"
    assert len(await _rows(v, Asset)) == len(await _rows(v, AssetTransaction)) == 1
    decision = _link_decision(candidate)
    linked = await _confirm(v, preview, [decision])
    assert linked["imported"] == 0 and linked["linked"] == 1
    assert (await _confirm(v, preview, [decision]))["imported"] == 0
    await _sync(v)
    replayed = await _upload(v, [_csv_row()])
    assert replayed["retained"] == replayed["imported"] == 0
    ledger = await _rows(v, AssetTransaction)
    assert len(ledger) == 1
    assert ledger[0].quantity == Decimal("12") and ledger[0].price == Decimal("7")
    assert {o.payload["source_local_id"] for o in await _rows(v, InvestmentObservation)} == {"api-trade-A", "csv-row-Q"}
    assert (await _rows(v, Asset))[0].units == Decimal("12")


async def test_equal_economics_with_distinct_verified_execution_ids_survive_http_apply(venue):
    v = venue
    stored = await _upload(v, [_csv_row("execution-A"), _csv_row("execution-B")])
    decisions = [_apply_decision(record) for record in stored["evidence"]["records"]]
    applied = await _confirm(v, stored["evidence"], decisions)
    assert applied["imported"] == 2
    assert len(await _rows(v, AssetTransaction)) == 2
    assert (await _rows(v, Asset))[0].units == Decimal("24")


async def test_anonymous_equal_executions_remain_candidates_without_financial_application(venue):
    stored = await _upload(venue, [_csv_row("", **{"Execution ID": ""}), _csv_row("", **{"Execution ID": ""})])
    records = stored["evidence"]["records"]
    assert len(records) == 2 and all(r["match_status"] == "candidate" for r in records)
    await _confirm(venue, stored["evidence"], [_apply_decision(records[0])], status=422)
    assert await _rows(venue, AssetTransaction) == []


async def test_reviewed_one_to_many_csv_order_links_to_two_real_api_fills(venue):
    v = venue
    v.history[WALLET] = [
        _api_row("api-fill-1", quantity="5", amount="35", advanced_trade_fill={"order_id": "order-A"}, kind="advanced_trade_fill"),
        _api_row("api-fill-2", quantity="7", amount="49", advanced_trade_fill={"order_id": "order-A"}, kind="advanced_trade_fill"),
    ]
    await _sync(v)
    stored = await _upload(v, [_csv_row(**{"Order ID": "order-A"})])
    record = _record(stored["evidence"], "csv-row-Q")
    assert len(record["candidate_legs"]) == 2
    allocations = [{"leg_id": c["leg_id"], "quantity": c["quantity"]} for c in record["candidate_legs"]]
    linked = await _confirm(v, stored["evidence"], [_link_decision(record, allocations)])
    assert linked["linked"] == 2 and linked["imported"] == 0
    assert len(await _rows(v, AssetTransaction)) == 2
    assert (await _rows(v, Asset))[0].units == Decimal("12")


@pytest.mark.parametrize("has_order", [True, False])
async def test_conversion_keeps_both_real_provider_legs_and_only_documents_grouping_with_order(venue, has_order):
    v = venue
    second = {
        "id": "synthetic-alt-wallet", "name": "Synthetic second coin", "type": "wallet", "portfolio_id": PORTFOLIO,
        "balance": {"amount": "0", "currency": "ALT"},
        "currency": {"code": "ALT", "type": "crypto", "id": "synthetic-alt-identity"},
    }
    v.accounts.append(second)
    reference = {"network": {"hash": "synthetic-shared-conversion-transaction", "status": "confirmed"}}
    if has_order:
        reference["trade"] = {"id": "conversion-order"}
    v.history[WALLET] = [_api_row("convert-in", kind="trade", **reference)]
    outgoing = _api_row("convert-out", kind="trade", quantity="-6", amount="-84", created_at="2026-01-02T12:00:02Z", **reference)
    outgoing["amount"]["currency"] = "ALT"
    v.history[second["id"]] = [outgoing]
    await _sync(v)
    observations = await _rows(v, InvestmentObservation)
    assert {o.payload["source_local_id"] for o in observations} == {"convert-in", "convert-out"}
    assert len(await _rows(v, InvestmentLeg)) == 2
    assert {o.payload["legs"][0]["transaction_ref"] for o in observations} == {"synthetic-shared-conversion-transaction"}
    if has_order:
        assert len(await _rows(v, InvestmentEvent)) == 1
    else:
        assert len(await _rows(v, InvestmentEvent)) == 2
        preview = await _evidence(v)
        assert any(r["match_status"] == "candidate" for r in preview["records"])


async def test_conflicting_total_and_valued_withdrawal_are_retained_without_invented_basis(venue):
    v = venue
    stored = await _upload(v, [
        _csv_row("bad-total", **{"Quantity": "10", "Price": "8.2", "Subtotal": "82", "Total": "84", "Fee": "3"}),
        _csv_row("withdrawal", **{"Kind": "Withdrawal", "Price": "", "Fee": "", "Market Value": "84"}),
    ])
    mismatch = _record(stored["evidence"], "bad-total")
    assert mismatch["application_status"] == "blocked" and "total" in mismatch["conflicting_fields"]
    await _confirm(v, stored["evidence"], [_apply_decision(mismatch)], status=422)
    withdrawal = next(o for o in stored["evidence"]["observations"] if o["source_local_id"] == "withdrawal")["legs"][0]
    assert Decimal(withdrawal["valuation_amount"]) == Decimal("84")
    assert withdrawal["acquisition_basis"] is withdrawal["external_funding_amount"] is None
    assert await _rows(v, AssetTransaction) == []


@pytest.mark.parametrize("source_kind", ["remaining_lots", "tax_workpaper"])
async def test_secondary_lot_or_workpaper_http_import_never_adds_inventory(venue, source_kind):
    await _sync(venue)
    stored = await _upload(venue, [_csv_row("secondary", **{"Cost Basis": "84"})], source_kind=source_kind)
    record = _record(stored["evidence"], "secondary")
    assert record["application_status"] == "not_applicable"
    await _confirm(venue, stored["evidence"], [_apply_decision(record)], status=422)
    assert len(await _rows(venue, AssetTransaction)) == 1
    assert (await _rows(venue, Asset))[0].units == Decimal("12")


async def test_unknown_fee_blocks_a_reported_price_but_preserves_null_fee(venue):
    stored = await _upload(venue, [_csv_row(**{"Fee": ""})])
    record = _record(stored["evidence"], "csv-row-Q")
    assert record["application_status"] == "blocked"
    assert "unknown_fee" in record["reason_codes"]
    assert stored["evidence"]["observations"][0]["legs"][0]["fee"] is None
    await _confirm(venue, stored["evidence"], [_apply_decision(record)], status=422)
    assert await _rows(venue, AssetTransaction) == []


@pytest.mark.parametrize("different_workspace", [False, True])
async def test_reused_provider_id_in_another_connection_or_workspace_never_crosslinks(venue, different_workspace):
    v = venue
    await _sync(v)
    primary_leg = (await _rows(v, InvestmentLeg))[0]
    workspace = v.workspace
    if different_workspace:
        workspace = Workspace(
            id=uuid.uuid4(), name="Synthetic second workspace", kind="personal",
            created_by_user_id=v.user.id, default_currency="USD",
        )
        v.session.add(workspace)
        await v.session.flush()
        v.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=v.user.id, role="owner"))
    connection = BankConnection(
        workspace_id=workspace.id, user_id=v.user.id, provider="coinbase",
        external_id="synthetic-second-connection", institution_name="Synthetic Second Exchange",
        credentials=v.credentials,
    )
    v.session.add(connection)
    await v.session.flush()
    group = AssetGroup(
        workspace_id=workspace.id, user_id=v.user.id, name="Synthetic second wallet",
        connection_id=connection.id, source="coinbase", external_id=f"{connection.external_id}::{PORTFOLIO}",
    )
    v.session.add(group)
    await v.session.commit()
    second = SimpleNamespace(**{**vars(v), "workspace": workspace, "connection": connection, "group": group,
                                "headers": {**v.headers, "X-Workspace-Id": str(workspace.id)}})
    await _sync(second)
    preview = await _evidence(second)
    observation = next(o for o in preview["observations"] if o["source_local_id"] == "api-trade-A")
    record = next(r for r in preview["records"] if r["observation_ref"] == observation["reference"])
    assert all(c["leg_id"] != str(primary_leg.id) for c in record["candidate_legs"])
    await _confirm(second, preview, [_link_decision(record, [{"leg_id": str(primary_leg.id), "quantity": "12"}])], status=404)
    observations = await _rows(v, InvestmentObservation)
    assert len(observations) == 2
    assert {o.connection_id for o in observations} == {v.connection.id, connection.id}
    assert {o.payload["source_local_id"] for o in observations} == {"api-trade-A"}
    if different_workspace:
        response = await v.client.get("/api/assets/evidence", headers=v.headers, params={"group_id": str(group.id)})
        assert response.status_code == 404


async def test_reported_asset_identity_conflict_cannot_apply_to_same_ticker_holding(venue):
    v = venue
    await _sync(v)
    stored = await _upload(v, [_csv_row("wrong-identity", **{"Asset ID": "different-reported-currency-id"})])
    record = _record(stored["evidence"], "wrong-identity")
    await _confirm(v, stored["evidence"], [_apply_decision(record)], status=422)
    assert len(await _rows(v, AssetTransaction)) == 1
    assert (await _rows(v, Asset))[0].units == Decimal("12")


async def test_csv_clock_precision_and_independent_statuses_roundtrip_through_storage(venue):
    v = venue
    stored = await _upload(v, [
        _csv_row("date-only", **{"Date": "2026-01-02", "Kind": "Withdrawal", "Source Workspace": "Untrusted historical label"}),
        _csv_row("naive-time", **{"Date": "2026-01-02T12:00:00.123456789", "Kind": "Withdrawal"}),
        _csv_row("offset-time", **{"Date": "2026-01-02T12:00:00.123456789+02:30", "Kind": "Withdrawal"}),
        _csv_row("unconfirmed", **{"Network Status": "unconfirmed", "Kind": "Withdrawal"}),
    ])
    assert stored["retained"] == 4
    saved = {o["source_local_id"]: o for o in (await _evidence(v))["observations"]}
    assert saved["date-only"]["time_precision"] == "date"
    assert saved["date-only"]["event_at"] is saved["date-only"]["timezone"] is None
    assert saved["date-only"]["historical_workspace_label"] == "Untrusted historical label"
    assert stored["evidence"]["target"]["workspace_id"] == str(v.workspace.id)
    assert saved["naive-time"]["event_time_raw"] == "2026-01-02T12:00:00.123456789"
    assert saved["naive-time"]["time_precision"] == "fractional"
    assert saved["naive-time"]["event_at"] is saved["naive-time"]["timezone"] is None
    assert saved["offset-time"]["event_time_raw"] == "2026-01-02T12:00:00.123456789+02:30"
    assert saved["offset-time"]["event_at"].endswith("+02:30")
    assert saved["offset-time"]["time_precision"] == "fractional"
    assert (saved["unconfirmed"]["provider_status"], saved["unconfirmed"]["network_status"]) == ("completed", "unconfirmed")
    assert saved["unconfirmed"]["settlement_status"] == "pending"
    assert await _rows(v, AssetTransaction) == []


async def test_failed_api_transfer_retains_fee_without_settling_attempted_units(venue):
    v = venue
    v.history[WALLET].append(_api_row(
        "failed-transfer", kind="send", quantity="-12", amount="-84", status="failed",
        network={"status": "failed", "transaction_fee": {"amount": "0.0000000000000000001", "currency": "SYN"}},
    ))
    await _sync(v)
    preview = await _evidence(v)
    failed = next(o for o in preview["observations"] if o["source_local_id"] == "failed-transfer")
    assert failed["settlement_status"] == "failed"
    assert failed["legs"][0]["direction"] == "out"
    assert Decimal(failed["legs"][0]["quantity"]) == Decimal("12")
    assert Decimal(failed["legs"][0]["fee"]) == Decimal("0.0000000000000000001")
    assert failed["legs"][0]["fee_currency"] == "SYN"
    assert len(await _rows(v, AssetTransaction)) == 1
    reconciliation = next(r for r in preview["reconciliation"] if r["asset_symbol"] == "SYN")
    assert Decimal(reconciliation["settled_movement_quantity"]) == Decimal("12")
    assert reconciliation["history_complete"] is reconciliation["basis_complete"] is False


async def test_unlink_and_import_undo_preserve_api_primary_and_all_observations(venue):
    v = venue
    await _sync(v)
    original = (await _rows(v, AssetTransaction))[0].id
    stored = await _upload(v, [_csv_row()])
    record = _record(stored["evidence"], "csv-row-Q")
    linked = await _confirm(v, stored["evidence"], [_link_decision(record)])
    linked_record = _record(linked["evidence"], "csv-row-Q")
    unlinked = _json(await v.client.delete(
        f"/api/assets/evidence/links/{linked_record['link_ids'][0]}", headers=v.headers,
        params={"expected_revision": linked["evidence"]["revision"]},
    ))
    assert _record(unlinked, "csv-row-Q")["application_status"] == "already_applied"
    undone = await v.client.delete(f"/api/import-logs/{stored['import_log_id']}", headers=v.headers)
    assert undone.status_code == 204, undone.text
    await _sync(v)
    await _upload(v, [_csv_row()])
    assert [tx.id for tx in await _rows(v, AssetTransaction)] == [original]
    assert len(await _rows(v, InvestmentObservation)) == 2


async def test_undo_of_owned_csv_application_cannot_replay_it_into_existence(venue):
    v = venue
    stored = await _upload(v, [_csv_row()])
    applied = await _confirm(v, stored["evidence"], [_apply_decision(_record(stored["evidence"], "csv-row-Q"))])
    undone = await v.client.delete(f"/api/import-logs/{applied['import_log_id']}", headers=v.headers)
    assert undone.status_code == 204, undone.text
    assert await _rows(v, AssetTransaction) == []
    assert len(await _rows(v, InvestmentObservation)) == 1
    replay = await _upload(v, [_csv_row()])
    record = _record(replay["evidence"], "csv-row-Q")
    assert record["application_status"] == "blocked"
    assert "application_reversed" in record["reason_codes"]
    repeated = await _confirm(v, replay["evidence"], [_apply_decision(record)])
    assert repeated["imported"] == 0
    assert await _rows(v, AssetTransaction) == []


async def test_opening_lots_require_and_persist_server_review_boundary(venue):
    v = venue
    stored = await _upload(
        v, [_csv_row("opening-lot", **{"Cost Basis": "84", "Date": "2025-12-31"})],
        mode="opening_lots", source_kind="remaining_lots",
    )
    record = _record(stored["evidence"], "opening-lot")
    await _confirm(v, stored["evidence"], [_apply_decision(record)], status=422)
    boundary = {"as_of": "2026-01-01", "overlap_reviewed": False, "assumption": "Synthetic reviewed opening inventory before this account history"}
    await _confirm(v, stored["evidence"], [_apply_decision(record)], status=409, opening_boundary=boundary)
    preview = await _evidence(v, boundary)
    await _confirm(v, preview, [_apply_decision(record)], status=422, opening_boundary=boundary)
    boundary["overlap_reviewed"] = True
    preview = await _evidence(v, boundary)
    applied = await _confirm(v, preview, [_apply_decision(record)], opening_boundary=boundary)
    assert applied["imported"] == 1
    events = await _rows(v, InvestmentEvent)
    assert len(events) == 1 and events[0].opening_boundary == boundary
    assert (await _rows(v, InvestmentObservation))[0].payload["source_kind"] == "remaining_lots"


async def test_tax_workpaper_classification_conflict_is_visible_even_when_quantity_matches(venue):
    v = venue
    await _sync(v)
    stored = await _upload(v, [_csv_row("workpaper", **{"Kind": "Reward"})], source_kind="tax_workpaper")
    record = _record(stored["evidence"], "workpaper")
    assert record["match_status"] == "conflicting"
    assert "classification" in record["conflicting_fields"]
    assert len(await _rows(v, AssetTransaction)) == 1


async def test_csv_first_without_precise_asset_id_cannot_double_the_portfolio_after_sync(venue):
    v = venue
    stored = await _upload(v, [_csv_row(**{"Asset ID": ""})])
    record = _record(stored["evidence"], "csv-row-Q")
    if record["application_status"] == "blocked":
        await _confirm(v, stored["evidence"], [_apply_decision(record)], status=422)
    else:
        await _confirm(v, stored["evidence"], [_apply_decision(record)])
    await _sync(v)
    active = [asset for asset in await _rows(v, Asset) if not asset.is_archived and asset.sell_date is None]
    assert sum((asset.units or Decimal("0") for asset in active), Decimal("0")) == Decimal("12")
    assert len([asset for asset in active if asset.units]) == 1
    assert len(await _rows(v, AssetTransaction)) <= 1
    preview = await _evidence(v)
    assert any(record["match_status"] in {"candidate", "conflicting"} for record in preview["records"])


async def test_pending_to_completed_api_revision_retains_both_facts_and_applies_once(venue):
    v = venue
    v.accounts[0]["balance"]["amount"] = "0"
    v.history[WALLET][0]["status"] = "pending"
    await _sync(v)
    assert await _rows(v, AssetTransaction) == []
    v.accounts[0]["balance"]["amount"] = "12"
    v.history[WALLET][0]["status"] = "completed"
    await _sync(v)
    await _sync(v)
    observations = await _rows(v, InvestmentObservation)
    assert len(observations) == 2
    assert {o.payload["provider_status"] for o in observations} == {"pending", "completed"}
    assert {o.payload["source_local_id"] for o in observations} == {"api-trade-A"}
    assert len(await _rows(v, AssetTransaction)) == 1
    assert (await _rows(v, Asset))[0].units == Decimal("12")


async def test_viewer_can_preview_source_evidence_but_cannot_retain_or_apply_it(venue, viewer_auth_headers):
    v = SimpleNamespace(**{**vars(venue), "headers": viewer_auth_headers})
    preview = await _preview(v, [_csv_row()])
    response = await v.client.post("/api/assets/import", headers=v.headers, json={
        "mode": "evidence", "group_id": str(v.group.id), "connection_id": str(v.connection.id),
        "observations": preview["evidence"]["observations"], "expected_revision": preview["evidence"]["revision"],
    })
    assert response.status_code == 403, response.text
    response = await v.client.post("/api/assets/evidence/confirm", headers=v.headers, json={
        "group_id": str(v.group.id), "decisions": [], "expected_revision": preview["evidence"]["revision"],
    })
    assert response.status_code == 403, response.text
    assert await _rows(v, InvestmentObservation) == await _rows(v, AssetTransaction) == []


async def test_one_to_many_link_refuses_a_source_total_that_disagrees_with_allocated_fills(venue):
    v = venue
    v.history[WALLET] = [
        _api_row("api-fill-1", quantity="5", amount="35", advanced_trade_fill={"order_id": "order-A"}, kind="advanced_trade_fill"),
        _api_row("api-fill-2", quantity="7", amount="49", advanced_trade_fill={"order_id": "order-A"}, kind="advanced_trade_fill"),
    ]
    await _sync(v)
    stored = await _upload(v, [_csv_row(**{"Order ID": "order-A", "Price": "8", "Subtotal": "96", "Total": "96"})])
    record = _record(stored["evidence"], "csv-row-Q")
    allocations = [{"leg_id": c["leg_id"], "quantity": c["quantity"]} for c in record["candidate_legs"]]
    assert len(allocations) == 2
    await _confirm(v, stored["evidence"], [_link_decision(record, allocations)], status=422)
    assert len(await _rows(v, AssetTransaction)) == 2
    assert _record(await _evidence(v), "csv-row-Q")["match_status"] != "linked"


async def test_nonzero_fee_below_ledger_cent_precision_cannot_silently_become_zero(venue):
    v = venue
    stored = await _upload(v, [_csv_row(**{
        "Fee": "0.0001", "Fee Currency": "USD", "Subtotal": "84", "Total": "84.0001",
    })])
    record = _record(stored["evidence"], "csv-row-Q")
    response = await v.client.post("/api/assets/evidence/confirm", headers=v.headers, json={
        "group_id": str(v.group.id), "expected_revision": stored["evidence"]["revision"],
        "decisions": [_apply_decision(record)], "allow_unpriced": True,
    })
    # An exact equivalent ledger representation is fine; a visible refusal is
    # also fine. Rounding the source's nonzero consideration away is neither.
    if response.status_code == 422:
        assert await _rows(v, AssetTransaction) == []
    else:
        _json(response)
        ledger = await _rows(v, AssetTransaction)
        assert len(ledger) == 1
        assert ledger[0].quantity * ledger[0].price + ledger[0].fee == Decimal("84.0001")


async def test_a_real_api_batch_previews_history_once_per_wallet_and_preserves_every_execution(venue, monkeypatch):
    from app.services import investment_evidence_service

    v = venue
    v.history[WALLET] = [_api_row(f"batch-execution-{index}", quantity="1", amount="7") for index in range(24)]
    v.accounts[0]["balance"]["amount"] = "24"
    original = investment_evidence_service.preview_evidence
    calls = []

    async def counted(*args, **kwargs):
        calls.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(investment_evidence_service, "preview_evidence", counted)
    await _sync(v)
    assert len(calls) == 1
    assert len(await _rows(v, AssetTransaction)) == 24
    assert (await _rows(v, Asset))[0].units == Decimal("24")
    calls.clear()
    await _sync(v)
    assert len(calls) <= 1
    assert len(await _rows(v, AssetTransaction)) == 24
    assert len(await _rows(v, InvestmentObservation)) == 24


async def test_history_without_any_holding_or_wallet_is_retained_under_current_mapped_account(venue):
    v = venue
    await v.session.delete(v.group)
    account = Account(
        workspace_id=v.workspace.id, user_id=v.user.id, connection_id=v.connection.id,
        external_id=PORTFOLIO, name="Synthetic current mapped account", type="investment",
        currency="USD", balance=Decimal("0"),
    )
    v.session.add(account)
    await v.session.commit()
    v.accounts[0]["balance"]["amount"] = "0"
    v.history[WALLET].append(_api_row("historical-withdrawal", kind="send", quantity="-12", amount="-84"))
    # The history phase must stand on the current account mapping even when
    # holdings were unavailable: retaining history cannot invent a position.
    await _sync_trades(v.session, v.connection, v.credentials, {PORTFOLIO})
    await v.session.commit()
    observations = await _rows(v, InvestmentObservation)
    assert {o.payload["source_local_id"] for o in observations} == {"api-trade-A", "historical-withdrawal"}
    assert all(o.workspace_id == v.workspace.id and o.connection_id == v.connection.id for o in observations)
    assert {o.payload["account_external_id"] for o in observations} == {PORTFOLIO}
    assert await _rows(v, Asset) == await _rows(v, AssetTransaction) == []
    groups = await _rows(v, AssetGroup)
    assert len(groups) == 1
    assert groups[0].connection_id == v.connection.id and groups[0].workspace_id == v.workspace.id
    v.group = groups[0]
    preview = await _evidence(v)
    assert preview["target"]["workspace_id"] == str(v.workspace.id)
    assert preview["target"]["account_id"] == str(account.id)
