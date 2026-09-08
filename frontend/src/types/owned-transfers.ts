/** Mirrors backend/app/schemas/owned_transfer.py. Exact values never pass through Number. */
export interface OwnershipCreate {
  group_id: string
  beneficial_owner: string
  chain: string
  address: string | null
  source_account_id: string | null
  valid_from: string | null
  valid_until: string | null
  reason: string
  evidence_observation_ids: string[]
}

export interface OwnershipRead extends OwnershipCreate {
  id: string
  workspace_id: string
  asserted_by: string | null
  asserted_at: string
  revoked_at: string | null
}

export interface LotSelection { lot_id: string; quantity: string }

export interface TransferLot {
  lot_id: string
  asset_id: string
  root_transaction_id: string | null
  source_leg_id: string | null
  quantity: string
  acquired: string | null
  acquisition_cost: string | null
  basis_complete: boolean
  lineage: string[]
  missing_links: string[]
}

export interface MovementSelection {
  leg_id: string
  asset_id: string
  ownership_id: string
  allocations: LotSelection[]
  reason: string
}

export interface TransferRequest {
  out_leg_id: string
  in_leg_id: string
  source_asset_id: string
  destination_asset_id: string
  source_ownership_id: string
  destination_ownership_id: string
  allocations: LotSelection[]
  fees: MovementSelection[]
  reason: string
  ordering_reviewed: boolean
}

export interface MovementRequest extends MovementSelection { ordering_reviewed: boolean }

export interface RetainedMovement {
  leg_id: string
  observation_id: string
  observation_ref: string
  leg_key: string
  group_id: string | null
  asset_id: string | null
  source: string
  source_local_id: string | null
  source_locator: string
  direction: string
  classification: string
  quantity: string | null
  chain: string | null
  token_address: string | null
  token_program: string | null
  transaction_ref: string | null
  leg_ref: string | null
  source_address: string | null
  destination_address: string | null
  source_owner: string | null
  destination_owner: string | null
  raw_units: string | null
  decimals: number | null
  quantity_role: string | null
  fee_payer: string | null
  event_date: string | null
  event_at: string | null
  time_precision: string
  provider_status: string | null
  network_status: string | null
  settlement_status: string
  application_id: string | null
  application_status: 'unapplied' | 'applied' | 'reversed'
  reason_codes: string[]
}

export interface HoldingEffect {
  asset_id: string
  quantity: string
  known_basis_quantity: string
  unknown_basis_quantity: string
  known_acquisition_cost: string
  performance_basis: string | null
  basis_complete: boolean
  settlement_complete: boolean
  realized_gain: string | null
  known_realized_gain: string
  unknown_disposition_quantity: string
  missing_links: string[]
  lots: TransferLot[]
}

export interface TransferPreview {
  workspace_id: string
  revision: string
  status: 'exact' | 'candidate' | 'conflicting'
  can_confirm: boolean
  reason_codes: string[]
  out_movement: RetainedMovement
  in_movement: RetainedMovement
  available_lots: TransferLot[]
  principal_quantity: string | null
  acquisition_cost: string | null
  known_acquisition_cost: string
  performance_basis: string | null
  unknown_basis_quantity: string
  fee_movements: RetainedMovement[]
  effects: HoldingEffect[]
}

export interface TransferRead {
  id: string
  workspace_id: string
  revision: string
  status: 'confirmed' | 'unresolved' | 'reversed'
  request: TransferRequest
  principal_quantity: string
  acquisition_cost: string | null
  known_acquisition_cost: string
  performance_basis: string | null
  unknown_basis_quantity: string
  reason_codes: string[]
  created_at: string
  reversed_at: string | null
  effects: HoldingEffect[]
}

export interface MovementPreview {
  workspace_id: string
  revision: string
  can_confirm: boolean
  reason_codes: string[]
  movement: RetainedMovement
  available_lots: TransferLot[]
  selected_lots: TransferLot[]
  effects: HoldingEffect[]
}

export interface MovementApplication {
  id: string
  workspace_id: string
  revision: string
  status: 'applied' | 'unresolved' | 'reversed'
  request: MovementRequest
  created_at: string
  reversed_at: string | null
  reason_codes: string[]
  selected_lots: TransferLot[]
  effects: HoldingEffect[]
}

export interface IncidentCreate {
  leg_id: string
  allegation: 'reported_scam'
  source_status: 'user_reported' | 'documented' | 'disputed'
  note: string
  evidence_observation_ids: string[]
  related_fee_leg_ids: string[]
}

export interface IncidentRead extends IncidentCreate {
  id: string
  workspace_id: string
  created_by: string | null
  created_at: string
  updated_at: string
  tax_treatment: 'unresolved'
}

export interface TransferHolding {
  id: string
  group_id: string | null
  name: string
  ticker: string | null
  currency: string
  units: string | null
  is_archived: boolean
}

export interface TransferIndex {
  workspace_id: string
  revision: string
  transfers: TransferRead[]
  movements: RetainedMovement[]
  applications: MovementApplication[]
  ownership: OwnershipRead[]
  holdings: TransferHolding[]
  incidents: IncidentRead[]
}

export interface TransferLots { revision: string; asset_id: string; lots: TransferLot[]; missing_links: string[] }
