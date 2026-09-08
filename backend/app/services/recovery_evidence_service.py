"""Recovery review is an evidence overlay; existing movement APIs own quantities."""
import csv
import io
import json
import uuid
from decimal import Decimal, localcontext

from fastapi import HTTPException
from sqlalchemy import select

from app.models.investment_evidence import InvestmentLeg
from app.models.recovery_evidence import InvestmentRecoveryEntry, InvestmentRecoveryReview
from app.schemas.investment_evidence import EvidenceLegInput
from app.schemas.recovery_evidence import (
    RecoveryApplication, RecoveryEntryRead, RecoveryPackage, RecoveryReviewInput,
    RecoveryReviewRead,
)
from app.services import investment_evidence_service as evidence
from app.services import owned_transfer_service as movements


async def _load(session, workspace_id, group_id, *, lock=False):
    await evidence._scope(session, workspace_id, group_id, lock=lock)
    state = await movements.prepare_replay(session, workspace_id)
    for key, model in (('recovery_entries', InvestmentRecoveryEntry), ('recovery_reviews', InvestmentRecoveryReview)):
        state[key] = {row.id: row for row in (await session.scalars(select(model).where(
            model.workspace_id == workspace_id,
        ).execution_options(populate_existing=True))).all()}
    state['recovery_revision'] = evidence._digest({
        'source': state['revision'],
        'group_id': str(group_id),
        **{key: [(str(row.id), row.payload) for row in sorted(state[key].values(), key=lambda row: str(row.id))]
           for key in ('recovery_entries', 'recovery_reviews')},
    })
    return state


def _get(state, key, identifier):
    row = state[key].get(identifier)
    if row is None:
        raise HTTPException(404, 'Referenced recovery evidence is unavailable in this workspace')
    return row


def _entry_payload(item):
    return item.model_dump(mode='json', exclude={'observation', 'observation_id'})


def _source(state, group_id, item):
    if item.observation_id is not None:
        row = _get(state, 'observations', item.observation_id)
        observation = evidence._input(row)
    else:
        observation = item.observation
    part = next((leg for leg in observation.legs if leg.key == item.leg_key), None)
    if part is None:
        raise HTTPException(422, 'Source leg is absent from the observation')
    for leg in observation.legs:
        if leg.asset_id:
            asset = _get(state, 'assets', leg.asset_id)
            source_group = row.group_id if item.observation_id else group_id
            if asset.group_id != source_group:
                raise HTTPException(404, 'Holding is unavailable in the selected wallet')
            if set(evidence._holding_conflicts(leg, asset)) - {'unverified_holding_identity'}:
                raise HTTPException(422, 'Holding identity conflicts with source evidence')
    # A role annotation cannot turn a notice into primary economic activity.
    if item.observation_id is None:
        source_kind = {'allowed_claim': 'recovery_notice', 'recovery_notice': 'recovery_notice',
                       'equity_statement': 'balance_snapshot', 'tax_workpaper': 'tax_workpaper'}.get(item.role)
        if source_kind:
            observation = observation.model_copy(update={'source_kind': source_kind})
    if item.role == 'receiving_receipt' and (part.direction != 'in' or part.classification not in {'transfer', 'unknown', 'recovery', 'acquisition'}):
        raise HTTPException(422, 'A recovery receipt must remain incoming quantity evidence, not a priced acquisition')
    if item.role == 'disposition' and part.direction not in {'out', 'unknown'}:
        raise HTTPException(422, 'Disposition direction conflicts with source evidence')
    return observation


def _identity(group_id, state, observation, item):
    group = state['groups'][group_id]
    source_key = state['observations'][item.observation_id].identity_key if item.observation_id else evidence._identity(group_id, group.connection_id, observation)
    return evidence._digest([source_key, item.leg_key, item.role])


