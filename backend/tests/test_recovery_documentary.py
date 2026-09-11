"""Documentary recovery links do not certify inventory, basis or financial lineage."""
import csv
import io
import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.investment_evidence import InvestmentObservation
from app.models.workspace import Workspace, WorkspaceMember
from app.services import recovery_evidence_service as service
from tests.test_owned_transfers_integration import checked, transfers as transfers
from tests.test_recovery_evidence_integration import RECOVERY, decision, entry, package, retain, review, transaction_count

pytestmark = pytest.mark.asyncio
ISIN = 'US0000000001'


def source(key, role='receiving_receipt', quantity='10', **leg):
    item = entry(key, role, quantity=quantity)
    item['observation']['legs'][0].update(isin=ISIN, **leg)
    return item


def association(saved, kind='documentary_receipt_disposition', quantity='4', **changes):
    a, b = [next(row for row in saved['entries'] if row['key'] == key) for key in ('left', 'right')]
    return decision('documentary', a['id'], target_entry_id=b['id'], relation_kind=kind,
                    relation_state='confirmed', supporting_observation_ids=[a['observation_id'], b['observation_id']],
                    account_mapping_evidence='Source accounts documented together', timing_evidence='Source dates reviewed',
                    **({'documentary_quantity': quantity} if kind == 'documentary_receipt_disposition' else {}), **changes)


async def post_review(v, saved, value):
    return await v.client.post(f'{RECOVERY}/reviews', headers=v.headers, json={
        'group_id': str(v.b.group_id), 'expected_revision': saved['revision'], 'reviews': [value]})


async def test_partial_documentary_link_preserves_exact_sources_and_strict_financial_lineage(transfers):
    v = transfers
    exact = '4.123456789012345678901234567890123456789'
    before = await transaction_count(v.session)
    saved = await retain(v, [source('left'), source('right', 'disposition', exact)])
    originals = [row['observation'] for row in saved['entries']]
    value = association(saved, quantity=exact)
    saved = checked(await post_review(v, saved, value))
    assert saved['reviews'][0]['documentary_quantity'] == exact
    assert saved['reviews'][0]['ready_for_review']
    assert [row['observation'] for row in saved['entries']] == originals
    assert await transaction_count(v.session) == before
    financial = {**value, 'key': 'financial', 'relation_kind': 'receipt_disposition', 'documentary_quantity': None}
    response = await post_review(v, saved, financial)
    assert response.status_code == 422 and 'Receipt lineage' in response.text
    for format in ('json', 'csv'):
        response = await v.client.get(f'{RECOVERY}/export', headers=v.headers, params={
            'group_id': str(v.b.group_id), 'format': format, 'expected_revision': saved['revision']})
        assert response.status_code == 200 and exact in response.text
        assert 'documentary_receipt_disposition' in response.text
    replay = await review(v, [value])
    assert len(replay['reviews']) == 1
    superseding = {**value, 'key': 'withdrawn', 'relation_state': 'candidate', 'supersedes_id': saved['reviews'][0]['id']}
    replay = await review(v, [superseding])
    assert {(r['is_current'], r['relation_state']) for r in replay['reviews']} == {(False, 'confirmed'), (True, 'candidate')}


@pytest.mark.parametrize('problem', ['ticker_only', 'same_unverified_holding', 'provider_namespace', 'isin_conflict',
                                   'unknown_quantity', 'missing_amount', 'zero_amount', 'over_receipt', 'over_sale',
                                   'account', 'timing', 'date', 'support', 'case', 'rounding', 'transfer'])
