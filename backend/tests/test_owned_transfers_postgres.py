"""Owned-transfer races use independent transactions in disposable PostgreSQL schemas."""
import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.owned_transfer import InvestmentMovementApplication, InvestmentOwnedTransfer
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.owned_transfer import (
    MovementConfirmRequest, MovementPreviewRequest, OwnershipCreate, TransferConfirmRequest, TransferPreviewRequest,
)
from tests.test_owned_transfers_integration import add_acquisition, make_holding, pair, retain_movement


@pytest_asyncio.fixture
async def transfer_pg_context(postgres_sessions):
    from app.services import owned_transfer_service as service

    async with postgres_sessions() as session:
        user = User(email="synthetic-transfers@example.invalid", hashed_password="synthetic-unused")
        session.add(user)
        await session.flush()
        workspace = Workspace(name="Synthetic Investment", created_by_user_id=user.id)
        session.add(workspace)
        await session.flush()
        source = await make_holding(session, workspace.id, user.id, "source")
        destination = await make_holding(session, workspace.id, user.id, "destination")
        second_destination = await make_holding(session, workspace.id, user.id, "second destination")
        acquisition = await add_acquisition(session, source)
        v = SimpleNamespace(
            sessions=postgres_sessions, workspace=workspace, user=user, a=source, b=destination,
            c=second_destination, acquisition=acquisition, ownership={}, addresses={},
        )
        for asset, address in zip([source, destination, second_destination], ["A" * 44, "B" * 44, "C" * 44]):
            ownership = await service.create_ownership(session, workspace.id, user.id, OwnershipCreate(
                group_id=asset.group_id, beneficial_owner="synthetic-owner", chain="solana", address=address,
                reason="Reviewed synthetic same-owner mapping",
            ))
            v.ownership[asset.id] = str(ownership.id)
            v.addresses[asset.id] = address
        return v


async def pg_request(v, destination, reference, quantity="6"):
    async with v.sessions() as session:
        scoped = SimpleNamespace(**vars(v), session=session)
        return await pair(scoped, v.a, destination, quantity=quantity, reference=reference)


async def reviewed(v, request):
    from app.services import owned_transfer_service as service

    async with v.sessions() as session:
        preview = await service.preview_transfer(session, v.workspace.id, TransferPreviewRequest.model_validate(request))
        request = {**request, "allocations": [{"lot_id": preview.available_lots[0].lot_id, "quantity": str(preview.principal_quantity)}]}
        preview = await service.preview_transfer(session, v.workspace.id, TransferPreviewRequest.model_validate(request))
        assert preview.can_confirm, preview
        return TransferConfirmRequest.model_validate({**request, "expected_revision": preview.revision})


async def race_confirmations(v, requests):
    from app.services import owned_transfer_service as service

    start = asyncio.Event()
    ready = [asyncio.Event() for _ in requests]

    async def confirm(data, index):
        async with v.sessions() as session:
            ready[index].set()
            await start.wait()
            try:
                return await service.confirm_transfer(session, v.workspace.id, v.user.id, data)
            except HTTPException as exc:
                return exc

    tasks = [asyncio.create_task(confirm(data, i)) for i, data in enumerate(requests)]
    try:
        await asyncio.wait_for(asyncio.gather(*(flag.wait() for flag in ready)), timeout=5)
        start.set()
        return await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_same_confirmation_race_has_one_pair_of_committed_movements(transfer_pg_context):
    from app.services import owned_transfer_service as service

    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-duplicate", "3"))
    results = await race_confirmations(v, [request, request])
    assert any(not isinstance(result, HTTPException) for result in results), results
    for index, result in enumerate(results):
        if isinstance(result, HTTPException):
            assert result.status_code == 409 and result.detail["code"] == "investment_busy"
            async with v.sessions() as session:
                results[index] = await service.confirm_transfer(session, v.workspace.id, v.user.id, request)
    assert results[0].id == results[1].id
    async with v.sessions() as session:
        assert len(list(await session.scalars(select(InvestmentOwnedTransfer)))) == 1
        assert len(list(await session.scalars(select(InvestmentMovementApplication)))) == 2
        assert len(list(await session.scalars(select(AssetTransaction)))) == 3
        a, b = await session.get(Asset, v.a.id), await session.get(Asset, v.b.id)
        assert (a.units, b.units) == (Decimal("7"), Decimal("3"))


async def test_distinct_transfers_cannot_concurrently_consume_the_same_source_units(transfer_pg_context):
    v = transfer_pg_context
    requests = [await pg_request(v, v.b, "synthetic-out-A"), await pg_request(v, v.c, "synthetic-out-B")]
    results = await race_confirmations(v, [await reviewed(v, request) for request in requests])
    successes = [result for result in results if not isinstance(result, HTTPException)]
    failures = [result for result in results if isinstance(result, HTTPException)]
    assert len(successes) == len(failures) == 1, results
    assert failures[0].status_code in {409, 422}
    async with v.sessions() as session:
        rows = list(await session.scalars(select(Asset)))
        assert sum(asset.units for asset in rows) == Decimal("10")
        assert (await session.get(Asset, v.a.id)).units == Decimal("4")
        assert sorted(asset.units for asset in rows if asset.id != v.a.id) == [Decimal("0"), Decimal("6")]
        assert len(list(await session.scalars(select(InvestmentOwnedTransfer)))) == 1
        assert len(list(await session.scalars(select(InvestmentMovementApplication)))) == 2