def _application(state, row, role):
    if role != 'receiving_receipt':
        return RecoveryApplication(leg_id=row.leg_id, reason_codes=['recovery_evidence_only'])
    app = movements._application(state, row.leg_id)
    reasons = movements._movement_reasons(state, row.leg_id)
    if app:
        qualification = state['qualification'].get(app.id, {})
        reasons = sorted(set(reasons + qualification.get('missing_links', [])))
        return RecoveryApplication(leg_id=row.leg_id, application_id=app.id,
                                   status='reversed' if app.reversed_at else 'applied', reason_codes=reasons)
    if not movements.physical_movement_key(state['legs'][row.leg_id].payload):
        reasons = sorted(set(reasons + ['offchain_receipt_application_unsupported']))
    return RecoveryApplication(leg_id=row.leg_id, status='unsupported' if reasons else 'unapplied', reason_codes=reasons)


def _read_entry(state, row):
    observation = evidence._input(_get(state, 'observations', row.observation_id))
    reasons = []
    peers = [peer for peer in state['recovery_entries'].values() if peer.entry_key == row.entry_key]
    if len({peer.fingerprint for peer in peers}) > 1:
        reasons.append('recovery_annotation_conflict')
    if not state['observations'][row.observation_id].is_current:
        reasons.append('source_unqualified')
    if str(row.observation_id) in state['conflicting_observations']:
        reasons.append('source_version_conflict')
    if row.payload['role'] in {'recovery_notice', 'receiving_receipt'}:
        if not row.payload.get('round_key'):
            reasons.append('round_identity_missing')
        if not row.payload.get('round_asset_key'):
            reasons.append('round_asset_identity_missing')
    if row.payload['role'] == 'recovery_notice':
        superseded = {review.supersedes_id for review in state['recovery_reviews'].values() if review.supersedes_id}
        receipt_links = [review for review in state['recovery_reviews'].values() if review.id not in superseded
                         and review.anchor_entry_id == row.id and review.payload.get('relation_kind') == 'notice_receipt'
                         and review.payload.get('target_entry_id') and review.payload.get('relation_state') == 'confirmed']
        if not receipt_links:
            reasons.append('receipt_missing')
    source_group = state['groups'].get(state['observations'][row.observation_id].group_id)
    return RecoveryEntryRead(**row.payload, id=row.id, observation_id=row.observation_id, observation=observation,
                             source_group_id=source_group.id if source_group else None, source_group_name=source_group.name if source_group else None,
                             application=_application(state, row, row.payload['role']), reason_codes=reasons)


