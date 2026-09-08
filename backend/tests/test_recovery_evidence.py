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


async def review(session, workspace, user, wallet, *decisions):
    package = await service.list_recovery(session, workspace.id, wallet.id)
    request = RecoveryReviewsRequest(group_id=wallet.id, expected_revision=package.revision, reviews=list(decisions))
    return await service.review_recovery(session, workspace.id, user.id, request)


def assertion(key, row, **changes):
    return RecoveryReviewInput(key=key, kind='assertion', entry_id=row.id, source_locator='synthetic/review',
                               reason='Synthetic provenance review', **changes)


async def modeled_inputs(session, workspace, user, wallet):
    item = entry('valued-equity', 'equity_statement')
    item.observation.legs[0].valuation_amount = Decimal('80')
    package = await retain(session, workspace, user, wallet, [item])
    row = package.entries[0]
    assumption = assertion('assumption', row, assertion_kind='accounting_assumption', assertion_status='supported',
                           supporting_observation_ids=[row.observation_id], proposed_value='USD source valuation review')
    package = await review(session, workspace, user, wallet, assumption)
    model = RecoveryReviewInput(key='model', kind='allocation', entry_id=row.id, assertion_status='modeled',
                                source_locator='synthetic/model', reason='Review preview only', value='80', currency='USD',
                                required_entry_ids=[row.id], required_review_ids=[package.reviews[0].id])
    return row, model


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['conflict', 'missing', 'unverified', 'modeled'])
async def test_model_own_status_qualifies_readiness_and_replay(session, test_workspace, test_user, wallet, status):
    _, model = await modeled_inputs(session, test_workspace, test_user, wallet)
    model.assertion_status = status
    package = await review(session, test_workspace, test_user, wallet, model)
    result = next(row for row in package.reviews if row.key == 'model')
    assert result.ready_for_review == (status == 'modeled')
    if status != 'modeled':
        assert f'assertion_{status}' in result.blockers
        resolved = model.model_copy(update={'key': 'resolved-model', 'assertion_status': 'modeled', 'supersedes_id': result.id})
        package = await review(session, test_workspace, test_user, wallet, resolved)
        assert next(row for row in package.reviews if row.key == 'resolved-model').ready_for_review
        assert not next(row for row in package.reviews if row.key == 'model').is_current
    replay = await review(session, test_workspace, test_user, wallet, model)
    assert replay.revision == package.revision
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,status', [('account_mapping', 'conflict'), ('lot_mapping', 'missing'), ('accounting_assumption', 'unverified')])
async def test_omitted_input_controls_block_until_supported_supersession(session, test_workspace, test_user, wallet, kind, status):
    row, model = await modeled_inputs(session, test_workspace, test_user, wallet)
    disputed = assertion('disputed', row, assertion_kind=kind, assertion_status=status, conflicting_fields=['source_mapping'])
    package = await review(session, test_workspace, test_user, wallet, disputed, model)
    current = next(item for item in package.reviews if item.key == 'model')
    assert not current.ready_for_review
    assert 'conflicting:source_mapping' in current.blockers
    old = next(item for item in package.reviews if item.key == 'disputed')
    assert old.id not in model.required_review_ids
    resolved = disputed.model_copy(update={'key': 'resolved', 'assertion_status': 'supported', 'conflicting_fields': [],
                                           'supersedes_id': old.id, 'supporting_observation_ids': [row.observation_id]})
    package = await review(session, test_workspace, test_user, wallet, resolved)
    assert next(item for item in package.reviews if item.key == 'model').ready_for_review
    assert not next(item for item in package.reviews if item.key == 'disputed').is_current
    replay = await review(session, test_workspace, test_user, wallet, disputed, resolved, model)
    assert replay.revision == package.revision
    exported = json.loads(service.export_recovery(replay, 'json', {}))['package']
    assert next(item for item in exported['reviews'] if item['key'] == 'disputed')['assertion_status'] == status
    assert next(item for item in exported['reviews'] if item['key'] == 'model')['ready_for_review']
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == 0


@pytest.mark.asyncio
async def test_models_exclude_other_outputs_and_unrelated_controls_but_detect_explicit_cycles(session, test_workspace, test_user, wallet):
    row, model = await modeled_inputs(session, test_workspace, test_user, wallet)
    package = await retain(session, test_workspace, test_user, wallet, [entry('unrelated', 'platform_ledger')])
    unrelated = next(item for item in package.entries if item.key == 'unrelated')
    conflict = assertion('unrelated-conflict', unrelated, assertion_kind='account_mapping', assertion_status='conflict')
    other = model.model_copy(update={'key': 'other-output', 'assertion_status': 'unverified'})
    package = await review(session, test_workspace, test_user, wallet, conflict, other, model)
    assert next(item for item in package.reviews if item.key == 'model').ready_for_review
    state = await service._load(session, test_workspace.id, wallet.id)
    models = {item.review_key: item for item in state['recovery_reviews'].values() if item.payload['kind'] == 'allocation'}
    active = {item.id for item in service._current_reviews(state)}
    current = models['model']
    current.payload = {**current.payload, 'required_review_ids': [str(current.id)]}
    assert 'model_dependency_cycle' in service._review_blockers(state, current, active)
    current.payload = {**current.payload, 'required_review_ids': [str(models['other-output'].id)]}
    models['other-output'].payload = {**models['other-output'].payload, 'required_review_ids': [str(current.id)]}
    assert 'model_dependency_cycle' in service._review_blockers(state, current, active)
    await session.rollback()  # Deliberately cyclic synthetic state is never persisted.