async def test_documentary_confirmation_rejects_unproven_identity_quantity_and_scope(transfers, problem):
    v = transfers
    left, right = source('left'), source('right', 'disposition', '4')
    if problem in {'ticker_only', 'same_unverified_holding', 'provider_namespace'}:
        for item in (left, right):
            item['observation']['legs'][0]['isin'] = None
            if problem == 'same_unverified_holding':
                item['observation']['legs'][0]['asset_id'] = str(v.b.id)
            if problem == 'provider_namespace':
                item['observation']['legs'][0]['provider_asset_id'] = 'same-string'
        if problem == 'provider_namespace':
            right['observation']['provider'] = 'different-provider'
    if problem == 'isin_conflict':
        right['observation']['legs'][0]['isin'] = 'US0000000002'
    if problem == 'unknown_quantity':
        right['observation']['legs'][0]['quantity'] = None
    if problem == 'over_receipt':
        left['observation']['legs'][0]['quantity'] = '2'
    if problem == 'date':
        right['observation'].update(event_date=None, time_precision='unknown')
    if problem == 'case':
        right['case_key'] = 'another-case'
    saved = await retain(v, [left, right])
    value = association(saved)
    for name, field, invalid in [('missing_amount', 'documentary_quantity', None), ('zero_amount', 'documentary_quantity', '0'),
                                 ('over_sale', 'documentary_quantity', '5'), ('account', 'account_mapping_evidence', None),
                                 ('timing', 'timing_evidence', None), ('support', 'supporting_observation_ids', []),
                                 ('rounding', 'quantity_adjustment', '0'), ('transfer', 'owned_transfer_id', str(uuid.uuid4()))]:
        if problem == name:
            value[field] = invalid
    result = await post_review(v, saved, value)
    assert result.status_code == 422, result.text
    if problem not in {'rounding', 'transfer'}:
        value['relation_state'] = 'candidate'
        # Cross-case references are invalid even as candidates.
        result = await post_review(v, saved, value)
        assert result.status_code == (422 if problem == 'case' else 200), result.text


@pytest.mark.parametrize('role', ['allowed_claim', 'recovery_notice'])
async def test_documentary_equity_origin_keeps_claim_dollars_separate_from_shares(transfers, role):
    v = transfers
    left = entry('left', role, quantity='1000', symbol='USD', details={'claim_amount': '1000', 'claim_currency': 'USD'})
    right = source('right', 'equity_statement', '4')
    right['round_asset_key'] = left['round_asset_key'] = 'security-round'
    right['details'] = {'reported_cost': '0', 'reported_cost_currency': 'USD'}
    saved = await retain(v, [left, right])
    before = await transaction_count(v.session)
    saved = checked(await post_review(v, saved, association(saved, kind='documentary_equity_distribution')))
    assert saved['reviews'][0]['ready_for_review']
    assert saved['reviews'][0]['documentary_quantity'] is None
    assert next(row for row in saved['entries'] if row['key'] == 'right')['details']['reported_cost'] == '0'
    assert await transaction_count(v.session) == before
    if role == 'recovery_notice':
        assert 'receipt_missing' in saved['missing_evidence']


@pytest.mark.parametrize('problem', ['security', 'round', 'round_asset', 'roles'])
async def test_documentary_equity_confirmation_preserves_security_and_round_boundaries(transfers, problem):
    left, right = source('left', 'recovery_notice'), source('right', 'equity_statement', '4')
    if problem == 'security':
        right['observation']['legs'][0]['isin'] = None
    elif problem == 'round':
        right['round_key'] = 'other-round'
    elif problem == 'round_asset':
        right['round_asset_key'] = 'other-security'
    else:
        left['role'] = 'platform_ledger'
    saved = await retain(transfers, [left, right])
    response = await post_review(transfers, saved, association(saved, kind='documentary_equity_distribution'))
    assert response.status_code == 422