def _review_blockers(state, review, active_ids, seen=None):
    seen = set(seen or ())
    if review.id in seen:
        return ['model_dependency_cycle']
    seen.add(review.id)
    data = RecoveryReviewInput.model_validate(review.payload)
    blockers = list(data.missing_evidence) + [f'conflicting:{field}' for field in data.conflicting_fields]
    if data.relation_state == 'confirmed' or data.assertion_status == 'supported':
        try:
            _validate_review(state, review.group_id, data.model_copy(update={'supersedes_id': None}))
        except HTTPException as exc:
            blockers.extend(['current_review_unqualified', str(exc.detail)])
    if data.kind != 'allocation':
        if data.kind == 'relation' and data.relation_state != 'confirmed':
            blockers.append(f'relation_{data.relation_state}')
        elif data.kind != 'relation' and data.assertion_status != 'supported':
            blockers.append(f'assertion_{data.assertion_status}')
    elif not data.required_entry_ids or not data.required_review_ids:
        blockers.append('model_required_inputs_and_assumptions_missing')
    if data.kind == 'allocation':
        anchor = state['recovery_entries'][data.entry_id]
        case_entries = [row for row in state['recovery_entries'].values() if row.payload['case_key'] == anchor.payload['case_key']
                        and row.payload['role'] in {'recovery_notice', 'receiving_receipt', 'equity_statement'}]
        required = [state['recovery_entries'][identifier] for identifier in data.required_entry_ids if identifier in state['recovery_entries']]
        required_rounds = {(row.payload.get('round_key'), row.payload.get('round_asset_key')) for row in required}
        for row in case_entries:
            round_identity = row.payload.get('round_key'), row.payload.get('round_asset_key')
            if not all(round_identity):
                blockers.append('model_round_identity_missing')
            elif round_identity not in required_rounds:
                blockers.append('model_round_input_missing')
        assumptions = [state['recovery_reviews'][identifier] for identifier in data.required_review_ids if identifier in state['recovery_reviews']]
        if not any(row.payload.get('assertion_kind') == 'accounting_assumption' for row in assumptions):
            blockers.append('controlling_accounting_assumption_missing')
        for row in required:
            leg = state['legs'][row.leg_id].payload
            supported_values = [part for part in assumptions if part.anchor_entry_id == row.id and part.payload.get('assertion_kind') == 'valuation'
                                and part.payload.get('assertion_status') == 'supported' and part.payload.get('value') is not None and part.payload.get('currency')]
            if row.payload['role'] in {'recovery_notice', 'receiving_receipt', 'equity_statement'} and not supported_values:
                if leg.get('valuation_amount') is None:
                    blockers.append('valuation_missing')
                if not leg.get('valuation_currency'):
                    blockers.append('valuation_currency_missing')
    if data.kind == 'assertion' and data.assertion_kind in {'valuation', 'reported_cost', 'provisional_allocation'}:
        if data.value is None:
            blockers.append('valuation_missing')
        if not data.currency:
            blockers.append('valuation_currency_missing')
    if data.assertion_kind == 'filing_assertion':
        blockers.append('filed_record_unverified')
    for identifier in data.supporting_observation_ids:
        observation = state['observations'].get(identifier)
        if observation is None or not observation.is_current:
            blockers.append('supporting_source_unqualified')
    for identifier in data.required_entry_ids:
        row = state['recovery_entries'].get(identifier)
        if row is None:
            blockers.append('required_entry_missing')
            continue
        item = _read_entry(state, row)
        blockers.extend(item.missing_evidence + item.reason_codes)
        if item.reported_state != 'confirmed':
            blockers.append('required_entry_unresolved')
        if item.role == 'tax_workpaper':
            blockers.append('workpaper_is_modeled')
    for identifier in data.required_review_ids:
        required = state['recovery_reviews'].get(identifier)
        if required is None or identifier not in active_ids:
            blockers.append('required_review_missing_or_superseded')
        else:
            blockers.extend(_review_blockers(state, required, active_ids, seen))
    return sorted(set(blockers))


