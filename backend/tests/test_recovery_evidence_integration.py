"""Independent synthetic recovery acceptance through the real HTTP boundaries."""
import csv
import io
import json
import os
import sys
import uuid
from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import get_async_session
from app.core.auth import get_jwt_strategy
from app.main import app
from app.models.asset_transaction import AssetTransaction
from app.models.investment_evidence import InvestmentObservation
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from tests.test_owned_transfers_integration import (
    apply_movement, checked, confirm_transfer, effect, make_holding, pair,
    retain_movement, select_lots, transfers as transfers,
)
from tests.test_owned_transfers_postgres import transfer_pg_context as transfer_pg_context

pytestmark = pytest.mark.asyncio
RECOVERY = "/api/assets/recovery"


def entry(key, role="recovery_notice", *, quantity="4", symbol="SYN", round_key="round-1", **changes):
    kind = {"allowed_claim": "recovery_notice", "recovery_notice": "recovery_notice",
            "tax_workpaper": "tax_workpaper", "equity_statement": "balance_snapshot"}.get(role, "primary_activity")
    classification = {"receiving_receipt": "transfer", "disposition": "sell"}.get(role, "unknown")
    value = {
        "key": key, "leg_key": "amount", "case_key": "synthetic-recovery",
        "round_key": round_key, "round_asset_key": symbol if round_key else None, "role": role,
        "reported_state": "confirmed", "details": {}, "missing_evidence": [],
        "observation": {
            "reference": key, "source": "synthetic_statement", "provider": "synthetic-platform",
            "source_kind": kind, "source_account_id": "synthetic-account-A", "source_local_id": key,
            "source_locator": f"synthetic/{key}.csv", "event_date": "2025-02-03",
            "event_time_raw": "2025-02-03", "time_precision": "date",
            "settlement_status": "settled", "provider_status": "completed",
            "legs": [{"key": "amount", "asset_symbol": symbol, "quantity": quantity,
                      "direction": "out" if role == "disposition" else "in",
                      "classification": classification, "fee": "0"}],
        },
    }
    value.update(changes)
    return value


async def package(v, group=None, **filters):
    return checked(await v.client.get(RECOVERY, headers=v.headers,
        params={"group_id": str(group or v.b.group_id), **filters}))


async def retain(v, entries, group=None):
    data = {"group_id": str(group or v.b.group_id), "entries": entries}
    preview = checked(await v.client.post(f"{RECOVERY}/preview", headers=v.headers, json=data))
    return checked(await v.client.post(f"{RECOVERY}/retain", headers=v.headers,
        json={**data, "expected_revision": preview["revision"]}))


async def review(v, reviews, group=None):
    current = await package(v, group)
    return checked(await v.client.post(f"{RECOVERY}/reviews", headers=v.headers, json={
        "group_id": str(group or v.b.group_id), "reviews": reviews,
        "expected_revision": current["revision"],
    }))


def decision(key, source, *, kind="relation", **changes):
    return {"key": key, "kind": kind, "entry_id": source,
            "source_locator": f"synthetic/review-{key}", "reason": "Reviewed synthetic source evidence",
            **changes}


async def transaction_count(session):
    return await session.scalar(select(func.count()).select_from(AssetTransaction))


async def test_four_roles_apply_one_receipt_then_transfer_and_sale_preserve_unknowns(transfers):
    await receipt_journey(transfers)