async def test_associated_holding_requalifies_without_exposing_moved_workspace_data(transfers):
    v = transfers
    v.b.isin = ISIN
    v.b.units = Decimal(0)
    await v.session.commit()
    items = [source('left', asset_id=str(v.b.id)), source('right', 'disposition', '4', asset_id=str(v.b.id))]
    saved = await retain(v, items)
    assert all(Decimal(row['associated_holding']['stored_quantity']) == 0 for row in saved['entries'])
    assert all(row['associated_holding']['quantity_observed_at'] is None for row in saved['entries'])
    saved = checked(await post_review(v, saved, association(saved)))
    original_revision = saved['revision']
    v.b.units = None
    v.b.is_archived = True
    await v.session.commit()
    saved = await package(v)
    assert saved['reviews'][0]['blockers']
    assert all(row['associated_holding']['stored_quantity'] is None for row in saved['entries'])
    assert 'holding_archived' in saved['entries'][0]['associated_holding']['reason_codes']
    stale = await v.client.get(f'{RECOVERY}/export', headers=v.headers, params={
        'group_id': str(v.b.group_id), 'format': 'json', 'expected_revision': original_revision})
    assert stale.status_code == 409
    foreign = Workspace(name='Synthetic foreign', created_by_user_id=v.user.id)
    v.session.add(foreign)
    await v.session.flush()
    v.b.workspace_id = foreign.id
    v.b.name = 'Foreign holding must not appear'
    await v.session.commit()
    saved = await package(v)
    assert 'Foreign holding must not appear' not in json.dumps(saved)
    assert saved['entries'][0]['associated_holding']['reason_codes'] == ['associated_holding_unavailable']
    assert saved['reviews'][0]['blockers']


async def test_manual_association_wallet_identity_viewer_and_source_requalification(transfers):
    v = transfers
    item = source('wrong-wallet', asset_id=str(v.c.id))
    response = await v.client.post(f'{RECOVERY}/preview', headers=v.headers, json={'group_id': str(v.b.group_id), 'entries': [item]})
    assert response.status_code == 404
    v.b.isin = 'US0000000002'
    await v.session.commit()
    item['observation']['legs'][0]['asset_id'] = str(v.b.id)
    response = await v.client.post(f'{RECOVERY}/preview', headers=v.headers, json={'group_id': str(v.b.group_id), 'entries': [item]})
    assert response.status_code == 422
    v.b.isin = ISIN
    await v.session.commit()
    saved = await retain(v, [source('left', asset_id=str(v.b.id)), source('right', 'disposition', '4')])
    value = association(saved)
    saved = checked(await post_review(v, saved, value))
    observation = await v.session.get(InvestmentObservation, uuid.UUID(saved['entries'][0]['observation_id']))
    observation.is_current = False
    await v.session.commit()
    saved = await package(v)
    assert saved['reviews'][0]['blockers'] and not saved['reviews'][0]['ready_for_review']
    membership = await v.session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == v.workspace.id, WorkspaceMember.user_id == v.user.id))
    membership.role = 'viewer'
    await v.session.commit()
    result = await post_review(v, saved, {**value, 'key': 'viewer'})
    assert result.status_code == 403
    for format in ('json', 'csv'):
        result = await v.client.get(f'{RECOVERY}/export', headers=v.headers, params={'group_id': str(v.b.group_id), 'format': format})
        assert result.status_code == 200
        if format == 'csv':
            rows = [json.loads(row['payload_json']) for row in csv.DictReader(io.StringIO(result.text))]
            assert any(row.get('associated_holding') for row in rows)


async def test_verified_holding_identity_can_support_both_legs_but_conflicts_requalify(transfers):
    v = transfers
    v.b.isin = ISIN
    await v.session.commit()
    items = [source('left', asset_id=str(v.b.id)), source('right', 'disposition', '4', asset_id=str(v.b.id))]
    for item in items:
        item['observation']['legs'][0]['isin'] = None
    saved = await retain(v, items)
    saved = checked(await post_review(v, saved, association(saved)))
    assert saved['reviews'][0]['ready_for_review']
    v.b.isin = None
    await v.session.commit()
    saved = await package(v)
    assert saved['reviews'][0]['blockers']
    state = await service._load(v.session, v.workspace.id, v.b.group_id)
    state['assets'][v.b.id].units = Decimal('0.123456789012345678901234567890123456789')
    projected = service._project(state, v.workspace.id, v.b.group_id)
    assert projected.entries[0].associated_holding.model_dump(mode='json')['stored_quantity'] == '0.123456789012345678901234567890123456789'