def _project(state, workspace_id, group_id, *, case_key=None, round_key=None, role=None, relation_state=None, q=None):
    all_entries = {row.id: _read_entry(state, row) for row in state['recovery_entries'].values()}
    superseded = {row.supersedes_id for row in state['recovery_reviews'].values() if row.supersedes_id}
    active_ids = set(state['recovery_reviews']) - superseded
    selected = set()
    for identifier, item in all_entries.items():
        if state['recovery_entries'][identifier].group_id != group_id:
            continue
        if case_key is not None and item.case_key != case_key or round_key is not None and item.round_key != round_key or role is not None and item.role != role:
            continue
        related_reviews = [row for row in state['recovery_reviews'].values() if row.id in active_ids
                           and (row.anchor_entry_id == identifier or row.payload.get('target_entry_id') == str(identifier))]
        statuses = {item.reported_state}
        for row in related_reviews:
            statuses.update({row.payload.get('relation_state'), row.payload.get('assertion_status')})
        if item.missing_evidence or any('missing' in reason or 'unresolved' in reason for reason in item.reason_codes):
            statuses.add('missing')
        if any('conflict' in reason for reason in item.reason_codes):
            statuses.add('conflict')
        if relation_state is not None and relation_state not in statuses:
            continue
        if q and q.casefold() not in json.dumps(item.model_dump(mode='json'), ensure_ascii=False).casefold():
            continue
        selected.add(identifier)
    # Include the reviewed relation/dependency closure, never unrelated wallet records.
    included, review_ids = set(selected), set()
    changed = True
    while changed:
        before = len(included), len(review_ids)
        for row in state['recovery_reviews'].values():
            data = row.payload
            if row.anchor_entry_id in included or row.id in review_ids or data.get('target_entry_id') in {str(identifier) for identifier in included}:
                review_ids.add(row.id)
                included.add(row.anchor_entry_id)
                included.update(uuid.UUID(identifier) for identifier in data.get('required_entry_ids', []))
                if data.get('target_entry_id'):
                    included.add(uuid.UUID(data['target_entry_id']))
                review_ids.update(uuid.UUID(identifier) for identifier in data.get('required_review_ids', []))
                if row.supersedes_id:
                    review_ids.add(row.supersedes_id)
        changed = before != (len(included), len(review_ids))
    entries = [item for identifier, item in all_entries.items() if identifier in included]
    for item in entries:
        if item.id not in selected:
            item.reason_codes.append('related_context')
    reviews = []
    for row in state['recovery_reviews'].values():
        if row.id not in review_ids:
            continue
        blockers = _review_blockers(state, row, active_ids)
        reviews.append(RecoveryReviewRead(**row.payload, id=row.id, created_at=row.created_at, created_by=row.created_by,
                                         is_current=row.id in active_ids, blockers=blockers, ready_for_review=not blockers and row.id in active_ids))
    selected_entries = [all_entries[identifier] for identifier in selected]
    rounds = {(item.case_key, item.round_key) for item in selected_entries if item.round_key}
    assets = {(item.case_key, item.round_key, item.round_asset_key) for item in selected_entries if item.round_key and item.round_asset_key}
    entries.sort(key=lambda item: (item.case_key, item.round_key or '', item.key, str(item.id)))
    reviews.sort(key=lambda item: (str(item.created_at), str(item.id)))
    return RecoveryPackage(workspace_id=workspace_id, group_id=group_id, revision=state['recovery_revision'],
                           entries=entries, reviews=reviews,
                           round_count=len(rounds), asset_record_count=len(assets),
                           missing_evidence=sorted({reason for item in entries for reason in item.missing_evidence + item.reason_codes if reason != 'related_context'}
                                                   | {reason for item in reviews if item.is_current for reason in item.missing_evidence + item.blockers}),
                           allocation_blockers=sorted({reason for item in reviews if item.kind == 'allocation' and item.is_current for reason in item.blockers}),
                           coverage=['evidence_review_only', 'history_completeness_unverified', 'offchain_receipt_application_unsupported'])


async def list_recovery(session, workspace_id, group_id, **filters):
    return _project(await _load(session, workspace_id, group_id), workspace_id, group_id, **filters)


def _drafts(state, group_id, entries):
    if len({item.key for item in entries}) != len(entries):
        raise HTTPException(422, 'Recovery entry keys must be unique within a request')
    drafts = []
    for item in entries:
        observation = _source(state, group_id, item)
        identity = _identity(group_id, state, observation, item)
        facts = {key: value for key, value in _entry_payload(item).items() if key != 'key'}
        fingerprint = evidence._digest([evidence._fingerprint(observation), facts])
        if any(row.group_id == group_id and row.payload['key'] == item.key and (row.entry_key != identity or row.fingerprint != fingerprint) for row in state['recovery_entries'].values()):
            raise HTTPException(409, 'Entry key already identifies different evidence; record a separate correction')
        old = next((row for row in state['recovery_entries'].values() if row.group_id == group_id and row.entry_key == identity and row.fingerprint == fingerprint), None)
        if not any(prior_identity == identity and prior_fingerprint == fingerprint for _, _, prior_identity, prior_fingerprint, _ in drafts):
            drafts.append((item, observation, identity, fingerprint, old))
    return drafts


async def preview_recovery(session, workspace_id, data):
    state = await _load(session, workspace_id, data.group_id)
    package = _project(state, workspace_id, data.group_id)
    for item, observation, _, _, old in _drafts(state, data.group_id, data.entries):
        if old:
            continue
        package.entries.append(RecoveryEntryRead(**_entry_payload(item), observation=observation,
                                                observation_id=item.observation_id,
                                                source_group_id=state['observations'][item.observation_id].group_id if item.observation_id else data.group_id,
                                                source_group_name=state['groups'][state['observations'][item.observation_id].group_id].name if item.observation_id and state['observations'][item.observation_id].group_id in state['groups'] else state['groups'][data.group_id].name,
                                                application=RecoveryApplication(reason_codes=['retain_before_movement_review'])))
    package.round_count = len({(row.case_key, row.round_key) for row in package.entries if row.round_key})
    package.asset_record_count = len({(row.case_key, row.round_key, row.round_asset_key) for row in package.entries if row.round_asset_key})
    return package