@pytest.mark.asyncio
@pytest.mark.parametrize('alias_kind', ['same_observation', 'physical_identity', 'canonical_link'])
async def test_receipt_round_conflicts_requalify_relations_and_allow_supported_resolution(session, test_workspace, test_user, wallet, alias_kind):
    from app.models.investment_evidence import InvestmentObservationLink

    receipt = entry('receipt', 'receiving_receipt')
    receipt.observation.legs[0].classification = 'transfer'
    if alias_kind == 'physical_identity':
        receipt.observation.legs[0] = receipt.observation.legs[0].model_copy(update={
            'chain': 'ethereum', 'token_address': 'native', 'transaction_ref': 'synthetic-receipt', 'leg_ref': '0',
            'source_address': 'synthetic-source', 'destination_address': 'synthetic-destination', 'quantity_role': 'principal',
        })
    original = [entry('notice'), entry('repeated-notice'), entry('second-round').model_copy(update={'round_key': 'second'}), receipt]
    package = await retain(session, test_workspace, test_user, wallet, original)
    rows = {row.key: row for row in package.entries}
    received = rows['receipt']

    def link(key, notice, target):
        return RecoveryReviewInput(key=key, kind='relation', entry_id=notice.id, target_entry_id=target.id,
                                    relation_kind='notice_receipt', relation_state='confirmed',
                                    source_locator='synthetic/notice-receipt', reason='Reviewed source account and timing',
                                    supporting_observation_ids=[received.observation_id], account_mapping_evidence='Synthetic account review',
                                    timing_evidence='Source dates agree')

    links = [link('first-link', rows['notice'], received), link('repeated-link', rows['repeated-notice'], received)]
    package = await review(session, test_workspace, test_user, wallet, *links)
    assert all(row.ready_for_review for row in package.reviews)
    assert all('receipt_missing' not in row.reason_codes for row in package.entries if row.key in {'notice', 'repeated-notice'})
    if alias_kind == 'same_observation':
        alias = receipt.model_copy(update={'key': 'alias', 'observation': None, 'observation_id': received.observation_id, 'round_key': 'second'})
    else:
        source = receipt.observation.model_copy(update={'reference': 'alias-source', 'source_local_id': 'alias-source', 'source': 'api'})
        alias = receipt.model_copy(update={'key': 'alias', 'observation': source, 'round_key': 'second'})
    package = await retain(session, test_workspace, test_user, wallet, [alias])
    annotated = next(row for row in package.entries if row.key == 'alias')
    if alias_kind == 'canonical_link':
        session.add(InvestmentObservationLink(workspace_id=test_workspace.id, observation_id=annotated.observation_id,
                    source_leg_key=annotated.leg_key, leg_id=received.application.leg_id, role='corroborates',
                    reason='Synthetic reviewed source alias', reviewed_by=test_user.id))
        await session.commit()
        package = await service.list_recovery(session, test_workspace.id, wallet.id)
    assert all(not row.ready_for_review and 'current_review_unqualified' in row.blockers for row in package.reviews)
    assert all('receipt_missing' in row.reason_codes for row in package.entries if row.role == 'recovery_notice')
    assert 'receipt_round_conflict' in next(row for row in package.entries if row.key == 'receipt').reason_codes
    conflicting_link = link('second-link', rows['second-round'], annotated)
    with pytest.raises(HTTPException) as error:
        await review(session, test_workspace, test_user, wallet, conflicting_link)
    assert error.value.status_code == 422
    assert 'receipt_round_conflict' in error.value.detail
    for entity in (wallet, test_workspace, test_user):
        await session.refresh(entity)
    candidate = conflicting_link.model_copy(update={'relation_state': 'candidate'})
    correction = RecoveryReviewInput(key='round-dispute', kind='correction', entry_id=annotated.id,
                                     field='round_key', proposed_value='first', assertion_status='conflict',
                                     source_locator='synthetic/round-review', reason='Conflicting annotation requires support')
    package = await review(session, test_workspace, test_user, wallet, candidate, correction)
    old = next(row for row in package.reviews if row.key == 'round-dispute')
    supported = correction.model_copy(update={'key': 'round-resolved', 'assertion_status': 'supported', 'supersedes_id': old.id,
                                              'supporting_observation_ids': [received.observation_id]})
    package = await review(session, test_workspace, test_user, wallet, supported)
    assert all(row.ready_for_review for row in package.reviews if row.key in {'first-link', 'repeated-link'})
    assert not next(row for row in package.reviews if row.key == 'second-link').ready_for_review
    assert 'receipt_missing' in next(row for row in package.entries if row.key == 'second-round').reason_codes
    assert next(row for row in package.entries if row.key == 'alias').round_key == 'second'  # Original annotation retained.
    assert not next(row for row in package.reviews if row.key == 'round-dispute').is_current
    alias_link = link('supported-alias-link', rows['repeated-notice'], annotated)
    package = await review(session, test_workspace, test_user, wallet, alias_link)
    assert next(row for row in package.reviews if row.key == 'supported-alias-link').ready_for_review
    repeated = await retain(session, test_workspace, test_user, wallet, [alias, *original])
    assert repeated.revision == package.revision
    replay = await review(session, test_workspace, test_user, wallet, *links, candidate, correction, supported, alias_link)
    assert replay.revision == package.revision
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == 0
