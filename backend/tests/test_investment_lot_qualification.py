"""Synthetic inventory qualification: reported observations never rewrite history."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event, select

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.bank_connection import BankConnection
from app.models.investment_evidence import (
    InvestmentEvent,
    InvestmentLeg,
    InvestmentObservation,
    InvestmentObservationLink,
)
from app.providers.base import HoldingData, PartialHoldings
from app.schemas.investment_evidence import EvidenceLegInput
from app.services.connection_service import _sync_holdings, _upsert_asset_from_holding
from app.services.investment_evidence_service import _holding_conflicts
from app.services.tax_lots import asset_tax_lots

IDENTITY = {"chain": "solana", "token_address": "native", "provider_asset_id": "synthetic-currency"}


async def _portfolio(session, user, workspace, quantity="0", ledger=True):
    now = datetime.now(timezone.utc).isoformat()
    connection = BankConnection(
        id=uuid.uuid4(),
        user_id=user.id,
        workspace_id=workspace.id,
        provider="coinbase",
        external_id="synthetic-connection",
        institution_name="Synthetic exchange",
        status="active",
        credentials={},
        settings={"holdings_quantity_attempt_at": now},
    )
    group = AssetGroup(
        id=uuid.uuid4(),
        user_id=user.id,
        workspace_id=workspace.id,
        name="Synthetic wallet",
        source="coinbase",
        tax_treatment="taxable",
        connection_id=connection.id,
    )
    asset = Asset(
        id=uuid.uuid4(),
        user_id=user.id,
        workspace_id=workspace.id,
        group_id=group.id,
        connection_id=connection.id,
        source="coinbase",
        external_id="synthetic-holding",
        account_external_id="synthetic-account",
        name="Synthetic coin",
        ticker="SYN",
        type="crypto",
        currency="USD",
        units=Decimal(quantity) if quantity is not None else None,
        is_archived=False,
        external_metadata={
            "evidence_asset_identity": IDENTITY.copy(),
            "obsolete": "old provider value",
            "provider_quantity_observation": {"quantity": quantity, "collected_at": now},
        },
    )
    session.add_all([connection, group, asset])
    if ledger:
        session.add(
            AssetTransaction(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                asset_id=asset.id,
                kind="buy",
                quantity=Decimal("10"),
                price=Decimal("7"),
                fee=Decimal(0),
                date=date(2020, 1, 1),
                source="import",
            )
        )
    await session.commit()
    return connection, group, asset


@pytest.mark.parametrize(
    "reported,status",
    [
        ("0", "mismatch"),
        (None, "not_comparable"),
        ("10", "exact_match"),
        ("10.0001", "within_tolerance"),
        ("11", "mismatch"),
    ],
)
async def test_quantity_comparison_preserves_historical_lots_and_does_not_write(
    session,
    test_user,
    test_workspace,
    reported,
    status,
):
    _, _, asset = await _portfolio(session, test_user, test_workspace, reported)
    before = {c.name: getattr(asset, c.name) for c in Asset.__table__.columns}
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement.strip().split()[0].upper())

    event.listen(session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        result = await asset_tax_lots(session, asset.id, test_workspace.id, as_of=date(2021, 6, 1))
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", capture)
    assert result is not None
    assert result["lots"][0]["quantity"] == 10
    assert result["lots"][0]["acquired"] == "2020-01-01"
    q = result["qualification"]
    assert q["comparison"] == status
    assert q["reported_quantity"] == reported
    assert Decimal(q["replayed_quantity"]) == 10
    assert q["quantity_supported"] == (status in {"exact_match", "within_tolerance"})
    assert "lifetime_history_unverified" in q["reason_codes"]
    assert q["scope"] == "latest_reported_vs_recorded"
    if reported is not None:
        assert Decimal(q["discrepancy"]) == Decimal("10") - Decimal(reported)
    assert before == {c.name: getattr(asset, c.name) for c in Asset.__table__.columns}
    assert not session.dirty and not session.new and not session.deleted
    assert not set(statements) & {"INSERT", "UPDATE", "DELETE"}


@pytest.mark.parametrize(
    "case,reason",
    [
        ("archived", "holding_archived"),
        ("closed", "holding_closed"),
        ("legacy", "quantity_observation_missing"),
        ("stale", "quantity_observation_stale"),
        ("error", "refresh_failed"),
        ("disconnected", "connection_unavailable"),
        ("source_only", "independent_quantity_unavailable"),
    ],
)
async def test_retained_or_unavailable_quantities_cannot_certify_inventory(
    session,
    test_user,
    test_workspace,
    case,
    reason,
):
    connection, _, asset = await _portfolio(session, test_user, test_workspace, "10")
    if case == "archived":
        asset.is_archived = True
    if case == "closed":
        asset.sell_date = date(2021, 1, 1)
    if case == "legacy":
        asset.external_metadata = {"evidence_asset_identity": IDENTITY}
    if case == "stale":
        connection.settings = {"holdings_quantity_attempt_at": "later-cycle"}
    if case == "error":
        connection.status = "sync_error"
    if case == "disconnected":
        asset.connection_id = None
    if case == "source_only":
        asset.source, asset.connection_id = "import", None
        asset.external_metadata = {"investment_evidence_created": True}
    await session.commit()
    result = await asset_tax_lots(session, asset.id, test_workspace.id)
    assert result is not None
    q = result["qualification"]
    assert q["comparison"] == "not_comparable"
    assert q["quantity_supported"] is False
    assert reason in q["reason_codes"]
    assert Decimal(q["stored_quantity"]) == 10
    assert result["lots"][0]["quantity"] == 10


async def test_snapshot_and_oversell_zero_never_certify_a_complete_history(
    session, test_user, test_workspace
):
    _, _, asset = await _portfolio(session, test_user, test_workspace, "10", ledger=False)
    result = await asset_tax_lots(session, asset.id, test_workspace.id)
    assert result is not None
    assert result["snapshot"] is True and result["lots"] == []
    assert "recorded_transactions_missing" in result["qualification"]["reason_codes"]
    asset.units = Decimal(0)
    asset.external_metadata = {
        **asset.external_metadata,
        "provider_quantity_observation": {
            **asset.external_metadata["provider_quantity_observation"],
            "quantity": "0",
        },
    }
    session.add(
        AssetTransaction(
            id=uuid.uuid4(),
            workspace_id=test_workspace.id,
            asset_id=asset.id,
            kind="sell",
            quantity=Decimal(10),
            price=Decimal(7),
            fee=Decimal(0),
            date=date(2020, 1, 1),
        )
    )
    await session.commit()
    result = await asset_tax_lots(session, asset.id, test_workspace.id)
    assert result is not None
    q = result["qualification"]
    assert q["comparison"] == "exact_match" and not q["quantity_supported"]
    assert "oversell" in q["reason_codes"]


@pytest.mark.parametrize("withdrawn", [False, True])
async def test_active_and_withdrawn_refresh_preserve_reviewed_identity_only(
    session,
    test_user,
    test_workspace,
    withdrawn,
):
    connection, _, asset = await _portfolio(session, test_user, test_workspace)
    holding = HoldingData(
        external_id=asset.external_id,
        account_external_id=asset.account_external_id,
        name="Synthetic refreshed coin",
        currency="USD",
        current_value=Decimal(0),
        quantity=Decimal(0),
        ticker="SYN",
        is_withdrawn=withdrawn,
        metadata={
            "provider_asset_id": "synthetic-currency",
            "status": "TOTAL_WITHDRAWAL" if withdrawn else "ACTIVE",
        },
    )
    provider = AsyncMock()
    provider.get_holdings.return_value = [holding]
    leg = EvidenceLegInput(
        key="principal",
        asset_symbol="SYN",
        direction="in",
        classification="transfer",
        quantity="10",
        chain=IDENTITY["chain"],
        token_address=IDENTITY["token_address"],
        provider_asset_id=IDENTITY["provider_asset_id"],
    )
    for _ in range(2):
        await _sync_holdings(session, test_user.id, connection, {}, provider=provider)
        await session.commit()
        assert asset.external_metadata["evidence_asset_identity"] == IDENTITY
        assert "obsolete" not in asset.external_metadata
        assert _holding_conflicts(leg, asset) == []
        assert asset.external_metadata["provider_quantity_observation"]["quantity"] == "0"
    assert (
        len(
            (
                await session.scalars(
                    select(AssetTransaction).where(AssetTransaction.asset_id == asset.id)
                )
            ).all()
        )
        == 1
    )


@pytest.mark.parametrize(
    "case", ["provider_id", "token", "ticker", "account", "workspace", "unproven_reconnect"]
)
async def test_refresh_does_not_carry_reviewed_identity_across_conflicts(
    session,
    test_user,
    test_workspace,
    case,
):
    connection, _, asset = await _portfolio(session, test_user, test_workspace)
    holding = HoldingData(
        external_id=asset.external_id,
        account_external_id=asset.account_external_id,
        name="Synthetic",
        currency="USD",
        current_value=Decimal(0),
        quantity=Decimal(0),
        ticker="SYN",
        metadata={"provider_asset_id": "synthetic-currency"},
    )
    wid, cid = test_workspace.id, connection.id
    assert holding.metadata is not None
    if case == "provider_id":
        holding.metadata["provider_asset_id"] = "changed-currency"
    if case == "token":
        holding.metadata["token_address"] = "changed-token"
    if case == "ticker":
        holding.ticker = "CHANGED"
    if case == "account":
        holding.account_external_id = "changed-account"
    if case == "workspace":
        wid = uuid.uuid4()
    if case == "unproven_reconnect":
        cid, holding.metadata = uuid.uuid4(), {}
    await _upsert_asset_from_holding(session, asset, holding, test_user.id, cid, "coinbase", wid)
    assert "evidence_asset_identity" not in asset.external_metadata


async def test_sparse_quantity_and_failed_refresh_preserve_position_but_invalidate_comparison(
    session,
    test_user,
    test_workspace,
):
    connection, _, asset = await _portfolio(session, test_user, test_workspace, "10")
    provider = AsyncMock()
    provider.get_holdings.return_value = [
        HoldingData(
            external_id=asset.external_id,
            account_external_id=asset.account_external_id,
            name="Synthetic",
            currency="USD",
            current_value=Decimal(0),
            quantity=None,
            ticker="SYN",
        )
    ]
    await _sync_holdings(session, test_user.id, connection, {}, provider=provider)
    await session.commit()
    assert asset.units == 10
    assert asset.external_metadata["provider_quantity_observation"]["quantity"] is None
    result = await asset_tax_lots(session, asset.id, test_workspace.id)
    assert result is not None
    q = result["qualification"]
    assert q["reported_quantity"] is None and q["comparison"] == "not_comparable"
    provider.get_holdings.side_effect = PartialHoldings([], [asset.external_id])
    await _sync_holdings(session, test_user.id, connection, {}, provider=provider)
    await session.commit()
    result = await asset_tax_lots(session, asset.id, test_workspace.id)
    assert result is not None
    q = result["qualification"]
    assert "quantity_observation_stale" in q["reason_codes"]
    assert asset.units == 10 and not asset.is_archived


@pytest.mark.parametrize("treatment", ["roth", "traditional", "hsa", "other", None])
async def test_reportability_and_workspace_boundaries(
    session, test_user, test_workspace, treatment
):
    connection, group, asset = await _portfolio(session, test_user, test_workspace, "10")
    if treatment is None:
        asset.group_id = None
    else:
        group.tax_treatment = treatment
    await session.commit()
    result = await asset_tax_lots(session, asset.id, test_workspace.id)
    assert result is not None
    assert result["lots"] == result["sales"] == [] and not result["tax_character"]
    assert "qualification" not in result
    assert await asset_tax_lots(session, asset.id, uuid.uuid4()) is None


@pytest.mark.parametrize(
    "asset_type,kind,quantity",
    [
        ("crypto", "buy", "12345678901234567890.123456789012345678"),
        ("option", "sell", "-2"),
    ],
)
async def test_exact_api_qualification_preserves_decimal_precision_and_written_sign(
    asset_type, kind, quantity
):
    wid, aid, cid, gid = [uuid.uuid4() for _ in range(4)]
    now = datetime.now(timezone.utc).isoformat()
    asset = Asset(
        id=aid,
        workspace_id=wid,
        connection_id=cid,
        group_id=gid,
        source="coinbase",
        type=asset_type,
        ticker="SYN",
        units=Decimal(quantity),
        external_metadata={
            "provider_quantity_observation": {"quantity": quantity, "collected_at": now}
        },
    )
    tx = AssetTransaction(
        id=uuid.uuid4(),
        workspace_id=wid,
        asset_id=aid,
        kind=kind,
        quantity=Decimal(quantity).copy_abs(),
        price=Decimal(1),
        fee=Decimal(0),
        date=date(2020, 1, 1),
    )
    conn = BankConnection(
        id=cid,
        workspace_id=wid,
        provider="coinbase",
        status="active",
        settings={"holdings_quantity_attempt_at": now},
    )
    session, asset_rows, tx_rows, source_rows = AsyncMock(), MagicMock(), MagicMock(), MagicMock()
    asset_rows.first.return_value = (asset, "taxable")
    tx_rows.scalars.return_value.all.return_value = [tx]
    source_rows.all.return_value = []
    session.execute.side_effect = [asset_rows, tx_rows, source_rows]
    session.scalar.return_value = conn
    with patch("app.services.owned_transfer_service.prepare_replay", new=AsyncMock()):
        result = await asset_tax_lots(session, aid, wid)
    assert result is not None
    q = result["qualification"]
    assert q["reported_quantity"] == q["replayed_quantity"] == quantity
    assert q["comparison"] == "exact_match" and q["quantity_supported"]
    with localcontext(prec=128):
        assert Decimal(q["tolerance"]) == abs(Decimal(quantity)) * Decimal("0.0001")


@pytest.mark.parametrize("settled", [True, False])
async def test_movement_read_keeps_missing_basis_and_settlement_qualification(
    session, test_user, test_workspace, settled
):
    _, _, asset = await _portfolio(session, test_user, test_workspace, "15")
    session.add(
        AssetTransaction(
            id=uuid.uuid4(),
            workspace_id=test_workspace.id,
            asset_id=asset.id,
            kind="move_in",
            quantity=Decimal(5),
            price=None,
            fee=Decimal(0),
            date=date(2021, 1, 1),
            movement={
                "settlement_complete": settled,
                "basis_complete": False,
                "missing_links": [] if settled else ["transfer_evidence_invalidated"],
            },
        )
    )
    await session.commit()
    result = await asset_tax_lots(session, asset.id, test_workspace.id)
    assert result is not None
    assert result["basis_complete"] is False
    q = result["qualification"]
    assert "acquisition_basis_incomplete" in q["reason_codes"]
    assert q["quantity_supported"] is settled
    if not settled:
        assert "movement_settlement_unqualified" in q["reason_codes"]
    assert not session.dirty


async def test_retained_sources_contribute_coverage_without_becoming_another_ledger(
    session, test_user, test_workspace
):
    _, group, asset = await _portfolio(session, test_user, test_workspace, "10")
    tx = await session.scalar(select(AssetTransaction).where(AssetTransaction.asset_id == asset.id))
    source = InvestmentObservation(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        group_id=group.id,
        identity_key="synthetic-identity",
        fingerprint="synthetic-fingerprint",
        is_current=False,
        payload={
            "settlement_status": "pending",
            "coverage": ["private-source-locator-must-not-leak"],
            "legs": [{"quantity": "999"}],
        },
    )
    execution = InvestmentEvent(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        group_id=group.id,
        event_key="synthetic-event",
    )
    session.add_all([source, execution])
    session.add(
        InvestmentLeg(
            id=uuid.uuid4(),
            workspace_id=test_workspace.id,
            event_id=execution.id,
            observation_id=source.id,
            asset_id=asset.id,
            asset_transaction_id=tx.id,
            source_leg_key="principal",
            payload={"quantity": "999"},
        )
    )
    await session.commit()
    result = await asset_tax_lots(session, asset.id, test_workspace.id)
    assert result is not None
    q = result["qualification"]
    assert q["comparison"] == "exact_match" and not q["quantity_supported"]
    assert {"source_activity_unqualified", "source_coverage_incomplete"} <= set(q["reason_codes"])
    assert Decimal(q["replayed_quantity"]) == 10
    assert "private-source-locator" not in str(result)


@pytest.mark.parametrize(
    "case",
    [
        "verified",
        "unrelated_source",
        "ticker_only",
        "conflicting_token",
        "cross_account",
        "foreign_source",
    ],
)
async def test_sync_adopts_reviewed_identity_only_with_real_scoped_proof(
    session, test_user, test_workspace, case
):
    connection, group, asset = await _portfolio(session, test_user, test_workspace, "10")
    external_id = asset.external_id
    group.external_id = f"{connection.external_id}::{asset.account_external_id}"
    asset.source, asset.external_id, asset.connection_id = "import", None, None
    identity = {"chain": "solana", "token_address": "native"}
    asset.external_metadata = {
        "investment_evidence_created": True,
        "evidence_asset_identity": identity,
    }
    source = InvestmentObservation(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        group_id=group.id,
        connection_id=connection.id,
        identity_key="adoption-source",
        fingerprint="adoption-source",
        payload={"source": "coinbase_api", "holding_external_id": external_id},
    )
    execution = InvestmentEvent(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        group_id=group.id,
        event_key="adoption-event",
    )
    leg = InvestmentLeg(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        event_id=execution.id,
        observation_id=source.id,
        asset_id=asset.id,
        source_leg_key="principal",
        payload={},
    )
    session.add_all([source, execution, leg])
    if case != "ticker_only":
        session.add(
            InvestmentObservationLink(
                id=uuid.uuid4(),
                workspace_id=test_workspace.id,
                observation_id=source.id,
                source_leg_key="principal",
                leg_id=leg.id,
                role="corroborates",
            )
        )
    if case == "unrelated_source":
        source.payload = {"source": "coinbase_api", "holding_external_id": "different-holding"}
    if case == "foreign_source":
        source.connection_id = None
    if case == "cross_account":
        asset.account_external_id = "different-account"
    metadata = {"provider_asset_id": "synthetic-currency"}
    if case == "conflicting_token":
        metadata["token_address"] = "different-token"
    await session.commit()
    provider = AsyncMock()
    provider.get_holdings.return_value = [
        HoldingData(
            external_id=external_id,
            account_external_id="synthetic-account",
            name="Synthetic",
            currency="USD",
            current_value=Decimal(70),
            quantity=Decimal(10),
            ticker="SYN",
            metadata=metadata,
        )
    ]
    await _sync_holdings(session, test_user.id, connection, {}, provider=provider)
    await session.commit()
    if case == "verified":
        assert asset.connection_id == connection.id and asset.external_id == external_id
        assert asset.external_metadata["evidence_asset_identity"] == identity
        assert "investment_evidence_created" not in asset.external_metadata
    else:
        assert asset.connection_id is None and asset.source == "import"
    assert (
        len(
            (
                await session.scalars(select(Asset).where(Asset.workspace_id == test_workspace.id))
            ).all()
        )
        == 1
    )