async def retain_recovery(session, workspace_id, user_id, data):
    try:
        state = await _load(session, workspace_id, data.group_id, lock=True)
        drafts = _drafts(state, data.group_id, data.entries)
        if any(old is None for *_, old in drafts) and data.expected_revision != state['recovery_revision']:
            raise HTTPException(409, 'Recovery evidence changed; refresh the preview')
        group = state['groups'][data.group_id]
        for item, observation, identity, fingerprint, old in drafts:
            if old:
                continue
            if item.observation_id:
                source = state['observations'][item.observation_id]
            else:
                retained, _ = await evidence._retain(session, workspace_id, data.group_id, group.connection_id, [observation])
                source = retained[observation.reference]
            leg = await session.scalar(select(InvestmentLeg).where(InvestmentLeg.observation_id == source.id, InvestmentLeg.source_leg_key == item.leg_key))
            session.add(InvestmentRecoveryEntry(workspace_id=workspace_id, group_id=data.group_id, observation_id=source.id,
                                                 leg_id=leg.id, entry_key=identity, fingerprint=fingerprint,
                                                 payload=_entry_payload(item), created_by=user_id))
        await session.commit()
        return await list_recovery(session, workspace_id, data.group_id)
    except Exception:
        await session.rollback()
        raise


def _validate_review(state, group_id, data):
    if data.assertion_kind == 'filing_assertion' and data.assertion_status == 'supported':
        raise HTTPException(422, 'filed_record_unverified: recovery evidence cannot verify a filing assertion')
    entry = _get(state, 'recovery_entries', data.entry_id)
    if entry.group_id != group_id:
        raise HTTPException(404, 'Review anchor is unavailable in the selected wallet')
    target = _get(state, 'recovery_entries', data.target_entry_id) if data.target_entry_id else None
    if target and (target.id == entry.id or target.payload['case_key'] != entry.payload['case_key']):
        raise HTTPException(422, 'Relationship target must be a distinct entry in the same recovery case')
    supporting = [_get(state, 'observations', identifier) for identifier in data.supporting_observation_ids]
    if data.relation_state == 'confirmed' or data.assertion_status == 'supported':
        sources = supporting + ([state['observations'][row.observation_id] for row in (entry, target) if row] if data.relation_state == 'confirmed' else [])
        if any(not source.is_current or str(source.id) in state['conflicting_observations'] for source in sources):
            raise HTTPException(422, 'Conflicting or superseded source evidence cannot support this review')
        if data.relation_state == 'confirmed' and any(row.payload['reported_state'] in {'conflict', 'missing'} for row in (entry, target) if row):
            raise HTTPException(422, 'The related source facts remain missing or disputed')
    for identifier in data.required_entry_ids:
        _get(state, 'recovery_entries', identifier)
    for identifier in data.required_review_ids:
        _get(state, 'recovery_reviews', identifier)
    if data.supersedes_id:
        old = _get(state, 'recovery_reviews', data.supersedes_id)
        if old.anchor_entry_id != entry.id or old.payload['kind'] != data.kind or old.payload.get('field') != data.field or old.payload.get('assertion_kind') != data.assertion_kind or old.payload.get('relation_kind') != data.relation_kind:
            raise HTTPException(422, 'A correction/review can supersede only the same logical assertion')
        if any(row.supersedes_id == old.id for row in state['recovery_reviews'].values()):
            raise HTTPException(409, 'This review has already been superseded')
    if data.owned_transfer_id:
        transfer = _get(state, 'transfers', data.owned_transfer_id)
        qualified = movements._transfer_read(state, transfer)
        if qualified.status != 'confirmed':
            raise HTTPException(422, 'Referenced owned transfer is not currently qualified')
    if data.owned_transfer_id and target:
        source_application = state['applications'][transfer.out_application_id]
        destination_application = state['applications'][transfer.in_application_id]
        if (state['observations'][entry.observation_id].group_id != state['assets'][source_application.asset_id].group_id
                or state['observations'][target.observation_id].group_id != state['assets'][destination_application.asset_id].group_id):
            raise HTTPException(422, 'Owned transfer endpoints do not establish this account chain')
        for candidate, asset_id in ((entry, source_application.asset_id), (target, destination_application.asset_id)):
            part = EvidenceLegInput.model_validate(state['legs'][candidate.leg_id].payload)
            if part.asset_id and part.asset_id != asset_id or evidence._holding_conflicts(part, state['assets'][asset_id]):
                raise HTTPException(422, 'Owned transfer asset does not support this relationship')
    if data.kind != 'relation' or target is None:
        return
    allowed = {
        'claim_notice': ({'allowed_claim', 'platform_ledger'}, {'recovery_notice'}),
        'notice_receipt': ({'recovery_notice'}, {'receiving_receipt'}),
        'receipt_disposition': ({'receiving_receipt'}, {'disposition'}),
        'disposition_proceeds': ({'disposition'}, {'cash_proceeds'}),
        'candidate_acquisition': ({'tax_workpaper'}, {'platform_ledger', 'receiving_receipt', 'disposition'}),
        'owned_transfer_reference': ({'receiving_receipt', 'platform_ledger'}, {'receiving_receipt', 'platform_ledger', 'disposition'}),
    }
    left, right = allowed[data.relation_kind]
    if entry.payload['role'] not in left or target.payload['role'] not in right:
        raise HTTPException(422, 'Source roles do not support this relationship kind')
    if data.relation_state != 'confirmed':
        return
    if data.missing_evidence or data.conflicting_fields or not data.supporting_observation_ids:
        raise HTTPException(422, 'Resolve conflicts and provide supporting evidence before confirming a relation')
    if data.relation_kind == 'candidate_acquisition':
        raise HTTPException(422, 'An acquisition correspondence remains a candidate pending provenance review')
    if data.relation_kind == 'owned_transfer_reference' and not data.owned_transfer_id:
        raise HTTPException(422, 'Use an existing confirmed owned transfer')
    if state['observations'][entry.observation_id].group_id != state['observations'][target.observation_id].group_id and data.relation_kind in {'receipt_disposition', 'owned_transfer_reference'}:
        if not data.owned_transfer_id:
            raise HTTPException(422, 'A cross-account disposition requires a supported owned-transfer link')
    if data.relation_kind in {'receipt_disposition', 'owned_transfer_reference'}:
        receipt = movements._application(state, entry.leg_id)
        if receipt is None or receipt.reversed_at or state['qualification'].get(receipt.id, {}).get('settlement_complete') is not True:
            raise HTTPException(422, 'Receipt lineage is not currently supported; retain a candidate')
        if data.owned_transfer_id:
            selected = source_application.payload.get('selected_lots', [])
            if not selected or any(lot.get('source_leg_id') != str(receipt.leg_id) for lot in selected):
                raise HTTPException(422, 'Owned transfer selected lots do not establish this receipt lineage')
        if data.relation_kind == 'receipt_disposition':
            sales = [state['transactions'][part.asset_transaction_id] for part in state['application_legs'].get(target.leg_id, [])
                     if part.asset_transaction_id in state['transactions'] and state['transactions'][part.asset_transaction_id].kind == 'sell']
            if len(sales) != 1:
                raise HTTPException(422, 'Disposition has no unique canonical sale; retain a candidate')
            sale = sales[0]
            replay = movements.movement_replay.replay([tx for tx in state['transactions'].values() if tx.asset_id == sale.asset_id])
            consumed = next((sale_row['lots'] for sale_row in replay['sales'] if sale_row['transaction_id'] == str(sale.id)), [])
            if not replay['settlement_complete'] or not consumed or any(lot.get('source_leg_id') != str(receipt.leg_id)
                    or data.owned_transfer_id and str(data.owned_transfer_id) not in lot.get('lineage', []) for lot in consumed):
                raise HTTPException(422, 'Recorded sale lots do not establish this recovery chain; retain a candidate')
    if data.relation_kind in {'notice_receipt', 'receipt_disposition'}:
        a = EvidenceLegInput.model_validate(state['legs'][entry.leg_id].payload)
        b = EvidenceLegInput.model_validate(state['legs'][target.leg_id].payload)
        if not evidence._same_asset(a, b):
            raise HTTPException(422, 'Related asset identities conflict')
        if a.quantity is None or b.quantity is None:
            raise HTTPException(422, 'Related quantities remain unknown')
        with localcontext(prec=512):
            matches = a.quantity + (data.quantity_adjustment or Decimal(0)) == b.quantity
        if not matches:
            raise HTTPException(422, 'Quantity difference needs an exact documented adjustment')
        if not data.account_mapping_evidence or not data.timing_evidence:
            raise HTTPException(422, 'Source account and timing compatibility require documented review')
        for source in (state['observations'][entry.observation_id], state['observations'][target.observation_id]):
            if not source.is_current or not (source.payload.get('event_date') or source.payload.get('event_at')):
                raise HTTPException(422, 'Source date/qualification is unresolved')
        if data.relation_kind == 'notice_receipt':
            if entry.payload.get('round_key') != target.payload.get('round_key') or not entry.payload.get('round_key'):
                raise HTTPException(422, 'Notice and receipt require the same explicit round')
            if state['observations'][target.observation_id].payload.get('settlement_status') != 'settled':
                raise HTTPException(422, 'Receipt settlement remains unresolved')


