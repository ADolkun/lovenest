"""Synthetic recovery overlays preserve sources, unknowns and shared eligibility."""
import csv
import io
import json
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.investment_evidence import InvestmentObservation
from app.schemas.investment_evidence import EvidenceOpeningBoundary
from app.schemas.recovery_evidence import (
    RecoveryDetails, RecoveryEntryInput, RecoveryPreviewRequest, RecoveryRetainRequest,
    RecoveryReviewInput, RecoveryReviewsRequest,
)
from app.services import investment_evidence_service as evidence
from app.services import recovery_evidence_service as service
from app.services.asset_import_service import parse_orders_csv
from tests.test_investment_evidence import observation, wallet as wallet


def entry(key='notice', role='recovery_notice', **changes):
    return RecoveryEntryInput(key=key, observation=observation(key, source_kind='tax_workpaper' if role == 'tax_workpaper' else 'primary_activity'),
                              leg_key='amount', case_key='synthetic-case', round_key='first', round_asset_key='SYN',
                              role=role, reported_state='confirmed', **changes)


async def retain(session, workspace, user, group, entries):
    request = RecoveryPreviewRequest(group_id=group.id, entries=entries)
    preview = await service.preview_recovery(session, workspace.id, request)
    return await service.retain_recovery(session, workspace.id, user.id,
                                         RecoveryRetainRequest(**request.model_dump(), expected_revision=preview.revision))


@pytest.mark.parametrize('label', ['claim', 'distribution', 'insolvency distribution', 'CLAIM_DISTRIBUTION_4'])
@pytest.mark.parametrize('price', ['0', '7'])
def test_valued_claim_labels_never_emit_a_buy_even_with_disposal_columns(label, price):
    orders, errors, _, _ = parse_orders_csv(f'ticker,date,quantity,price,kind,date sold,proceeds\nSYN,2031-04-05,2,{price},{label},2031-04-06,20\n'.encode())
    assert orders == []
    assert [error.reason for error in errors] == ['recovery_evidence_required']


@pytest.mark.parametrize('value', [0.25, 'NaN', 'Infinity', '-1', '1e1000'])
def test_role_money_validates_at_the_exact_source_boundary(value):
    with pytest.raises(ValidationError):
        RecoveryDetails(claim_amount=value)


@pytest.mark.asyncio
async def test_retention_is_replay_safe_with_new_client_key_and_preserves_nulls(session, test_workspace, test_user, wallet):
    item = entry(details=RecoveryDetails(claim_amount='0', claim_currency='USD'))
    first = await retain(session, test_workspace, test_user, wallet, [item])
    replay = item.model_copy(update={'key': 'new-ui-key'})
    second = await retain(session, test_workspace, test_user, wallet, [replay])
    assert len(first.entries) == len(second.entries) == 1
    assert first.entries[0].id == second.entries[0].id
    assert second.entries[0].details.claim_amount == 0
    assert second.entries[0].details.cash_credited is None
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == 0
    changed = item.model_copy(update={'round_key': 'other-round'})
    with pytest.raises(HTTPException) as error:
        await retain(session, test_workspace, test_user, wallet, [changed])
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_attach_preserves_source_group_and_does_not_copy_it(session, test_workspace, test_user, wallet):
    other = AssetGroup(workspace_id=test_workspace.id, user_id=test_user.id, name='Synthetic source')
    session.add(other)
    await session.commit()
    source = observation('source')
    preview = await evidence.preview_evidence(session, test_workspace.id, other.id, [source])
    saved = await evidence.import_evidence(session, test_workspace.id, test_user.id, other.id, [source], expected_revision=preview.revision)
    identifier = uuid.UUID(saved.evidence.observations[0].reference)
    item = RecoveryEntryInput(key='attachment', observation_id=identifier, leg_key='amount', role='platform_ledger', case_key='case')
    package = await retain(session, test_workspace, test_user, wallet, [item])
    assert package.entries[0].source_group_id == other.id
    assert package.entries[0].source_group_name == other.name
    assert await session.scalar(select(func.count()).select_from(InvestmentObservation)) == 1
    assert (await session.get(InvestmentObservation, identifier)).group_id == other.id
    source_preview = await evidence.preview_evidence(session, test_workspace.id, other.id)
    assert source_preview.records[0].application_status == 'eligible'


@pytest.mark.asyncio
async def test_recovery_workpaper_cannot_bypass_review_via_opening_lots(session, test_workspace, test_user, wallet):
    item = entry(role='tax_workpaper')
    await retain(session, test_workspace, test_user, wallet, [item])
    boundary = EvidenceOpeningBoundary(as_of='2025-02-03', overlap_reviewed=True, assumption='Synthetic standalone lot review')
    preview = await evidence.preview_evidence(session, test_workspace.id, wallet.id, opening_boundary=boundary)
    assert preview.records[0].application_status != 'eligible'
    assert 'recovery_evidence_only' in preview.records[0].reason_codes
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == 0


