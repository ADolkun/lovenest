import { StrictMode } from 'react'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { OwnedTransfersPanel } from './owned-transfers-panel'
import { ownedTransfers } from '@/lib/api'
import { renderWithProviders } from '@/test/utils'
import type { AssetGroup } from '@/types'
import type { HoldingEffect, RetainedMovement, TransferIndex, TransferLot, TransferPreview, TransferRead } from '@/types/owned-transfers'

const scope = vi.hoisted(() => ({ canWrite: true }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => scope }))

const wallets = [{ id: 'wallet-a', name: 'Exchange' }, { id: 'wallet-b', name: 'Owned wallet' }] as AssetGroup[]
const lot: TransferLot = { lot_id: 'original-acquisition:fragment-a', asset_id: 'asset-a', root_transaction_id: 'acquisition-a', source_leg_id: null, quantity: '2.123456789012345678', acquired: '2024-01-03', acquisition_cost: '40.123456789012345678', basis_complete: true, lineage: [], missing_links: [] }
const unknownLot: TransferLot = { ...lot, lot_id: 'opening-unknown', root_transaction_id: null, quantity: '1', acquired: null, acquisition_cost: null, basis_complete: false, missing_links: ['acquisition_missing'] }
const movement: RetainedMovement = { leg_id: 'leg-out', observation_id: 'observation-out', observation_ref: 'observation-out', leg_key: 'withdrawal', group_id: 'wallet-a', asset_id: 'asset-a', source: 'synthetic', source_local_id: 'outgoing-source', source_locator: 'source.csv:2', direction: 'out', classification: 'transfer', quantity: '3.123456789012345678', chain: 'solana', token_address: 'native', token_program: null, transaction_ref: 'synthetic-transaction', leg_ref: 'instruction:0', source_address: 'owner-a', destination_address: 'owner-b', source_owner: 'owner-a', destination_owner: 'owner-b', raw_units: '3123456789012345678', decimals: 18, quantity_role: 'principal', fee_payer: null, event_date: '2026-01-02', event_at: null, time_precision: 'date', provider_status: 'completed', network_status: 'finalized', settlement_status: 'settled', application_id: null, application_status: 'unapplied', reason_codes: [] }

function fixture(): TransferIndex {
  return {
    workspace_id: 'workspace', revision: 'revision-1', transfers: [], applications: [], incidents: [],
    movements: [movement, { ...movement, leg_id: 'leg-in', observation_id: 'observation-in', observation_ref: 'observation-in', leg_key: 'receipt', source_local_id: 'incoming-source', direction: 'in', group_id: 'wallet-b', asset_id: 'asset-b' }, { ...movement, leg_id: 'leg-fee', observation_id: 'observation-fee', observation_ref: 'observation-fee', leg_key: 'fee', source_local_id: 'fee-source', classification: 'fee', quantity: '0.000000000000000001', quantity_role: 'network_fee', fee_payer: 'owner-a' }],
    holdings: [{ id: 'asset-a', group_id: 'wallet-a', name: 'Source holding', ticker: 'SYN', currency: 'USD', units: '10', is_archived: false }, { id: 'asset-b', group_id: 'wallet-b', name: 'Destination holding', ticker: 'SYN', currency: 'USD', units: '3.123456789012345678', is_archived: true }],
    ownership: ['a', 'b'].map((suffix) => ({ id: `ownership-${suffix}`, group_id: `wallet-${suffix}`, beneficial_owner: 'same-owner', chain: 'solana', address: `owner-${suffix}`, source_account_id: null, valid_from: '2024-01-01', valid_until: null, reason: 'Documented ownership', evidence_observation_ids: [], workspace_id: 'workspace', asserted_by: 'user', asserted_at: '2026-01-01T00:00:00Z', revoked_at: null })),
  }
}