async def review_recovery(session, workspace_id, user_id, data):
    try:
        state = await _load(session, workspace_id, data.group_id, lock=True)
        if len({item.key for item in data.reviews}) != len(data.reviews):
            raise HTTPException(422, 'Review keys must be distinct')
        pending = []
        for item in data.reviews:
            payload = item.model_dump(mode='json')
            fingerprint = evidence._digest(payload)
            prior = next((row for row in state['recovery_reviews'].values() if row.group_id == data.group_id and row.review_key == item.key and row.fingerprint == fingerprint), None)
            if prior is None:
                if any(row.group_id == data.group_id and row.review_key == item.key for row in state['recovery_reviews'].values()):
                    raise HTTPException(409, 'Review key already identifies different evidence; append a new review with supersedes_id')
                pending.append((item, payload, fingerprint))
        if pending and data.expected_revision != state['recovery_revision']:
            raise HTTPException(409, 'Recovery evidence changed; refresh the review')
        for item, payload, fingerprint in pending:
            _validate_review(state, data.group_id, item)
            row = InvestmentRecoveryReview(id=uuid.uuid4(), workspace_id=workspace_id, group_id=data.group_id,
                                            anchor_entry_id=item.entry_id, supersedes_id=item.supersedes_id,
                                            review_key=item.key, fingerprint=fingerprint, payload=payload, created_by=user_id)
            session.add(row)
            state['recovery_reviews'][row.id] = row
        await session.commit()
        return await list_recovery(session, workspace_id, data.group_id)
    except Exception:
        await session.rollback()
        raise


def export_recovery(package, format, filters):
    payload = {'schema_version': 1, 'filters': filters, 'package': package.model_dump(mode='json')}
    if format == 'json':
        return json.dumps(payload, ensure_ascii=False, indent=2)
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['record_type', 'id', 'case_key', 'round_key', 'role', 'state', 'payload_json'])
    metadata = {key: value for key, value in payload['package'].items() if key not in {'entries', 'reviews'}}
    writer.writerow(['metadata', '', '', '', '', '', json.dumps({'schema_version': 1, 'filters': filters, **metadata})])
    for kind, records in (('entry', payload['package']['entries']), ('review', payload['package']['reviews'])):
        for row in records:
            # JSON is the lossless transport; readable columns never execute as spreadsheet formulas.
            cells = [kind, row['id'], row.get('case_key', ''), row.get('round_key') or '', row.get('role', row.get('kind')), row.get('reported_state', row.get('relation_state') or row.get('assertion_status'))]
            writer.writerow(["'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for value in cells] + [json.dumps(row, ensure_ascii=False)])
    return output.getvalue()