async def receipt_journey(v):
    v.b.external_metadata = {"evidence_asset_identity": {"chain": "solana", "token_address": "native"}}
    await v.session.commit()
    baseline = await transaction_count(v.session)
    receipt = await retain_movement(v.session, v.b, direction="in", reference="synthetic-recovery-receipt",
        source="D" * 44, destination=v.addresses[v.b.id], quantity="4", valuation_amount="999",
        valuation_currency="USD")
    originals = [entry("claim", "allowed_claim"), entry("notice"), entry("workpaper", "tax_workpaper")]
    received = entry("receipt", "receiving_receipt", observation_id=str(receipt.observation_id),
                     observation=None, leg_key=receipt.source_leg_key)
    originals.append(received)
    saved = await retain(v, originals)
    assert len(saved["entries"]) == 4
    assert await transaction_count(v.session) == baseline
    ids = {item["key"]: item["id"] for item in saved["entries"]}
    await review(v, [decision("claim-notice", ids["claim"], target_entry_id=ids["notice"],
                             relation_kind="claim_notice", relation_state="confirmed",
                             supporting_observation_ids=[str(receipt.observation_id)])])
    await review(v, [decision("notice-receipt", ids["notice"], target_entry_id=ids["receipt"],
        relation_kind="notice_receipt", relation_state="confirmed",
        supporting_observation_ids=[str(receipt.observation_id)],
        account_mapping_evidence="Synthetic notice names this receiving account",
        timing_evidence="Synthetic receipt occurred on the notice date")])
    await review(v, [decision("workpaper-receipt", ids["workpaper"], target_entry_id=ids["receipt"],
        relation_kind="candidate_acquisition", relation_state="candidate",
        missing_evidence=["original_acquisition_provenance"])])
    applied = await apply_movement(v, v.b, receipt)
    assert Decimal(effect(applied, v.b)["quantity"]) == 4
    assert effect(applied, v.b)["performance_basis"] is None
    lot = effect(applied, v.b)["lots"][0]
    assert lot["acquired"] is None and lot["acquisition_cost"] is None
    assert await transaction_count(v.session) == baseline + 1
    repeated = checked(await v.client.post("/api/assets/evidence/movements", headers=v.headers,
        json={**applied["request"], "expected_revision": applied["revision"]}))
    assert repeated["id"] == applied["id"]
    replay = await retain(v, list(reversed(originals)))
    assert len(replay["entries"]) == 4
    receipt_read = next(row for row in replay["entries"] if row["key"] == "receipt")
    assert receipt_read["application"]["status"] == "applied"
    assert receipt_read["application"]["application_id"] == applied["id"]
    assert await transaction_count(v.session) == baseline + 1
    moved = await confirm_transfer(v, await select_lots(v, await pair(v, v.b, v.c,
        quantity="4", reference="synthetic-recovery-onward", when="2025-02-05T12:00:00+00:00")))
    assert Decimal(effect(moved, v.c)["quantity"]) == 4
    assert Decimal(effect(moved, v.c)["unknown_basis_quantity"]) == 4
    assert effect(moved, v.c)["performance_basis"] is None
    sale = checked(await v.client.post(f"/api/assets/{v.c.id}/transactions", headers=v.headers,
        json={"kind": "sell", "quantity": "4", "price": "30", "date": "2025-03-01"}), 201)
    assert sale["realized_gain"] is None
    detail = checked(await v.client.get(f"/api/assets/evidence/transfers/{moved['id']}", headers=v.headers))
    assert effect(detail, v.c)["realized_gain"] is None
    assert Decimal(effect(detail, v.c)["unknown_disposition_quantity"]) == 4
    assert len((await package(v))["entries"]) == 4


async def test_postgres_full_receipt_transfer_sale_journey(transfer_pg_context, client):
    v = transfer_pg_context
    async with v.sessions() as session:
        session.add(WorkspaceMember(workspace_id=v.workspace.id, user_id=v.user.id, role="owner"))
        await session.commit()
    token = await get_jwt_strategy().write_token(v.user)
    v.client, v.headers = client, {"Authorization": f"Bearer {token}", "X-Workspace-Id": str(v.workspace.id)}

    async def isolated_session():
        async with v.sessions() as session:
            yield session

    previous = app.dependency_overrides[get_async_session]
    app.dependency_overrides[get_async_session] = isolated_session
    try:
        async with v.sessions() as session:
            v.session = session
            for name in ("a", "b", "c"):
                setattr(v, name, await session.merge(getattr(v, name)))
            await receipt_journey(v)
    finally:
        app.dependency_overrides[get_async_session] = previous


async def test_rounds_missing_receipt_historical_boundaries_and_offchain_are_nonadditive(transfers):
    v = transfers
    baseline = await transaction_count(v.session)
    rows = [entry("round1-a"), entry("round1-b", symbol="SYN-B"), entry("round2-a", round_key="round-2"),
        entry("ledger900", "platform_ledger", quantity="900", round_key=None,
              details={"boundary_kind": "historical_before_withdrawal", "account_bucket": "Earn"}),
        entry("ledger850", "platform_ledger", quantity="850", round_key=None,
              details={"boundary_kind": "later_after_withdrawal", "account_bucket": "Earn"}),
        entry("claim-currency", "allowed_claim", quantity=None, round_key=None,
              details={"claim_amount": "900", "claim_currency": "USD", "account_bucket": "unknown"}),
        entry("offchain", "receiving_receipt")]
    rows[4]["observation"]["event_date"] = "2025-02-04"
    saved = await retain(v, rows)
    assert saved["round_count"] == 2 and saved["asset_record_count"] == 3
    missing = next(row for row in saved["entries"] if row["key"] == "round2-a")
    saved = await review(v, [decision("missing-round2-receipt", missing["id"],
        relation_kind="notice_receipt", relation_state="missing", target_entry_id=None,
        missing_evidence=["receiving_account_activity"])])
    assert "receiving_account_activity" in saved["missing_evidence"]
    offchain = next(row for row in saved["entries"] if row["key"] == "offchain")
    assert offchain["application"]["status"] == "unsupported"
    assert offchain["application"]["reason_codes"]
    assert await transaction_count(v.session) == baseline
    assert len((await retain(v, rows))["entries"]) == len(rows)
    claim = next(row for row in saved["entries"] if row["key"] == "claim-currency")
    assert claim["details"]["claim_amount"] == "900"
    assert claim["observation"]["legs"][0]["quantity"] is None
    assert claim["observation"]["legs"][0]["acquisition_basis"] is None