async def test_second_leg_database_failure_rolls_back_reservations_both_ledgers_and_caches(transfer_pg_context):
    from app.services import owned_transfer_service as service

    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-rollback", "3"))

    def fail_destination(mapper, connection, target):
        if target.asset_id == v.b.id:
            raise RuntimeError("synthetic destination insert failure")

    event.listen(AssetTransaction, "before_insert", fail_destination)
    try:
        async with v.sessions() as session:
            with pytest.raises(RuntimeError, match="synthetic destination"):
                await service.confirm_transfer(session, v.workspace.id, v.user.id, request)
    finally:
        event.remove(AssetTransaction, "before_insert", fail_destination)
    async with v.sessions() as session:
        assert list(await session.scalars(select(InvestmentOwnedTransfer))) == []
        assert list(await session.scalars(select(InvestmentMovementApplication))) == []
        assert len(list(await session.scalars(select(AssetTransaction)))) == 1
        assert (await session.get(Asset, v.a.id)).units == Decimal("10")
        assert (await session.get(Asset, v.b.id)).units == Decimal("0")
    async with v.sessions() as session:
        result = await service.confirm_transfer(session, v.workspace.id, v.user.id, request)
        assert result.status == "confirmed"


async def test_parallel_reversal_is_idempotent_and_returns_all_units(transfer_pg_context):
    from app.services import owned_transfer_service as service

    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-reverse-race", "3"))
    async with v.sessions() as session:
        confirmed = await service.confirm_transfer(session, v.workspace.id, v.user.id, request)

    async def reverse():
        async with v.sessions() as session:
            return await service.reverse_transfer(session, v.workspace.id, confirmed.id, confirmed.revision)

    tasks = [asyncio.create_task(reverse()), asyncio.create_task(reverse())]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    completed = []
    for result in results:
        if isinstance(result, HTTPException):
            assert isinstance(result.detail, dict)
            assert result.status_code == 409 and result.detail["code"] == "investment_busy"
            result = await reverse()
        assert not isinstance(result, BaseException), result
        completed.append(result)
    assert all(result.status == "reversed" for result in completed)
    async with v.sessions() as session:
        assert (await session.get(Asset, v.a.id)).units == Decimal("10")
        assert (await session.get(Asset, v.b.id)).units == Decimal("0")
        decisions = list(await session.scalars(select(InvestmentOwnedTransfer)))
        assert len(decisions) == 1 and decisions[0].reversed_at is not None
        applications = list(await session.scalars(select(InvestmentMovementApplication)))
        assert len(applications) == 2 and all(application.reversed_at is not None for application in applications)


async def test_postgres_exact_principal_fee_partial_basis_zero_and_unknown_roundtrip(transfer_pg_context):
    from app.services import owned_transfer_service as service

    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-exact", "3.000000001"))
    async with v.sessions() as session:
        fee = await retain_movement(
            session, v.a, direction="out", reference="synthetic-exact", quantity="0.02",
            source=v.addresses[v.a.id], destination=None, leg_key="meta.fee", classification="fee",
            quantity_role="network_fee", fee_payer=v.addresses[v.a.id], fee_semantics="separate",
        )
        body = request.model_dump(mode="json", exclude={"expected_revision"})
        body["fees"] = [{
            "leg_id": str(fee.id), "asset_id": str(v.a.id), "ownership_id": v.ownership[v.a.id],
            "allocations": [{"lot_id": body["allocations"][0]["lot_id"], "quantity": "0.02"}],
            "reason": "Exact independent network fee",
        }]
        preview = await service.preview_transfer(session, v.workspace.id, TransferPreviewRequest.model_validate(body))
        confirmed = await service.confirm_transfer(session, v.workspace.id, v.user.id, TransferConfirmRequest.model_validate({
            **body, "expected_revision": preview.revision,
        }))
        assert confirmed.principal_quantity == Decimal("3.000000001")
        assert confirmed.acquisition_cost == Decimal("60.000000020")
        assert confirmed.performance_basis == Decimal("60.000000020")
        assert confirmed.model_dump(mode="json")["principal_quantity"] == "3.000000001"
    async with v.sessions() as session:
        source = await session.get(Asset, v.a.id)
        destination = await session.get(Asset, v.b.id)
        assert source.units == Decimal("6.979999999")
        assert destination.units == Decimal("3.000000001")
        txs = list(await session.scalars(select(AssetTransaction)))
        assert next(tx for tx in txs if tx.kind == "fee").quantity == Decimal("0.02")
        assert next(tx for tx in txs if tx.kind == "move_in").price is None
        assert next(tx for tx in txs if tx.kind == "buy").fee == Decimal("0")
        detail = await service.get_transfer(session, v.workspace.id, confirmed.id)
        assert detail.acquisition_cost == Decimal("60.000000020")
        assert next(row for row in detail.effects if row.asset_id == v.a.id).known_acquisition_cost == Decimal("139.599999980")
        index = await service.list_transfers(session, v.workspace.id)
        principal = next(row for row in index.movements if row.leg_id == request.out_leg_id)
        assert principal.raw_units == "3000000001" and principal.decimals == 9
        assert principal.model_dump(mode="json")["quantity"] == "3.000000001"
        assert next(row for row in index.movements if row.leg_id == fee.id).quantity == Decimal("0.02")
        c = await session.get(Asset, v.c.id)
        await add_acquisition(session, c, quantity="1", price="0", when=date(2025, 1, 3))
        zero_lots = await service.available_lots(session, v.workspace.id, v.c.id)
        assert zero_lots.lots[0].acquisition_cost == Decimal("0")
        assert zero_lots.lots[0].basis_complete
        unknown = await retain_movement(
            session, v.c, direction="in", reference="synthetic-pg-unknown", quantity="1",
            source="D" * 44, destination=v.addresses[v.c.id], when="2025-02-04T12:00:00+00:00",
        )
        request = MovementPreviewRequest(leg_id=unknown.id, asset_id=v.c.id, ownership_id=v.ownership[v.c.id], reason="Reviewed unknown acquisition")
        preview = await service.preview_movement(session, v.workspace.id, request)
        applied = await service.apply_movement(session, v.workspace.id, v.user.id, MovementConfirmRequest(
            **request.model_dump(), expected_revision=preview.revision,
        ))
        result = next(row for row in applied.effects if row.asset_id == v.c.id)
        assert result.quantity == Decimal("2")
        assert result.known_basis_quantity == result.unknown_basis_quantity == Decimal("1")
        assert result.known_acquisition_cost == Decimal("0")
        assert result.performance_basis is None and not result.basis_complete
        raw = result.model_dump(mode="json")
        unknown_lot = next(lot for lot in raw["lots"] if not lot["basis_complete"])
        assert unknown_lot["acquisition_cost"] is None and unknown_lot["acquired"] is None


