"""Real row-lock checks in a disposable schema; set EVIDENCE_TEST_DATABASE_URL."""
import asyncio
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.investment_evidence import EvidenceAllocation, EvidenceDecision
from app.services import investment_evidence_service as service
from tests.test_investment_evidence import observation

@pytest_asyncio.fixture
async def pg_context(postgres_sessions):
    async with postgres_sessions() as session:
        user = User(email="synthetic-evidence@example.invalid", hashed_password="synthetic-unused")
        session.add(user)
        await session.flush()
        workspace = Workspace(name="Synthetic Investment", created_by_user_id=user.id)
        session.add(workspace)
        await session.flush()
        group = AssetGroup(name="Synthetic Wallet", workspace_id=workspace.id, user_id=user.id)
        session.add(group)
        await session.commit()
        return postgres_sessions, workspace.id, user.id, group.id


@pytest.mark.asyncio
async def test_concurrent_apply_is_once_and_decimal_roundtrip_is_exact(pg_context):
    sessions, workspace_id, user_id, group_id = pg_context
    item = observation(quantity="0.123456789012345678")
    async with sessions() as session:
        preview = await service.preview_evidence(session, workspace_id, group_id, [item])
        saved = await service.import_evidence(session, workspace_id, user_id, group_id, [item], expected_revision=preview.revision)
    decision = EvidenceDecision(observation_ref=saved.evidence.observations[0].reference, leg_key="amount", action="apply")

    async def confirm():
        async with sessions() as session:
            return await service.confirm_evidence(session, workspace_id, user_id, group_id, [decision], saved.evidence.revision, allow_unpriced=True)

    results = await asyncio.gather(confirm(), confirm())
    assert sorted(result.imported for result in results) == [0, 1]
    async with sessions() as session:
        rows = list(await session.scalars(select(AssetTransaction)))
        assert len(rows) == 1
        assert rows[0].quantity == Decimal("0.123456789012345678")
        preview = await service.preview_evidence(session, workspace_id, group_id)
        assert preview.observations[0].legs[0].quantity == item.legs[0].quantity


@pytest.mark.asyncio
async def test_competing_links_cannot_both_confirm_the_same_observation(pg_context):
    sessions, workspace_id, user_id, group_id = pg_context
    async with sessions() as session:
        items = [observation("execution-A", execution="A"), observation("execution-B", execution="B")]
        preview = await service.preview_evidence(session, workspace_id, group_id, items)
        saved = await service.import_evidence(session, workspace_id, user_id, group_id, items, expected_revision=preview.revision)
        for source in saved.evidence.observations:
            current = await service.preview_evidence(session, workspace_id, group_id)
            await service.confirm_evidence(session, workspace_id, user_id, group_id, [
                EvidenceDecision(observation_ref=source.reference, leg_key="amount", action="apply"),
            ], current.revision, allow_unpriced=True)
        incoming = observation("export-row", source="other_csv", execution="row")
        preview = await service.preview_evidence(session, workspace_id, group_id, [incoming])
        saved = await service.import_evidence(session, workspace_id, user_id, group_id, [incoming], expected_revision=preview.revision)
        record = next(r for r in saved.evidence.records if r.application_status != "already_applied")
        assert len(record.candidate_legs) == 2

    async def link(target):
        async with sessions() as session:
            try:
                return await service.confirm_evidence(session, workspace_id, user_id, group_id, [
                    EvidenceDecision(
                        observation_ref=record.observation_ref, leg_key="amount", action="link",
                        allocations=[EvidenceAllocation(leg_id=target.leg_id, quantity="12")],
                        reason="Synthetic reviewed allocation",
                    ),
                ], saved.evidence.revision)
            except HTTPException as exc:
                return exc.status_code

    results = await asyncio.gather(*(link(target) for target in record.candidate_legs))
    assert sum(result == 409 for result in results) == 1
    assert sum(getattr(result, "linked", 0) for result in results) == 1