@pytest.mark.parametrize("label", ["claim", "distribution", "insolvency distribution"])
async def test_valued_recovery_labels_cannot_enter_generic_orders(transfers, label):
    v = transfers
    baseline = await transaction_count(v.session)
    content = f"ticker,date,quantity,price,kind,currency,fee\nSYN,2025-02-03,4,7,{label},USD,0\n".encode()
    result = checked(await v.client.post("/api/assets/import/preview", headers=v.headers,
        data={"group_id": str(v.b.group_id), "allow_unpriced": "true"},
        files={"file": ("synthetic-recovery.csv", content, "text/csv")}))
    assert result["orders"] == []
    assert "recovery_evidence_required" in json.dumps(result)
    checked(await v.client.post("/api/assets/import", headers=v.headers,
        json={"group_id": str(v.b.group_id), "orders": result["orders"], "allow_unpriced": True}))
    assert await transaction_count(v.session) == baseline


async def test_cross_account_sale_and_tax_mapping_remain_candidates_without_transfer(transfers):
    v = transfers
    source = await retain(v, [entry("recovery-receipt", "receiving_receipt")])
    sale = entry("sale-B", "disposition", details={"proceeds": "120", "proceeds_currency": "USD"})
    sale["observation"]["source_account_id"] = "synthetic-account-B"
    tax = entry("tax-lot-B", "tax_workpaper")
    tax["observation"]["source_account_id"] = "synthetic-account-B"
    tax["observation"].update(event_at="2025-02-03T07:00:00-05:00", time_precision="second",
                               event_time_raw="2025-02-03T07:00:00-05:00", timezone="America/New_York")
    other = await retain(v, [sale, tax], v.c.group_id)
    acquisition = entry("acquisition-A", "platform_ledger")
    acquisition["observation"].update(event_at="2025-02-03T12:00:00+00:00", time_precision="second",
        event_time_raw="2025-02-03T12:00:00+00:00", timezone="UTC")
    acquisition["observation"]["legs"][0].update(classification="buy", unit_price="20", execution_currency="USD")
    primary = await retain(v, [acquisition], v.a.group_id)
    receipt_id = source["entries"][0]["id"]
    ids = {row["key"]: row["id"] for row in other["entries"]}
    linked_tax = await review(v, [decision("candidate-acquisition-A", ids["tax-lot-B"],
        target_entry_id=primary["entries"][0]["id"], relation_kind="candidate_acquisition", relation_state="candidate",
        conflicting_fields=["source_account_id"], reason="Exact quantities and timezone-adjusted instants match; account provenance is disputed")], v.c.group_id)
    assert linked_tax["reviews"][0]["relation_state"] == "candidate"
    assert "related_context" not in linked_tax["missing_evidence"]
    candidate = decision("possible-receipt-sale", receipt_id, target_entry_id=ids["sale-B"],
        relation_kind="receipt_disposition", relation_state="candidate",
        missing_evidence=["intervening_owned_transfer"])
    linked = await review(v, [candidate])
    assert linked["reviews"][-1]["relation_state"] == "candidate"
    bad = {**candidate, "key": "unsupported-confirmed", "relation_state": "confirmed"}
    response = await v.client.post(f"{RECOVERY}/reviews", headers=v.headers, json={
        "group_id": str(v.b.group_id), "reviews": [bad], "expected_revision": linked["revision"]})
    assert response.status_code == 422, response.text
    tax_review = await review(v, [decision("tax-account-conflict", ids["tax-lot-B"], kind="assertion",
        assertion_kind="account_mapping", assertion_status="conflict", proposed_value="synthetic-account-A",
        conflicting_fields=["source_account_id"], missing_evidence=["original_acquisition_provenance"])], v.c.group_id)
    persisted_sale = next(row for row in tax_review["entries"] if row["key"] == "sale-B")
    assert persisted_sale["details"]["proceeds"] == "120"
    assert persisted_sale["details"]["acquisition_date"] is None
    assert persisted_sale["details"]["cash_credited"] is None
    assert persisted_sale["observation"]["legs"][0]["acquisition_basis"] is None
    assert next(row for row in tax_review["entries"] if row["key"] == "tax-lot-B")["observation"]["source_account_id"] == "synthetic-account-B"
    assert await transaction_count(v.session) == 1