async def test_shared_isin_can_bridge_distinct_provider_identifiers(transfers):
    left = source('left', provider_asset_id='provider-a-id')
    right = source('right', 'disposition', '4', provider_asset_id='provider-b-id')
    right['observation']['provider'] = 'another-provider'
    saved = await retain(transfers, [left, right])
    saved = checked(await post_review(transfers, saved, association(saved)))
    assert saved['reviews'][0]['ready_for_review']


async def test_documentary_sources_cannot_contradict_reviewed_holding_token_program(transfers):
    v = transfers
    v.b.external_metadata = {'evidence_asset_identity': {'chain': 'solana', 'token_address': 'mint', 'token_program': 'program-a'}}
    await v.session.commit()
    items = [source('left'), source('right', 'disposition', '4')]
    for item in items:
        item['observation']['legs'][0].update(isin=None, asset_id=str(v.b.id), chain='solana', token_address='mint', token_program='program-b')
    saved = await retain(v, items)
    response = await post_review(v, saved, association(saved))
    assert response.status_code == 422 and 'token program' in response.text


@pytest.mark.parametrize('token_program', ['program-b', None])
async def test_retained_holding_program_change_qualifies_list_and_exports(transfers, token_program):
    v = transfers
    identity = {'chain': 'solana', 'token_address': 'synthetic-mint', 'token_program': 'program-a'}
    v.b.external_metadata = {'evidence_asset_identity': identity}
    await v.session.commit()
    item = source('receipt', asset_id=str(v.b.id), **identity)
    item['observation']['legs'][0]['isin'] = None
    before = await transaction_count(v.session)
    saved = await retain(v, [item])
    original = saved['entries'][0]
    reason = 'holding_identity_conflict:token_program'
    assert reason not in original['associated_holding']['reason_codes']
    old_revision = saved['revision']
    v.b.external_metadata = {'evidence_asset_identity': {**identity, 'token_program': token_program}}
    await v.session.commit()
    saved = await package(v)
    assert saved['revision'] != old_revision
    assert saved['reviews'] == []
    projections = [saved['entries'][0]]
    for format in ('json', 'csv'):
        params = {'group_id': str(v.b.group_id), 'format': format}
        stale = await v.client.get(f'{RECOVERY}/export', headers=v.headers,
                                   params={**params, 'expected_revision': old_revision})
        assert stale.status_code == 409
        response = await v.client.get(f'{RECOVERY}/export', headers=v.headers,
                                      params={**params, 'expected_revision': saved['revision']})
        assert response.status_code == 200
        if format == 'json':
            projections.append(response.json()['package']['entries'][0])
        else:
            projections.append(next(json.loads(row['payload_json']) for row in
                                    csv.DictReader(io.StringIO(response.text)) if row['record_type'] == 'entry'))
    for projected in projections:
        assert projected['id'] == original['id']
        assert projected['observation_id'] == original['observation_id']
        assert projected['observation'] == original['observation']
        assert projected['application'] == original['application']
        assert (reason in projected['associated_holding']['reason_codes']) == (token_program is not None)
    assert await transaction_count(v.session) == before


async def test_reviewed_holding_provider_id_without_namespace_cannot_fill_source_identity(transfers):
    v = transfers
    # The reviewed holding namespace has an ID but does not record its issuing provider.
    v.b.external_metadata = {'evidence_asset_identity': {'provider_asset_id': 'ambiguous-security-id'}}
    await v.session.commit()
    items = [source('left'), source('right', 'disposition', '4')]
    for item in items:
        item['observation']['legs'][0].update(isin=None, asset_id=str(v.b.id))
    items[1]['observation']['provider'] = 'another-provider'
    saved = await retain(v, items)
    response = await post_review(v, saved, association(saved))
    assert response.status_code == 422