async def test_confirmation_and_manual_sale_share_inventory_lock(transfer_pg_context):
    from app.schemas.asset import AssetTransactionCreate
    from app.services import asset_transaction_service, owned_transfer_service

    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-confirm-vs-sale", "6"))
    start = asyncio.Event()

    async def confirm():
        async with v.sessions() as session:
            await start.wait()
            return await owned_transfer_service.confirm_transfer(session, v.workspace.id, v.user.id, request)

    async def sell():
        async with v.sessions() as session:
            await start.wait()
            return await asset_transaction_service.add_transaction(session, v.a.id, v.workspace.id, AssetTransactionCreate(
                kind="sell", quantity="8", price="30", date=date(2025, 2, 4),
            ))

    tasks = [asyncio.create_task(confirm()), asyncio.create_task(sell())]
    start.set()
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert any(not isinstance(result, BaseException) for result in results), results
    for result in results:
        if isinstance(result, BaseException):
            assert isinstance(result, HTTPException) and result.status_code in {409, 422}, result
    async with v.sessions() as session:
        transfers = list(await session.scalars(select(InvestmentOwnedTransfer)))
        sales = list(await session.scalars(select(AssetTransaction).where(AssetTransaction.kind == "sell")))
        assert len(transfers) + len(sales) == 1
        a, b = await session.get(Asset, v.a.id), await session.get(Asset, v.b.id)
        assert (a.units, b.units) == ((Decimal("4"), Decimal("6")) if transfers else (Decimal("2"), Decimal("0")))


async def test_reversal_failure_restores_both_applications_and_lot_allocations(transfer_pg_context):
    from app.services import owned_transfer_service as service

    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-reversal-rollback", "3"))
    async with v.sessions() as session:
        original = await service.confirm_transfer(session, v.workspace.id, v.user.id, request)

    def fail_destination(mapper, connection, target):
        if target.asset_id == v.b.id:
            raise RuntimeError("synthetic destination reversal failure")

    event.listen(AssetTransaction, "before_delete", fail_destination)
    try:
        async with v.sessions() as session:
            with pytest.raises(RuntimeError, match="synthetic destination reversal"):
                await service.reverse_transfer(session, v.workspace.id, original.id, original.revision)
    finally:
        event.remove(AssetTransaction, "before_delete", fail_destination)
    async with v.sessions() as session:
        decision = await session.get(InvestmentOwnedTransfer, original.id)
        assert decision.reversed_at is None
        applications = list(await session.scalars(select(InvestmentMovementApplication)))
        assert len(applications) == 2
        assert all(row.reversed_at is None and row.transaction_id is not None for row in applications)
        assert (await session.get(Asset, v.a.id)).units == Decimal("7")
        assert (await session.get(Asset, v.b.id)).units == Decimal("3")
        assert len(list(await session.scalars(select(AssetTransaction)))) == 3
        detail = await service.get_transfer(session, v.workspace.id, original.id)
        assert detail.status == "confirmed"
        assert detail.request.allocations == request.allocations
        assert detail.acquisition_cost == Decimal("60")