async def test_equity_assertions_and_model_correction_do_not_erase_originals_or_blockers(transfers):
    v = transfers
    equity = entry("equity", "equity_statement", quantity="8", symbol="SYN-EQUITY", round_key=None,
        details={"reported_cost": "80", "reported_cost_currency": "USD"})
    equity["observation"].update(event_date=None, event_time_raw=None, time_precision="unknown")
    workpaper = entry("allocation-model", "tax_workpaper", quantity=None, round_key=None,
        details={"provisional_allocation": "110", "allocation_currency": "USD"},
        missing_evidence=["shifted_recovery_reference", "later_round_valuation"])
    saved = await retain(v, [equity, workpaper])
    ids = {row["key"]: row["id"] for row in saved["entries"]}
    source_before = deepcopy(next(row for row in saved["entries"] if row["key"] == "equity")["observation"])
    values = [decision("displayed-cost", ids["equity"], kind="assertion", assertion_kind="reported_cost",
                       assertion_status="reported", value="80", currency="USD"),
        decision("model-cost", ids["equity"], kind="assertion", assertion_kind="provisional_allocation",
                 assertion_status="modeled", value="110", currency="USD"),
        decision("allocation", ids["allocation-model"], kind="allocation", assertion_status="modeled",
                 required_entry_ids=[ids["equity"], ids["allocation-model"]],
                 missing_evidence=["shifted_recovery_reference", "later_round_valuation"]),
        decision("filing", ids["allocation-model"], kind="assertion", assertion_kind="filing_assertion",
                 assertion_status="unverified", proposed_value="Synthetic unverified filing assumption")]
    saved = await review(v, values)
    allocation = next(row for row in saved["reviews"] if row["key"] == "allocation")
    assert not allocation["ready_for_review"] and allocation["blockers"]
    assert "shifted_recovery_reference" in json.dumps(allocation)
    assert "later_round_valuation" in json.dumps(allocation)
    saved = await review(v, [decision("correct-reference", ids["allocation-model"], kind="correction",
        field="recovery_reference", proposed_value="synthetic-correct-round", assertion_status="supported",
        supporting_observation_ids=[next(row for row in saved["entries"] if row["key"] == "allocation-model")["observation_id"]])])
    allocation = next(row for row in saved["reviews"] if row["key"] == "allocation")
    assert not allocation["ready_for_review"] and allocation["blockers"]
    assert next(row for row in saved["reviews"] if row["key"] == "filing")["assertion_status"] == "unverified"
    current = next(row for row in saved["entries"] if row["key"] == "equity")
    assert current["observation"] == source_before
    assert current["details"]["statement_date"] is None
    assert current["observation"]["legs"][0]["acquisition_basis"] is None
    assert {row["value"] for row in saved["reviews"] if row["key"] in {"displayed-cost", "model-cost"}} == {"80", "110"}


async def test_exact_filtered_json_csv_exports_replay_and_append_only_review(transfers):
    v = transfers
    exact = "0.123456789012345678901234567890123456789"
    source = entry("precise", quantity=exact, details={"reported_cost": "0", "reported_cost_currency": "USD"})
    source["observation"].update(event_date=None, event_time_raw="source date unavailable", time_precision="unknown")
    saved = await retain(v, [source, entry("unrelated", case_key="different-case")])
    row = next(item for item in saved["entries"] if item["key"] == "precise")
    first = decision("mapping-original", row["id"], kind="assertion", assertion_kind="account_mapping",
                     assertion_status="conflict", conflicting_fields=["account_mapping"])
    saved = await review(v, [first])
    original = next(item for item in saved["reviews"] if item["key"] == first["key"])
    correction = decision("mapping-correction", row["id"], kind="assertion", supersedes_id=original["id"],
        assertion_kind="account_mapping", proposed_value="reviewed synthetic account", assertion_status="supported",
        supporting_observation_ids=[row["observation_id"]])
    saved = await review(v, [correction])
    filtered = await package(v, case_key="synthetic-recovery")
    assert len(filtered["entries"]) == 1 and len(filtered["reviews"]) == 2
    exported = {}
    for format in ("json", "csv"):
        response = await v.client.get(f"{RECOVERY}/export", headers=v.headers, params={
            "group_id": str(v.b.group_id), "case_key": "synthetic-recovery",
            "expected_revision": filtered["revision"], "format": format})
        assert response.status_code == 200, response.text
        assert "attachment" in response.headers["content-disposition"]
        exported[format] = response
        assert exact in response.text and "unrelated" not in response.text
    decoded = exported["json"].json()
    assert exact in json.dumps(decoded)
    csv_rows = list(csv.DictReader(io.StringIO(exported["csv"].text)))
    payloads = [json.loads(item["payload_json"]) for item in csv_rows]
    assert exact in json.dumps(payloads)
    assert "source date unavailable" in json.dumps(payloads)
    assert any(item.get("id") == original["id"] and item.get("is_current") is False for item in payloads)
    assert row["details"]["reported_cost"] == "0" and row["details"]["acquisition_date"] is None
    before = await transaction_count(v.session)
    assert len((await retain(v, [source]))["entries"]) == 2
    assert await transaction_count(v.session) == before