const effect: HoldingEffect = { asset_id: 'asset-b', quantity: '3.123456789012345678', known_basis_quantity: '2.123456789012345678', unknown_basis_quantity: '1', known_acquisition_cost: '40.123456789012345678', performance_basis: null, basis_complete: false, settlement_complete: true, realized_gain: null, known_realized_gain: '0', unknown_disposition_quantity: '0', missing_links: ['acquisition_missing', 'funding_unknown'], lots: [lot, unknownLot] }
let index: TransferIndex
let preview: TransferPreview
let saved: TransferRead

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  scope.canWrite = true
  index = fixture()
  preview = { workspace_id: 'workspace', revision: 'preview-1', status: 'exact', can_confirm: true, reason_codes: ['funding_unknown'], out_movement: index.movements[0], in_movement: index.movements[1], available_lots: [lot, unknownLot], principal_quantity: movement.quantity, acquisition_cost: null, known_acquisition_cost: '40.123456789012345678', performance_basis: null, unknown_basis_quantity: '1', fee_movements: [], effects: [effect] }
  saved = { id: 'transfer-a', workspace_id: 'workspace', revision: 'saved-1', status: 'confirmed', request: { out_leg_id: 'leg-out', in_leg_id: 'leg-in', source_asset_id: 'asset-a', destination_asset_id: 'asset-b', source_ownership_id: 'ownership-a', destination_ownership_id: 'ownership-b', allocations: [{ lot_id: lot.lot_id, quantity: '2.123456789012345678' }, { lot_id: unknownLot.lot_id, quantity: '1' }], fees: [], reason: 'Reviewed source documents', ordering_reviewed: false }, principal_quantity: movement.quantity!, acquisition_cost: null, known_acquisition_cost: preview.known_acquisition_cost, performance_basis: null, unknown_basis_quantity: '1', reason_codes: [], created_at: '2026-01-03T00:00:00Z', reversed_at: null, effects: [effect] }
  vi.spyOn(ownedTransfers, 'index').mockImplementation(async () => index)
  vi.spyOn(ownedTransfers, 'lots').mockImplementation(async (_workspace, assetId) => ({ revision: index.revision, asset_id: assetId, lots: [lot, unknownLot], missing_links: [] }))
  vi.spyOn(ownedTransfers, 'preview').mockImplementation(async () => preview)
  vi.spyOn(ownedTransfers, 'confirm').mockImplementation(async () => { index = { ...index, revision: 'revision-2', transfers: [saved] }; return saved })
  vi.spyOn(ownedTransfers, 'reverse').mockImplementation(async () => { saved = { ...saved, revision: 'saved-2', status: 'reversed', reversed_at: '2026-01-04T00:00:00Z' }; index = { ...index, revision: 'revision-3', transfers: [saved] }; return saved })
  vi.spyOn(ownedTransfers, 'createOwnership').mockImplementation(async (_workspace, body) => { const assertion = { ...body, id: 'ownership-new', workspace_id: 'workspace', asserted_by: 'user', asserted_at: '2026-01-01T00:00:00Z', revoked_at: null }; index = { ...index, revision: 'ownership-revision', ownership: [...index.ownership, assertion] }; return assertion })
  vi.spyOn(ownedTransfers, 'annotate').mockImplementation(async (_workspace, body) => { const incident = { ...body, id: 'incident-a', workspace_id: 'workspace', created_by: 'user', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', tax_treatment: 'unresolved' as const }; index = { ...index, incidents: [incident] }; return incident })
})

const panel = (workspaceId = 'workspace') => <StrictMode><OwnedTransfersPanel key={workspaceId} workspaceId={workspaceId} wallets={wallets} scopeWalletIds={['wallet-a']} /></StrictMode>

async function openMovement(route = '/assets?tab=activity&activity=transfers&movement=leg-out') {
  const rendered = renderWithProviders(panel(), { route })
  await screen.findByLabelText('Holding for this movement')
  return rendered
}

async function prepare() {
  const rendered = await openMovement()
  const { user } = rendered
  await user.selectOptions(screen.getByLabelText('Confirmed ownership mapping'), 'ownership-a')
  await user.selectOptions(screen.getByLabelText('Corresponding retained movement'), 'leg-in')
  await user.selectOptions(screen.getByLabelText('Holding for the other endpoint'), 'asset-b')
  await user.selectOptions(screen.getAllByLabelText('Confirmed ownership mapping')[1], 'ownership-b')
  await user.click(screen.getByRole('checkbox', { name: /^Lot 1/ }))
  fireEvent.change(screen.getByLabelText('Selected quantity · Lot 1'), { target: { value: '2.123456789012345678' } })
  await user.click(screen.getByRole('checkbox', { name: /^Lot 2/ }))
  fireEvent.change(screen.getByLabelText('Selected quantity · Lot 2'), { target: { value: '1' } })
  fireEvent.change(screen.getByLabelText('Evidence supporting this decision'), { target: { value: 'Reviewed source documents' } })
  return rendered
}

it('previews and confirms exact selected lots in StrictMode without inventing unknown cost or date', async () => {
  const { user } = await prepare()
  expect(ownedTransfers.confirm).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Preview exact effects' }))
  const reviewed = within(await screen.findByRole('region', { name: 'Reviewed effects' }))
  expect(reviewed.getAllByText('40.123456789012345678').length).toBeGreaterThan(0)
  expect(reviewed.getByText('original acquisition cost').nextElementSibling).toHaveTextContent('Unknown')
  expect(reviewed.getAllByText('performance basis')[0].nextElementSibling).toHaveTextContent('Unknown')
  expect(ownedTransfers.preview).toHaveBeenCalledWith('workspace', saved.request)
  expect(reviewed.getByRole('button', { name: 'Confirm owned transfer' })).toBeDisabled()
  await user.click(reviewed.getByRole('checkbox'))
  await user.dblClick(reviewed.getByRole('button', { name: 'Confirm owned transfer' }))
  await waitFor(() => expect(ownedTransfers.confirm).toHaveBeenCalledTimes(1))
  expect(ownedTransfers.confirm).toHaveBeenCalledWith('workspace', { ...saved.request, expected_revision: 'preview-1' })
  expect(await screen.findByText(/Decision saved/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Confirm owned transfer' })).not.toBeInTheDocument()
})

it('records explicitly reviewed ownership in StrictMode and refreshes the selected assertion', async () => {
  const { user } = await openMovement()
  await user.click(screen.getByText('Record an ownership assertion'))
  await user.type(screen.getByLabelText('Owner identifier'), 'Documented owner')
  await user.type(screen.getByLabelText('Owned address'), 'owner-a')
  await user.type(screen.getByLabelText('Ownership supported from'), '2024-01-01')
  await user.type(screen.getByLabelText('Evidence supporting ownership'), 'Account ownership statement')
  const save = screen.getByRole('button', { name: 'Save ownership assertion' })
  expect(save).toBeDisabled()
  await user.click(screen.getByRole('checkbox', { name: /I reviewed this account or address/ }))
  await user.click(save)
  await waitFor(() => expect(screen.getByLabelText('Confirmed ownership mapping')).toHaveValue('ownership-new'))
  expect(save).toBeDisabled()
  expect(ownedTransfers.createOwnership).toHaveBeenCalledWith('workspace', expect.objectContaining({ beneficial_owner: 'Documented owner', address: 'owner-a', valid_from: '2024-01-01', evidence_observation_ids: ['observation-out'] }))
  expect(ownedTransfers.confirm).not.toHaveBeenCalled()
})

it('enriches already-applied principal legs and preserves independent quantity reversal after transfer reversal', async () => {
  index.applications = index.movements.slice(0, 2).map((item, i) => ({
    id: `application-${i}`, workspace_id: 'workspace', revision: `application-revision-${i}`, status: 'applied',
    request: { leg_id: item.leg_id, asset_id: item.asset_id!, ownership_id: i ? 'ownership-b' : 'ownership-a', allocations: i ? [] : saved.request.allocations, reason: 'Quantity supported independently', ordering_reviewed: false },
    created_at: '2026-01-02T00:00:00Z', reversed_at: null, reason_codes: [], selected_lots: [], effects: [effect],
  }))
  index.movements = index.movements.map((item, i) => i < 2 ? { ...item, application_id: `application-${i}`, application_status: 'applied' } : item)
  const applyQuantity = vi.spyOn(ownedTransfers, 'applyMovement')
  const reverseQuantity = vi.spyOn(ownedTransfers, 'reverseMovement')
  const { user } = await prepare()
  expect(screen.getByLabelText('Reviewed action')).toHaveValue('transfer')
  expect(screen.queryByRole('option', { name: /Apply this quantity only/ })).not.toBeInTheDocument()
  expect(screen.getByText(/This quantity is already applied/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Preview exact effects' }))
  await user.click(await screen.findByRole('checkbox', { name: /I reviewed both endpoint/ }))
  await user.click(screen.getByRole('button', { name: 'Confirm owned transfer' }))
  await user.click(await screen.findByRole('link', { name: /Open transfer decision/ }))
  await user.click(await screen.findByRole('checkbox', { name: /I reviewed the affected holdings/ }))
  await user.click(screen.getByRole('button', { name: 'Confirm transfer reversal' }))
  expect(await screen.findByText('Decision status: reversed')).toBeInTheDocument()
  await user.click(screen.getByRole('link', { name: /Open source movement/ }))
  expect(await screen.findByText('Decision status: applied')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Confirm quantity reversal' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Preview exact effects' })).not.toBeInTheDocument()
  expect(ownedTransfers.confirm).toHaveBeenCalledWith('workspace', { ...saved.request, expected_revision: 'preview-1' })
  expect(applyQuantity).not.toHaveBeenCalled()
  expect(reverseQuantity).not.toHaveBeenCalled()
})

it('requires fee lot selection separately and sends tiny quantities without number rounding', async () => {
  const { user } = await prepare()
  await user.click(screen.getByText('Review separately evidenced fee units'))
  await user.click(screen.getByRole('checkbox', { name: /fee-source.*unapplied/ }))
  const feeScope = within(screen.getByText('Fee payer ownership').closest('fieldset')!.parentElement!)
  await user.selectOptions(feeScope.getByLabelText('Confirmed ownership mapping'), 'ownership-a')
  await user.click(feeScope.getByRole('checkbox', { name: /^Lot 1/ }))
  fireEvent.change(feeScope.getByLabelText('Selected quantity · Lot 1'), { target: { value: '0.000000000000000001' } })
  await user.click(screen.getByRole('button', { name: 'Preview exact effects' }))
  await waitFor(() => expect(ownedTransfers.preview).toHaveBeenCalledWith('workspace', expect.objectContaining({ fees: [{ leg_id: 'leg-fee', asset_id: 'asset-a', ownership_id: 'ownership-a', allocations: [{ lot_id: lot.lot_id, quantity: '0.000000000000000001' }], reason: 'Reviewed source documents' }] })))
})

it('shows candidates and stale-review errors without allowing confirmation or reusing old consent', async () => {
  preview = { ...preview, can_confirm: false, status: 'candidate', reason_codes: ['settlement_unresolved'] }
  const { user } = await prepare()
  await user.click(screen.getByRole('button', { name: 'Preview exact effects' }))
  expect(await screen.findByText('settlement unresolved')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Confirm owned transfer' })).toBeDisabled()
  preview = { ...preview, can_confirm: true, status: 'exact', reason_codes: [] }
  await user.click(screen.getByRole('button', { name: 'Preview exact effects' }))
  await user.click(await screen.findByRole('checkbox', { name: /I reviewed both endpoint/ }))
  vi.mocked(ownedTransfers.confirm).mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: 'review_changed', message: 'Source inventory changed. Review again.' } } } })
  await user.click(screen.getByRole('button', { name: 'Confirm owned transfer' }))
  expect(await screen.findByText('Source inventory changed. Review again.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Confirm owned transfer' })).not.toBeInTheDocument()
  expect(ownedTransfers.index).toHaveBeenCalledTimes(2)
})

