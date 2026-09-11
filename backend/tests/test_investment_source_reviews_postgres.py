"""Exact acquisition corrections and serialized confirmation in disposable PostgreSQL schemas."""
import asyncio
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.investment_evidence import InvestmentLeg, InvestmentObservation, InvestmentSourceReview
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.investment_source_review import SourceReviewConfirm, SourceReviewRequest, SourceSemantics
from app.services import investment_evidence_service as evidence
from app.services import investment_source_review_service as reviews
from tests.test_investment_evidence import apply, observation, save


@pytest.mark.asyncio
async def test_postgres_exact_correction_concurrent_apply_key_collision_and_reversal(postgres_sessions):
    async with postgres_sessions() as session:
        user = User(email="source-review@example.invalid", hashed_password="unused-synthetic")
        session.add(user)
        await session.flush()
        workspace = Workspace(name="Synthetic corrections", created_by_user_id=user.id)
        session.add(workspace)
        await session.flush()
        group = AssetGroup(name="Synthetic wallet", user_id=user.id, workspace_id=workspace.id)
        session.add(group)
        await session.commit()
        workspace_id, user_id, group_id = workspace.id, user.id, group.id
        original = observation("api", source="coinbase_api", quantity="0.123456789012345678")
        result = await save(session, workspace, user, group, [original])
        await apply(session, workspace, user, group, result.evidence)
        source = observation("csv", quantity="0.123456789012345678")
        source.legs[0].unit_price = source.legs[0].subtotal = source.legs[0].fee = None
        source.legs[0].total = Decimal("0.987654312098765424")
        await save(session, workspace, user, group, [source])
        rows = list(await session.scalars(select(InvestmentObservation)))
        legs = list(await session.scalars(select(InvestmentLeg)))
        selected = next(row for row in rows if row.payload["source"] == "csv")
        target = next(row for row in rows if row.payload["source"] == "coinbase_api")
        declaration = SourceReviewRequest(group_id=group_id, request_key="association", action="associate", reason="Synthetic exact execution evidence",
            source_leg_id=next(row.id for row in legs if row.observation_id == selected.id),
            target_leg_id=next(row.id for row in legs if row.observation_id == target.id), same_execution_reviewed=True,
            source_semantics=SourceSemantics(clock_role="posted", amount_field="total", amount_meaning="fee_inclusive_total", decimal_places=18),
            target_semantics=SourceSemantics(clock_role="execution", amount_field="unit_price", amount_meaning="valuation"))
        preview = await reviews.preview_review(session, workspace_id, declaration)
        package = await reviews.confirm_review(session, workspace_id, user_id, SourceReviewConfirm(request=declaration, expected_revision=preview.revision, preview_digest=preview.preview_digest))
        request = SourceReviewRequest(group_id=group_id, request_key="correction", action="correct", review_id=package.reviews[-1].id, reason="Documented inclusive acquisition amount")
        preview = await reviews.preview_review(session, workspace_id, request)
        assert preview.supported, preview.blockers
        confirm = SourceReviewConfirm(request=request, expected_revision=preview.revision, preview_digest=preview.preview_digest)

    async def apply_once(data):
        async with postgres_sessions() as session:
            try:
                return await reviews.confirm_review(session, workspace_id, user_id, data)
            except HTTPException as exc:
                return exc

    results = await asyncio.wait_for(asyncio.gather(apply_once(confirm), apply_once(confirm)), timeout=15)
    assert any(not isinstance(result, Exception) for result in results)
    for result in results:
        if isinstance(result, HTTPException):
            assert result.status_code == 409 and result.detail["code"] == "investment_busy"
            result = await apply_once(confirm)
        assert not isinstance(result, Exception)
    collision = confirm.model_copy(update={"request": request.model_copy(update={"reason": "Conflicting request identity"})})
    assert (await apply_once(collision)).status_code == 409
    async with postgres_sessions() as session:
        txs = list(await session.scalars(select(AssetTransaction)))
        assert len(txs) == 1 and txs[0].quantity == Decimal("0.123456789012345678")
        assert txs[0].price == 8 and txs[0].fee == 0
        retained = list(await session.scalars(select(InvestmentSourceReview)))
        assert len(retained) == 2
        applied = next(row for row in retained if row.payload["request"]["action"] == "correct")
        assert applied.payload["effects"]["original_source_fee"] is None
        reverse = SourceReviewRequest(group_id=group_id, request_key="reverse", action="reverse", review_id=applied.id, reason="Restore prior synthetic acquisition")
        preview = await reviews.preview_review(session, workspace_id, reverse)
        assert preview.supported, preview.blockers
        data = SourceReviewConfirm(request=reverse, expected_revision=preview.revision, preview_digest=preview.preview_digest)
    results = await asyncio.wait_for(asyncio.gather(apply_once(data), apply_once(data)), timeout=15)
    for result in results:
        if isinstance(result, HTTPException):
            assert result.status_code == 409
            result = await apply_once(data)
        assert not isinstance(result, Exception)
    async with postgres_sessions() as session:
        assert (await session.scalars(select(AssetTransaction))).one().price == 7
        assert len(list(await session.scalars(select(InvestmentSourceReview)))) == 3
        view = await evidence.preview_evidence(session, workspace_id, group_id)
        assert all(row.application_status == "already_applied" for row in view.records)