async def test_source_attachment_cross_group_allowed_cross_workspace_and_viewer_denied(transfers):
    v = transfers
    v.c.external_metadata = {"evidence_asset_identity": {"chain": "solana", "token_address": "native"}}
    await v.session.commit()
    source = await retain_movement(v.session, v.c, direction="in", reference="synthetic-other-group",
        source="D" * 44, destination=v.addresses[v.c.id], quantity="4")
    attached = entry("attached", "receiving_receipt", observation=None,
        observation_id=str(source.observation_id), leg_key=source.source_leg_key)
    saved = await retain(v, [attached])
    assert saved["entries"][0]["observation_id"] == str(source.observation_id)
    original = await v.session.get(InvestmentObservation, source.observation_id)
    assert original.group_id == v.c.group_id
    assert saved["entries"][0]["source_group_id"] == str(v.c.group_id)
    foreign = Workspace(name="Synthetic foreign", created_by_user_id=v.user.id)
    v.session.add(foreign)
    await v.session.flush()
    other_asset = await make_holding(v.session, foreign.id, v.user.id, "Foreign")
    foreign_source = await retain_movement(v.session, other_asset, direction="in",
        reference="synthetic-foreign-receipt", source="D" * 44, destination="E" * 44, quantity="4")
    invalid = {**attached, "key": "foreign", "observation_id": str(foreign_source.observation_id)}
    for route in ("preview", "retain"):
        body = {"group_id": str(v.b.group_id), "entries": [invalid]}
        if route == "retain":
            body["expected_revision"] = saved["revision"]
        result = await v.client.post(f"{RECOVERY}/{route}", headers=v.headers, json=body)
        assert result.status_code in (403, 404, 422), result.text
    for route in (RECOVERY, f"{RECOVERY}/export"):
        result = await v.client.get(route, headers=v.headers,
            params={"group_id": str(other_asset.group_id), "format": "json"})
        assert result.status_code in (403, 404, 422), result.text
    membership = await v.session.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == v.workspace.id, WorkspaceMember.user_id == v.user.id))
    membership.role = "viewer"
    await v.session.commit()
    current = await package(v)
    assert current["entries"]
    downloaded = await v.client.get(f"{RECOVERY}/export", headers=v.headers,
        params={"group_id": str(v.b.group_id), "format": "json", "expected_revision": current["revision"]})
    assert downloaded.status_code == 200
    for route, body in (("retain", {"entries": [entry("viewer-write")]}),
                        ("reviews", {"reviews": [decision("viewer-review", saved["entries"][0]["id"],
                            relation_kind="notice_receipt", relation_state="missing")]})):
        result = await v.client.post(f"{RECOVERY}/{route}", headers=v.headers, json={
            "group_id": str(v.b.group_id), "expected_revision": current["revision"], **body})
        assert result.status_code == 403, result.text