@pytest.mark.asyncio
async def test_review_revisions_corrections_and_lossless_exports(session, test_workspace, test_user, wallet):
    package = await retain(session, test_workspace, test_user, wallet, [entry(role='equity_statement')])
    row = package.entries[0]
    assertion = RecoveryReviewInput(key='allocation', kind='assertion', entry_id=row.id, assertion_kind='provisional_allocation',
                                     assertion_status='modeled', value='110.00000000000000000000000000001', currency='USD',
                                     source_locator='synthetic/model', reason='Provisional source assertion')
    request = RecoveryReviewsRequest(group_id=wallet.id, expected_revision=package.revision, reviews=[assertion])
    package = await service.review_recovery(session, test_workspace.id, test_user.id, request)
    replay = await service.review_recovery(session, test_workspace.id, test_user.id, request)
    assert len(replay.reviews) == 1
    altered = assertion.model_copy(update={'value': Decimal('80')})
    with pytest.raises(HTTPException) as error:
        await service.review_recovery(session, test_workspace.id, test_user.id, RecoveryReviewsRequest(group_id=wallet.id, expected_revision=package.revision, reviews=[altered]))
    assert error.value.status_code == 409
    for entity in (wallet, test_workspace, test_user):
        await session.refresh(entity)
    model = RecoveryReviewInput(key='model', kind='allocation', entry_id=row.id, assertion_status='modeled', source_locator='synthetic/model',
                                reason='Incomplete input preview', required_entry_ids=[row.id], required_review_ids=[package.reviews[0].id],
                                conflicting_fields=['shifted_reference'])
    package = await service.review_recovery(session, test_workspace.id, test_user.id, RecoveryReviewsRequest(group_id=wallet.id, expected_revision=package.revision, reviews=[model]))
    assert 'valuation_missing' in package.allocation_blockers
    assert 'controlling_accounting_assumption_missing' in package.allocation_blockers
    assert 'conflicting:shifted_reference' in package.allocation_blockers
    exported = json.loads(service.export_recovery(package, 'json', {}))['package']
    rows = list(csv.DictReader(io.StringIO(service.export_recovery(package, 'csv', {}))))
    assert [json.loads(item['payload_json']) for item in rows if item['record_type'] == 'entry'] == exported['entries']
    assert [json.loads(item['payload_json']) for item in rows if item['record_type'] == 'review'] == exported['reviews']
    assert next(item for item in exported['reviews'] if item['key'] == 'allocation')['value'] == '110.00000000000000000000000000001'
    assert exported['entries'][0]['details']['statement_date'] is None


@pytest.mark.asyncio
async def test_filters_include_assertion_conflicts_and_derived_missing_without_counting_context(session, test_workspace, test_user, wallet):
    saved = await retain(session, test_workspace, test_user, wallet, [entry('notice'), entry('stock', 'equity_statement')])
    stock = next(item for item in saved.entries if item.key == 'stock')
    request = RecoveryReviewsRequest(group_id=wallet.id, expected_revision=saved.revision, reviews=[RecoveryReviewInput(
        key='cost-conflict', kind='assertion', entry_id=stock.id, assertion_kind='reported_cost', assertion_status='conflict',
        value='80', currency='USD', source_locator='synthetic/stock', reason='Competing cost assertion')])
    await service.review_recovery(session, test_workspace.id, test_user.id, request)
    conflicts = await service.list_recovery(session, test_workspace.id, wallet.id, relation_state='conflict')
    assert [row.key for row in conflicts.entries] == ['stock']
    missing = await service.list_recovery(session, test_workspace.id, wallet.id, relation_state='missing')
    assert [row.key for row in missing.entries] == ['notice']
    assert 'receipt_missing' in missing.missing_evidence
    assert 'related_context' not in missing.missing_evidence


@pytest.mark.asyncio
async def test_supported_assertion_requalifies_when_source_is_superseded(session, test_workspace, test_user, wallet):
    package = await retain(session, test_workspace, test_user, wallet, [entry('stock', 'equity_statement')])
    stock = package.entries[0]
    decision = RecoveryReviewInput(key='reviewed-value', kind='assertion', entry_id=stock.id, assertion_kind='valuation',
                                    assertion_status='supported', value='80', currency='USD',
                                    supporting_observation_ids=[stock.observation_id], source_locator='synthetic/statement', reason='Source-stated valuation')
    package = await service.review_recovery(session, test_workspace.id, test_user.id, RecoveryReviewsRequest(group_id=wallet.id, expected_revision=package.revision, reviews=[decision]))
    assert package.reviews[0].ready_for_review
    source = await session.get(InvestmentObservation, stock.observation_id)
    source.is_current = False
    await session.commit()
    package = await service.list_recovery(session, test_workspace.id, wallet.id)
    assert package.reviews[0].assertion_status == 'supported'  # original decision remains recorded
    assert not package.reviews[0].ready_for_review
    assert 'supporting_source_unqualified' in package.reviews[0].blockers


@pytest.mark.asyncio
async def test_workpaper_cannot_verify_a_filing_assertion(session, test_workspace, test_user, wallet):
    package = await retain(session, test_workspace, test_user, wallet, [entry('workpaper', 'tax_workpaper')])
    row = package.entries[0]
    assertion = RecoveryReviewInput(key='filing-label', kind='assertion', entry_id=row.id, assertion_kind='filing_assertion',
                                     assertion_status='unverified', source_locator='synthetic/workpaper',
                                     supporting_observation_ids=[row.observation_id], reason='Model claims this was already filed')
    package = await service.review_recovery(session, test_workspace.id, test_user.id, RecoveryReviewsRequest(group_id=wallet.id, expected_revision=package.revision, reviews=[assertion]))
    assert 'filed_record_unverified' in package.reviews[0].blockers
    assert not package.reviews[0].ready_for_review
    supported = assertion.model_copy(update={'key': 'unsupported-filing-proof', 'assertion_status': 'supported'})
    with pytest.raises(HTTPException) as error:
        await service.review_recovery(session, test_workspace.id, test_user.id, RecoveryReviewsRequest(group_id=wallet.id, expected_revision=package.revision, reviews=[supported]))
    assert error.value.status_code == 422
    assert 'filed_record_unverified' in error.value.detail