it('reverses atomically in StrictMode and keeps inspectable dependency conflicts', async () => {
  index.transfers = [saved]
  vi.mocked(ownedTransfers.reverse).mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: 'dependent_activity', message: 'Reverse the next transfer first.', dependencies: [{ type: 'transfer', id: 'transfer-next' }] } } } })
  const { user } = renderWithProviders(panel(), { route: '/assets?tab=activity&activity=transfers&transfer=transfer-a' })
  const check = await screen.findByRole('checkbox', { name: /I reviewed the affected holdings/ })
  await user.click(check)
  await user.click(screen.getByRole('button', { name: 'Confirm transfer reversal' }))
  expect(await screen.findByText('Reverse the next transfer first.')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Open dependent transfer/ })).toHaveAttribute('href', expect.stringContaining('transfer=transfer-next'))
  expect(screen.getByRole('button', { name: 'Confirm transfer reversal' })).toBeDisabled()
  await user.click(check)
  await user.click(screen.getByRole('button', { name: 'Confirm transfer reversal' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Confirm transfer reversal' })).not.toBeInTheDocument())
  expect(screen.getByText('Decision status: reversed')).toBeInTheDocument()
  expect(ownedTransfers.reverse).toHaveBeenLastCalledWith('workspace', 'transfer-a', 'saved-1')
})

it('saves a reported-scam allegation without financial action or resolved tax treatment', async () => {
  const { user } = await openMovement()
  await user.click(screen.getByText('Reported scam annotation'))
  await user.type(screen.getByLabelText('Allegation and supporting source'), 'Reported by the account owner; claim unresolved')
  await user.selectOptions(screen.getByLabelText('Allegation source status'), 'disputed')
  await user.click(screen.getByRole('button', { name: 'Save allegation' }))
  expect(await screen.findByText(/Saved allegation.*Reported by the account owner/)).toHaveTextContent('Tax treatment unresolved')
  expect(ownedTransfers.annotate).toHaveBeenCalledWith('workspace', expect.objectContaining({ allegation: 'reported_scam', source_status: 'disputed', evidence_observation_ids: ['observation-out'] }), undefined)
  expect(ownedTransfers.confirm).not.toHaveBeenCalled()
})

it('applies a reviewed unknown-basis incoming quantity without selecting or inventing an acquisition lot', async () => {
  const request = { leg_id: 'leg-in', asset_id: 'asset-b', ownership_id: 'ownership-b', allocations: [], reason: 'Receipt supported; acquisition missing', ordering_reviewed: false }
  vi.spyOn(ownedTransfers, 'previewMovement').mockResolvedValue({ workspace_id: 'workspace', revision: 'movement-preview', can_confirm: true, reason_codes: ['acquisition_missing'], movement: index.movements[1], available_lots: [], selected_lots: [], effects: [effect] })
  vi.spyOn(ownedTransfers, 'applyMovement').mockImplementation(async () => {
    const application = { id: 'application-b', workspace_id: 'workspace', revision: 'movement-applied', status: 'applied' as const, request, created_at: '2026-01-03T00:00:00Z', reversed_at: null, reason_codes: ['acquisition_missing'], selected_lots: [], effects: [effect] }
    index = { ...index, revision: 'applied', applications: [application], movements: index.movements.map((item) => item.leg_id === 'leg-in' ? { ...item, application_id: application.id, application_status: 'applied' } : item) }
    return application
  })
  const { user } = await openMovement('/assets?tab=activity&activity=transfers&movement=leg-in')
  await user.selectOptions(screen.getByLabelText('Reviewed action'), 'movement')
  await user.selectOptions(screen.getByLabelText('Confirmed ownership mapping'), 'ownership-b')
  await user.type(screen.getByLabelText('Evidence supporting this decision'), request.reason)
  expect(screen.queryByRole('checkbox', { name: /^Lot 1/ })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Preview exact effects' }))
  await user.click(await screen.findByRole('checkbox', { name: /I reviewed both endpoint/ }))
  await user.click(screen.getByRole('button', { name: 'Apply reviewed quantity' }))
  expect(await screen.findByText(/Decision saved/)).toBeInTheDocument()
  expect(ownedTransfers.applyMovement).toHaveBeenCalledWith('workspace', { ...request, expected_revision: 'movement-preview' })
  expect(screen.getByLabelText('Reviewed action')).toHaveValue('transfer')
  expect(screen.queryByRole('option', { name: /Apply this quantity only/ })).not.toBeInTheDocument()
  expect(screen.getByText(/This quantity is already applied/)).toBeInTheDocument()
  expect(ownedTransfers.confirm).not.toHaveBeenCalled()
})

it('keeps consumed source lots and prior transfer links after a full external outflow leaves no inventory', async () => {
  const selectedLots = [{ ...lot, source_leg_id: 'earlier-source-leg', lineage: ['earlier-transfer'] }, unknownLot]
  const emptyEffect = { ...effect, asset_id: 'asset-a', quantity: '0', known_basis_quantity: '0', unknown_basis_quantity: '0', known_acquisition_cost: '0', performance_basis: '0', lots: [] }
  const request = { leg_id: 'leg-out', asset_id: 'asset-a', ownership_id: 'ownership-a', allocations: saved.request.allocations, reason: 'External outflow supported; treatment unknown', ordering_reviewed: false }
  vi.spyOn(ownedTransfers, 'previewMovement').mockResolvedValue({ workspace_id: 'workspace', revision: 'outflow-preview', can_confirm: true, reason_codes: ['tax_treatment_unknown'], movement, available_lots: [lot, unknownLot], selected_lots: selectedLots, effects: [emptyEffect] })
  vi.spyOn(ownedTransfers, 'applyMovement').mockImplementation(async () => {
    const application = { id: 'external-application', workspace_id: 'workspace', revision: 'outflow-applied', status: 'applied' as const, request, created_at: '2026-01-03T00:00:00Z', reversed_at: null, reason_codes: ['tax_treatment_unknown'], selected_lots: selectedLots, effects: [emptyEffect] }
    index = { ...index, revision: 'outflow-applied', applications: [application], movements: index.movements.map((item) => item.leg_id === 'leg-out' ? { ...item, application_id: application.id, application_status: 'applied' } : item) }
    return application
  })
  const { user } = await openMovement()
  await user.selectOptions(screen.getByLabelText('Reviewed action'), 'movement')
  await user.selectOptions(screen.getByLabelText('Confirmed ownership mapping'), 'ownership-a')
  await user.click(screen.getByRole('checkbox', { name: /^Lot 1/ }))
  fireEvent.change(screen.getByLabelText('Selected quantity · Lot 1'), { target: { value: request.allocations[0].quantity } })
  await user.click(screen.getByRole('checkbox', { name: /^Lot 2/ }))
  fireEvent.change(screen.getByLabelText('Selected quantity · Lot 2'), { target: { value: '1' } })
  fireEvent.change(screen.getByLabelText('Evidence supporting this decision'), { target: { value: request.reason } })
  await user.click(screen.getByRole('button', { name: 'Preview exact effects' }))
  await user.click(await screen.findByText('Selected / consumed source lineage'))
  expect(screen.getByText('Original acquisition: acquisition-a')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Open transfer earlier-transfer' })).toHaveAttribute('href', expect.stringContaining('transfer=earlier-transfer'))
  await user.click(screen.getByRole('checkbox', { name: /I reviewed both endpoint/ }))
  await user.click(screen.getByRole('button', { name: 'Apply reviewed quantity' }))
  expect(await screen.findByText('Decision status: applied')).toBeInTheDocument()
  await user.click(screen.getByText('Selected / consumed source lineage'))
  const lineage = within(screen.getByText('Selected / consumed source lineage').parentElement!)
  expect(lineage.getByText('Original acquisition: acquisition-a')).toBeInTheDocument()
  expect(lineage.getByText('Acquired: Unknown · Quantity: 1 · Original acquisition cost: Unknown')).toBeInTheDocument()
  expect(lineage.getByRole('link', { name: 'Open source movement earlier-source-leg' })).toHaveAttribute('href', expect.stringContaining('movement=earlier-source-leg'))
  expect(lineage.getByRole('link', { name: 'Open transfer earlier-transfer' })).toBeInTheDocument()
  expect(screen.getByText('replay quantity').nextElementSibling).toHaveTextContent('0')
  expect(screen.getByText('realized gain').nextElementSibling).toHaveTextContent('Unknown')
  expect(screen.queryByText('Original acquisition lineage')).not.toBeInTheDocument()
  expect(ownedTransfers.applyMovement).toHaveBeenCalledWith('workspace', { ...request, expected_revision: 'outflow-preview' })
  expect(ownedTransfers.confirm).not.toHaveBeenCalled()
})

it('retains archived source scope, exact source deep links, pagination and no-match states', async () => {
  index.movements = Array.from({ length: 26 }, (_, i) => ({ ...movement, leg_id: `leg-${i}`, source_local_id: `source-${i}`, observation_id: `obs-${i}`, observation_ref: `obs-${i}` }))
  const { user } = renderWithProviders(panel(), { route: '/assets?tab=activity&activity=transfers&wallet=wallet-a' })
  await screen.findByText('source-0')
  expect(screen.queryByText('source-25')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Next' }))
  expect(screen.getByText('source-25')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Search source or reference'), 'does-not-exist')
  expect(screen.getByText(/No records match these filters/)).toBeInTheDocument()
  expect(ownedTransfers.confirm).not.toHaveBeenCalled()
})

it('keeps viewer review read-only and opens an observation by its exact source leg key', async () => {
  scope.canWrite = false
  await openMovement('/assets?tab=activity&activity=transfers&observation_ref=observation-out&leg_key=withdrawal')
  expect(screen.getByRole('button', { name: 'Preview exact effects' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Save ownership assertion' })).not.toBeInTheDocument()
  expect(screen.getByText(/Viewers can inspect saved evidence/)).toBeInTheDocument()
  expect(ownedTransfers.createOwnership).not.toHaveBeenCalled()
})

it('ignores a late preview after switching workspace and masks precise source facts', async () => {
  let resolve!: (value: TransferPreview) => void
  vi.mocked(ownedTransfers.preview).mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const { user, rerender } = await prepare()
  await user.click(screen.getByRole('button', { name: 'Preview exact effects' }))
  index = { ...fixture(), workspace_id: 'other-workspace', movements: [], ownership: [], holdings: [] }
  rerender(panel('other-workspace'))
  await act(async () => resolve(preview))
  expect(screen.queryByRole('button', { name: 'Confirm owned transfer' })).not.toBeInTheDocument()
  expect(ownedTransfers.confirm).not.toHaveBeenCalled()
})

it('masks addresses and exact amounts while distinguishing fetch failure from empty evidence', async () => {
  localStorage.setItem('privacyMode', 'true')
  vi.mocked(ownedTransfers.index).mockRejectedValueOnce({ response: { status: 503, data: { detail: { message: 'Retained evidence unavailable' } } } })
  const { user } = renderWithProviders(panel(), { route: '/assets?tab=activity&activity=transfers' })
  expect((await screen.findAllByRole('alert'))[0]).toBeInTheDocument()
  expect(screen.queryByText(/No retained movements/)).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Refresh review' }))
  const first = (await screen.findAllByRole('button')).find((button) => button.textContent?.includes('•••••') && button.textContent.includes('settled'))!
  await user.click(first)
  expect(screen.queryByText('owner-a')).not.toBeInTheDocument()
  expect(screen.queryByText('3.123456789012345678')).not.toBeInTheDocument()
})