async def test_preview_is_readonly_stale_review_rejects_and_source_versions_are_immutable(transfers):
    v = transfers
    first = entry("immutable-source", "equity_statement", quantity="8", round_key=None,
                  details={"reported_cost": "80", "reported_cost_currency": "USD"})
    before = await v.session.scalar(select(func.count()).select_from(InvestmentObservation))
    preview = checked(await v.client.post(f"{RECOVERY}/preview", headers=v.headers,
        json={"group_id": str(v.b.group_id), "entries": [first]}))
    assert await v.session.scalar(select(func.count()).select_from(InvestmentObservation)) == before
    assert await transaction_count(v.session) == 1
    saved = await retain(v, [first])
    row = saved["entries"][0]
    stale = await v.client.post(f"{RECOVERY}/retain", headers=v.headers, json={
        "group_id": str(v.b.group_id), "entries": [entry("different-source")],
        "expected_revision": preview["revision"]})
    assert stale.status_code == 409
    stale_export = await v.client.get(f"{RECOVERY}/export", headers=v.headers,
        params={"group_id": str(v.b.group_id), "format": "json", "expected_revision": preview["revision"]})
    assert stale_export.status_code == 409
    changed = deepcopy(first)
    changed["details"]["reported_cost"] = "110"
    rejected = await v.client.post(f"{RECOVERY}/preview", headers=v.headers,
        json={"group_id": str(v.b.group_id), "entries": [changed]})
    assert rejected.status_code == 409
    saved = await review(v, [decision("reviewed-cost-correction", row["id"], kind="correction",
        field="reported_cost", value="110", currency="USD", assertion_status="supported",
        supporting_observation_ids=[row["observation_id"]])])
    assert len(saved["entries"]) == 1 and saved["entries"][0]["details"]["reported_cost"] == "80"
    assert saved["reviews"][0]["value"] == "110"
    assert next(item for item in saved["entries"] if item["id"] == row["id"])["observation"] == row["observation"]
    changed["key"] = "immutable-source-version2"
    saved = await retain(v, [changed])
    assert {item["details"]["reported_cost"] for item in saved["entries"]} == {"80", "110"}
    assert all("recovery_annotation_conflict" in item["reason_codes"] for item in saved["entries"])
    same_facts = {**first, "key": "same-source-facts-another-import"}
    assert len((await retain(v, [same_facts]))["entries"]) == 2
    assert await transaction_count(v.session) == 1


async def test_postgres_http_retention_preserves_long_decimals_and_nulls(
    postgres_sessions, client, test_user, test_workspace, auth_headers,
):
    from types import SimpleNamespace

    async with postgres_sessions() as session:
        session.add(User(id=test_user.id, email=test_user.email, hashed_password=test_user.hashed_password,
                         is_active=True, is_verified=True))
        await session.flush()
        session.add(Workspace(id=test_workspace.id, name="Synthetic PG", created_by_user_id=test_user.id))
        await session.flush()
        session.add(WorkspaceMember(workspace_id=test_workspace.id, user_id=test_user.id, role="owner"))
        asset = await make_holding(session, test_workspace.id, test_user.id, "PG recovery")
        context = SimpleNamespace(client=client, b=asset, headers={**auth_headers, "X-Workspace-Id": str(test_workspace.id)})

    async def isolated_session():
        async with postgres_sessions() as session:
            yield session

    previous = app.dependency_overrides[get_async_session]
    app.dependency_overrides[get_async_session] = isolated_session
    try:
        exact = "9007199254740993.12345678901234567890123456789"
        saved = await retain(context, [entry("pg-exact", quantity=exact,
            details={"claim_amount": "0", "claim_currency": "USD"})])
        assert saved["entries"][0]["observation"]["legs"][0]["quantity"] == exact
        assert saved["entries"][0]["details"]["claim_amount"] == "0"
        assert saved["entries"][0]["details"]["acquisition_date"] is None
        assert len((await retain(context, [entry("pg-exact", quantity=exact,
            details={"claim_amount": "0", "claim_currency": "USD"})]))["entries"]) == 1
        async with postgres_sessions() as session:
            assert await transaction_count(session) == 0
            assert await session.scalar(select(func.count()).select_from(InvestmentObservation)) == 1
    finally:
        app.dependency_overrides[get_async_session] = previous


async def test_postgres_recovery_migration_roundtrip_preserves_existing_user():
    """Use an owned temporary database: metadata.create_all cannot validate Alembic."""
    import asyncio
    from pathlib import Path

    configured = os.environ.get("EVIDENCE_TEST_DATABASE_URL")
    if not configured:
        if os.environ.get("CI"):
            pytest.fail("CI must supply EVIDENCE_TEST_DATABASE_URL for migration integration")
        pytest.skip("isolated PostgreSQL not configured")
    database = f"recovery_migration_{uuid.uuid4().hex}"
    url = make_url(configured).set(database=database)
    admin = create_async_engine(configured, isolation_level="AUTOCOMMIT")
    test_engine = create_async_engine(url)
    backend = Path(__file__).resolve().parents[1]
    script = (
        "import os; from app.core.config import get_settings; "
        "get_settings().database_url=os.environ['RECOVERY_MIGRATION_DATABASE_URL']; "
        "from alembic import command; from alembic.config import Config; "
        "import sys; getattr(command, sys.argv[1])(Config('alembic.ini'), sys.argv[2])"
    )

    async def migrate(action, revision):
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, action, revision,
            cwd=backend, env={**os.environ, "RECOVERY_MIGRATION_DATABASE_URL": url.render_as_string(hide_password=False)},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), timeout=120)
        assert process.returncode == 0, output.decode()

    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
        await migrate("upgrade", "102")
        async with test_engine.begin() as connection:
            await connection.execute(text("INSERT INTO users (id, email, hashed_password, is_active, is_superuser, is_verified) VALUES (:id, :email, 'synthetic-unused', true, false, true)"),
                {"id": uuid.uuid4(), "email": "synthetic-migration@example.com"})
        await migrate("upgrade", "103")
        async with test_engine.connect() as connection:
            for table in ("investment_recovery_entries", "investment_recovery_reviews"):
                assert await connection.scalar(text("SELECT to_regclass(:name)"), {"name": table}) == table
        await migrate("downgrade", "102")
        async with test_engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('investment_recovery_entries')")) is None
            assert await connection.scalar(text("SELECT count(*) FROM users WHERE email = 'synthetic-migration@example.com'")) == 1
        await migrate("upgrade", "103")
        async with test_engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "103"
            assert await connection.scalar(text("SELECT count(*) FROM users WHERE email = 'synthetic-migration@example.com'")) == 1
    finally:
        await test_engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        await admin.dispose()


async def test_unrelated_owned_transfer_cannot_confirm_recovery_receipt_to_sale(transfers):
    v = transfers
    unrelated = await confirm_transfer(v, await select_lots(v, await pair(v, v.a, v.c, quantity="4")))
    receipt = entry("unrelated-receipt", "receiving_receipt")
    receipt["observation"]["legs"][0]["asset_id"] = str(v.a.id)
    left = await retain(v, [receipt], v.a.group_id)
    sale = entry("unrelated-sale", "disposition", details={"proceeds": "120", "proceeds_currency": "USD"})
    sale["observation"]["legs"][0]["asset_id"] = str(v.c.id)
    right = await retain(v, [sale], v.c.group_id)
    request = decision("unrelated-proof", left["entries"][0]["id"],
        target_entry_id=right["entries"][0]["id"], relation_kind="receipt_disposition",
        relation_state="confirmed", owned_transfer_id=unrelated["id"],
        supporting_observation_ids=[left["entries"][0]["observation_id"], right["entries"][0]["observation_id"]],
        account_mapping_evidence="The account names match", timing_evidence="The displayed dates match")
    current = await package(v, v.a.group_id)
    result = await v.client.post(f"{RECOVERY}/reviews", headers=v.headers, json={
        "group_id": str(v.a.group_id), "reviews": [request], "expected_revision": current["revision"]})
    assert result.status_code == 422, result.text


async def test_model_missing_valuation_is_blocked_despite_supported_assumption(transfers):
    v = transfers
    saved = await retain(v, [entry("unvalued-equity", "equity_statement", quantity="8")])
    source = saved["entries"][0]
    saved = await review(v, [decision("model-assumption", source["id"], kind="assertion",
        assertion_kind="accounting_assumption", assertion_status="supported",
        field="allocation_scope", proposed_value="Requires a valuation for this equity input",
        supporting_observation_ids=[source["observation_id"]])])
    assumption = saved["reviews"][0]
    saved = await review(v, [decision("needs-equity-value", source["id"], kind="allocation",
        assertion_status="modeled", required_entry_ids=[source["id"]], required_review_ids=[assumption["id"]])])
    model = next(row for row in saved["reviews"] if row["key"] == "needs-equity-value")
    assert not model["ready_for_review"], model
    assert any("valuation" in reason for reason in model["blockers"]), model


async def test_missing_notice_and_conflicting_assertions_are_filterable(transfers):
    v = transfers
    saved = await retain(v, [entry("notice-without-receipt"), entry("disputed-equity", "equity_statement", quantity="8")])
    notice = next(row for row in saved["entries"] if row["key"] == "notice-without-receipt")
    assert any("receipt" in reason and "missing" in reason for reason in notice["reason_codes"]), notice
    assert "notice-without-receipt" in {row["key"] for row in (await package(v, state="missing"))["entries"]}
    equity = next(row for row in saved["entries"] if row["key"] == "disputed-equity")
    await review(v, [decision("disputed-cost", equity["id"], kind="assertion",
        assertion_kind="reported_cost", assertion_status="conflict", value="80", currency="USD")])
    assert "disputed-equity" in {row["key"] for row in (await package(v, state="conflict"))["entries"]}


async def test_instant_only_source_clock_supports_notice_receipt_review(transfers):
    v = transfers
    rows = [entry("instant-notice"), entry("instant-receipt", "receiving_receipt")]
    for row in rows:
        row["observation"].update(event_date=None, event_at="2025-02-03T12:00:00+00:00",
            event_time_raw="2025-02-03T12:00:00+00:00", time_precision="second", timezone="UTC")
    saved = await retain(v, rows)
    ids = {row["key"]: row for row in saved["entries"]}
    prior = await review(v, [decision("instant-missing", ids["instant-notice"]["id"],
        relation_kind="notice_receipt", relation_state="missing", missing_evidence=["receiving_account_activity"])])
    old = prior["reviews"][0]
    saved = await review(v, [decision("instant-relation", ids["instant-notice"]["id"],
        target_entry_id=ids["instant-receipt"]["id"], relation_kind="notice_receipt", relation_state="confirmed",
        supersedes_id=old["id"],
        supporting_observation_ids=[ids["instant-receipt"]["observation_id"]],
        account_mapping_evidence="Synthetic receiving account is named by the notice",
        timing_evidence="Both sources report this exact UTC instant")])
    current = next(row for row in saved["reviews"] if row["key"] == "instant-relation")
    assert current["relation_state"] == "confirmed" and current["is_current"]
    assert current["blockers"] == []
    assert not next(row for row in saved["reviews"] if row["id"] == old["id"])["is_current"]
    assert "receiving_account_activity" not in saved["missing_evidence"]
    assert all(row["observation"]["event_date"] is None for row in saved["entries"])


async def test_recovery_receipt_reversal_keeps_tombstone_and_source_replay_nonfinancial(transfers):
    v = transfers
    v.b.external_metadata = {"evidence_asset_identity": {"chain": "solana", "token_address": "native"}}
    await v.session.commit()
    receipt = await retain_movement(v.session, v.b, direction="in", reference="synthetic-reversed-recovery",
        source="D" * 44, destination=v.addresses[v.b.id], quantity="4")
    source = entry("reversible-receipt", "receiving_receipt", observation_id=str(receipt.observation_id),
        observation=None, leg_key=receipt.source_leg_key)
    await retain(v, [source])
    applied = await apply_movement(v, v.b, receipt)
    reverse = checked(await v.client.delete(f"/api/assets/evidence/movements/{applied['id']}",
        headers=v.headers, params={"expected_revision": applied["revision"]}))
    assert reverse["status"] == "reversed"
    retained = await retain(v, [source])
    assert retained["entries"][0]["application"]["status"] == "reversed"
    assert retained["entries"][0]["application"]["application_id"] == applied["id"]
    before = await transaction_count(v.session)
    repeated = await v.client.post("/api/assets/evidence/movements", headers=v.headers,
        json={**applied["request"], "expected_revision": reverse["revision"]})
    assert repeated.status_code == 409
    assert await transaction_count(v.session) == before
    await v.session.refresh(v.b)
    assert v.b.units == 0


async def test_workpaper_cannot_verify_filing_or_unblock_dependent_allocation(transfers):
    v = transfers
    saved = await retain(v, [entry("filing-workpaper", "tax_workpaper", quantity="8",
        details={"provisional_allocation": "110", "allocation_currency": "USD"})])
    source = saved["entries"][0]
    filing = decision("filing-assertion", source["id"], kind="assertion", assertion_kind="filing_assertion",
        assertion_status="supported", supporting_observation_ids=[source["observation_id"]],
        proposed_value="Workpaper claims a prior filing used this model")
    result = await v.client.post(f"{RECOVERY}/reviews", headers=v.headers, json={
        "group_id": str(v.b.group_id), "reviews": [filing], "expected_revision": saved["revision"]})
    assert result.status_code == 422, result.text
    filing["assertion_status"] = "unverified"
    saved = await review(v, [filing])
    review_row = next(row for row in saved["reviews"] if row["key"] == "filing-assertion")
    assert "filed_record_unverified" in review_row["blockers"]
    assert not review_row["ready_for_review"]
    saved = await review(v, [decision("filing-dependent-allocation", source["id"], kind="allocation",
        assertion_status="modeled", required_entry_ids=[source["id"]], required_review_ids=[review_row["id"]])])
    model = next(row for row in saved["reviews"] if row["key"] == "filing-dependent-allocation")
    assert "filed_record_unverified" in model["blockers"] and not model["ready_for_review"]
